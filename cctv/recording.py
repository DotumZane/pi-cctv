"""Segmented, stream-copy recording helpers for the centralized recorder."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from .store import Store

SCRUB_ATLAS_BUILD_SLOT = threading.BoundedSemaphore(1)
SCRUB_ATLAS_FPS = 8
SCRUB_BACKFILL_FPS = 1
SCRUB_ATLAS_RETRY_SECONDS = 600


def recording_files(root: Path) -> list[Path]:
    files = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        files.extend(Path(directory) / name for name in names
                     if not name.startswith(".") and name.endswith((".mkv", ".mp4")))
    return files


def mp4_duration(path: Path) -> float | None:
    """Read the movie duration without decoding video or scanning media data."""
    def boxes(handle, end):
        while handle.tell() + 8 <= end:
            start = handle.tell()
            header = handle.read(8)
            if len(header) != 8:
                return
            size, kind = struct.unpack('>I4s', header)
            if size == 1:
                extended = handle.read(8)
                if len(extended) != 8:
                    return
                size = struct.unpack('>Q', extended)[0]
            elif size == 0:
                size = end - start
            finish = start + size
            if finish < handle.tell() or finish > end:
                return
            yield kind, finish
            handle.seek(finish)
    try:
        with path.open('rb') as handle:
            size = path.stat().st_size
            for kind, end in boxes(handle, size):
                if kind != b'moov':
                    continue
                for child, child_end in boxes(handle, end):
                    if child != b'mvhd':
                        continue
                    payload = handle.read(min(32, child_end - handle.tell()))
                    if not payload:
                        return None
                    if payload[0] == 0 and len(payload) >= 20:
                        scale, duration = struct.unpack_from('>II', payload, 12)
                        unknown = 0xffffffff
                    elif payload[0] == 1 and len(payload) >= 32:
                        scale, duration = struct.unpack_from('>IQ', payload, 20)
                        unknown = 0xffffffffffffffff
                    else:
                        return None
                    seconds = duration / scale if scale and duration != unknown else 0
                    return seconds if 0 < seconds <= 7 * 86400 else None
    except (OSError, ValueError, struct.error):
        pass
    return None


def trickplay_path(root: Path, recording: Path) -> Path:
    relative = recording.relative_to(root)
    return root / ".trickplay-cache" / relative.parent / f"{relative.stem}.mp4"


def playback_cache_path(root: Path, recording: Path) -> Path:
    relative = recording.relative_to(root)
    return root / '.playback-cache' / relative.parent / f'{relative.stem}.mp4'


def scrub_frames_dir(root: Path, recording: Path) -> Path:
    relative = recording.relative_to(root)
    return root / ".scrub-frames" / relative.parent / relative.stem


def scrub_frames_ready(source: Path, directory: Path) -> bool:
    manifest = directory / "manifest.json"
    return manifest.is_file() and manifest.stat().st_mtime_ns >= source.stat().st_mtime_ns


def prepare_scrub_frames(source: Path, directory: Path, fps: int = SCRUB_ATLAS_FPS) -> None:
    """Build a compact sequence of real frames in seek-free JPEG atlases."""
    try:
        if scrub_frames_ready(source, directory):
            return
    except OSError:
        return
    if not SCRUB_ATLAS_BUILD_SLOT.acquire(blocking=False):
        return
    temporary = directory.with_name(f"{directory.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    failure = directory.with_suffix(".failed")
    success = False
    try:
        if scrub_frames_ready(source, directory):
            success = True
            return
        directory.parent.mkdir(parents=True, exist_ok=True)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
            capture_output=True, text=True, timeout=20,
        )
        duration = float(probe.stdout.strip()) if probe.returncode == 0 else 0
        if not math.isfinite(duration) or duration <= 0:
            return
        temporary.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", str(source), "-map", "0:v:0", "-an",
             "-vf", f"fps={fps},scale=384:216:force_original_aspect_ratio=decrease,"
                    "pad=384:216:(ow-iw)/2:(oh-ih)/2,tile=6x4",
             "-fps_mode", "vfr", "-q:v", "7", "-start_number", "0",
             str(temporary / "%03d.jpg")],
            capture_output=True, timeout=180,
        )
        sheets = sorted(temporary.glob("*.jpg"))
        if result.returncode != 0 or not sheets:
            return
        frame_count = min(math.ceil(duration * fps), len(sheets) * 24)
        (temporary / "manifest.json").write_text(json.dumps({
            "fps": fps, "width": 384, "height": 216, "columns": 6, "rows": 4,
            "frames": frame_count, "sheets": len(sheets),
        }))
        if directory.exists():
            shutil.rmtree(directory)
        temporary.replace(directory)
        failure.unlink(missing_ok=True)
        success = True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    finally:
        try:
            shutil.rmtree(temporary, ignore_errors=True)
            if not success and directory.parent.is_dir():
                failure.touch()
        finally:
            SCRUB_ATLAS_BUILD_SLOT.release()


def prepare_trickplay(recording: Path, target: Path) -> None:
    """Produce an independently decodable, silent scrub proxy."""
    temporary = target.with_name(f"{target.stem}.{os.getpid()}.{threading.get_ident()}.tmp.mp4")
    failure = target.with_suffix(".failed")
    success = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", str(recording), "-map", "0:v:0", "-an", "-vf", "fps=8,scale=640:-2",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", "-g", "1",
             "-bf", "0", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary)],
            capture_output=True, timeout=180,
        )
        if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size:
            temporary.replace(target)
            failure.unlink(missing_ok=True)
            success = True
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        temporary.unlink(missing_ok=True)
        if not success:
            failure.touch()


@dataclass(frozen=True)
class RecorderConfig:
    camera_id: str
    stream_url: str
    output_dir: Path
    segment_seconds: int = 30
    retention_hours: int = 24
    storage_limit_gb: int = 500

    def ffmpeg_command(self) -> list[str]:
        output = self.output_dir / self.camera_id / "%Y-%m-%d" / "%H-%M-%S.mp4"
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-rtsp_transport", "tcp", "-fflags", "nobuffer", "-i", self.stream_url,
            "-map", "0:v:0", "-c", "copy", "-f", "segment", "-segment_format", "mp4",
            "-segment_format_options", "movflags=+faststart",
            "-segment_time", str(self.segment_seconds), "-strftime", "1",
            "-reset_timestamps", "1", str(output),
        ]

    def prune_before(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(hours=self.retention_hours)


def prune_recordings(output_dir: Path, retention_hours: int, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
    removed = 0
    for path in recording_files(output_dir):
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()
            proxy = trickplay_path(output_dir, path)
            proxy.unlink(missing_ok=True)
            playback_cache_path(output_dir, path).unlink(missing_ok=True)
            proxy.with_suffix(".failed").unlink(missing_ok=True)
            frames = scrub_frames_dir(output_dir, path)
            shutil.rmtree(frames, ignore_errors=True)
            frames.with_suffix(".failed").unlink(missing_ok=True)
            removed += 1
    for proxy in (output_dir / ".trickplay-cache").glob("**/*.mp4"):
        source = output_dir / proxy.relative_to(output_dir / ".trickplay-cache")
        if not source.is_file() and not source.with_suffix(".mkv").is_file():
            proxy.unlink(missing_ok=True)
    for failure in (output_dir / ".trickplay-cache").glob("**/*.failed"):
        source = output_dir / failure.relative_to(output_dir / ".trickplay-cache").with_suffix(".mp4")
        if not source.is_file() and not source.with_suffix(".mkv").is_file():
            failure.unlink(missing_ok=True)
    for cached in (output_dir / '.playback-cache').glob('**/*.mp4'):
        source = output_dir / cached.relative_to(output_dir / '.playback-cache')
        if not source.is_file() and not source.with_suffix('.mkv').is_file():
            cached.unlink(missing_ok=True)
    frame_root = output_dir / ".scrub-frames"
    for manifest in frame_root.glob("**/manifest.json"):
        source = output_dir / manifest.parent.relative_to(frame_root).with_suffix(".mp4")
        if not source.is_file() and not source.with_suffix(".mkv").is_file():
            shutil.rmtree(manifest.parent, ignore_errors=True)
    for failure in frame_root.glob("**/*.failed"):
        source = output_dir / failure.relative_to(frame_root).with_suffix(".mp4")
        if not source.is_file() and not source.with_suffix(".mkv").is_file():
            failure.unlink(missing_ok=True)
    return removed


def enforce_storage_limit(output_dir: Path, limit_gb: int) -> int:
    files = sorted(recording_files(output_dir), key=lambda path: path.stat().st_mtime)
    def assets_size(path: Path) -> int:
        proxy = trickplay_path(output_dir, path)
        playback = playback_cache_path(output_dir, path)
        frames = scrub_frames_dir(output_dir, path)
        return ((proxy.stat().st_size if proxy.is_file() else 0)
                + (playback.stat().st_size if playback.is_file() else 0)
                + sum(frame.stat().st_size for frame in frames.glob("*.jpg")))
    total = sum(path.stat().st_size + assets_size(path) for path in files)
    removed = 0
    limit = max(1, limit_gb) * 1024**3
    for path in files:
        if total <= limit: break
        size = path.stat().st_size
        path.unlink()
        proxy = trickplay_path(output_dir, path)
        proxy_size = assets_size(path)
        proxy.unlink(missing_ok=True)
        playback_cache_path(output_dir, path).unlink(missing_ok=True)
        proxy.with_suffix(".failed").unlink(missing_ok=True)
        frames = scrub_frames_dir(output_dir, path)
        shutil.rmtree(frames, ignore_errors=True)
        frames.with_suffix(".failed").unlink(missing_ok=True)
        total -= size + proxy_size
        removed += 1
    return removed


def run(config: RecorderConfig, cleanup_interval: int = 300) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    last_cleanup = 0.0
    while True:
        settings_path = config.output_dir / "nvr-settings.json"
        try:
            settings = json.loads(settings_path.read_text())
            config = RecorderConfig(
                config.camera_id,
                config.stream_url,
                config.output_dir,
                int(settings.get("segment_seconds", config.segment_seconds)),
                int(settings.get("retention_hours", config.retention_hours)),
                int(settings.get("storage_limit_gb", config.storage_limit_gb)),
            )
        except (OSError, ValueError, TypeError):
            settings = {}
        now = time.monotonic()
        if now - last_cleanup >= cleanup_interval:
            prune_recordings(config.output_dir, config.retention_hours)
            enforce_storage_limit(config.output_dir, config.storage_limit_gb)
            last_cleanup = now
        if not settings.get("recording_enabled", True) or settings.get("camera_recording", {}).get(config.camera_id, True) is False:
            time.sleep(min(cleanup_interval, 5))
            continue
        config.output_dir.joinpath(config.camera_id).mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(config.ffmpeg_command())
        restart = False
        while process.poll() is None:
            time.sleep(2)
            try:
                current = json.loads(settings_path.read_text())
            except (OSError, ValueError, TypeError):
                current = {}
            segment_seconds = int(current.get("segment_seconds", config.segment_seconds))
            if (
                not current.get("recording_enabled", True)
                or current.get("camera_recording", {}).get(config.camera_id, True) is False
                or segment_seconds != config.segment_seconds
            ):
                process.terminate()
                restart = True
                break
        return_code = process.wait()
        if restart:
            continue
        print(f"Recorder for {config.camera_id} exited with {return_code}; restarting", flush=True)
        time.sleep(3)


def recording_schedule_active(schedule: dict, now: datetime | None = None) -> bool:
    """Evaluate a daily local-time window, including windows across midnight."""
    if schedule.get("mode", "always") == "always":
        return True
    try:
        zone = ZoneInfo(schedule["timezone"])
        now = (now or datetime.now(timezone.utc)).astimezone(zone)
        start = int(schedule["start"][:2]) * 60 + int(schedule["start"][3:])
        end = int(schedule["end"][:2]) * 60 + int(schedule["end"][3:])
        minute = now.hour * 60 + now.minute
        return start <= minute < end if start < end else minute >= start or minute < end
    except (KeyError, ValueError, TypeError, ZoneInfoNotFoundError):
        return False


def recording_space_available(output_dir: Path, reserve_gb: int) -> bool:
    """Stop recording before consuming the configured free-space reserve."""
    try:
        return shutil.disk_usage(output_dir).free > max(0, reserve_gb) * 1024**3
    except OSError:
        return False


def next_scrub_frame_source(output_dir: Path, *, oldest_first: bool, now: float | None = None) -> Path | None:
    """Choose a closed segment needing a preview, including older footage."""
    now = time.time() if now is None else now
    candidates = []
    for source in recording_files(output_dir):
        try:
            modified = source.stat().st_mtime
            if now - modified >= 8:
                candidates.append((modified, source))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=not oldest_first)
    for _, source in candidates:
        try:
            frames = scrub_frames_dir(output_dir, source)
            if scrub_frames_ready(source, frames):
                continue
            failure = frames.with_suffix('.failed')
            if failure.is_file() and now - failure.stat().st_mtime < SCRUB_ATLAS_RETRY_SECONDS:
                continue
            return source
        except OSError:
            # Retention can remove a segment while the index is being scanned.
            continue
    return None


class RecorderSupervisor:
    """Keep one stream-copy recorder running for each enabled online camera."""

    def __init__(self, store: Store, output_dir: Path, stream_url_template: str = "rtsp://media:8554/{camera_id}", poll_seconds: int = 5) -> None:
        self.store = store
        self.output_dir = output_dir
        self.stream_url_template = stream_url_template
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._processes: dict[str, tuple[subprocess.Popen, tuple[int, int, int], str]] = {}
        self._thread: threading.Thread | None = None
        self._trickplay_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cctv-trickplay")
        self._trickplay_job = None
        self._scrub_oldest_first = False

    def start(self) -> None:
        if not shutil.which("ffmpeg"):
            print("FFmpeg is missing; NVR recording is disabled", flush=True)
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="cctv-recorder-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._trickplay_pool.shutdown(wait=False, cancel_futures=True)

    def _settings(self) -> dict:
        try:
            return json.loads((self.output_dir / "nvr-settings.json").read_text())
        except (OSError, ValueError, TypeError):
            return {}

    def _stop_recorder(self, camera_id: str) -> None:
        recorder = self._processes.pop(camera_id, None)
        if not recorder:
            return
        process = recorder[0]
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _run(self) -> None:
        last_cleanup = 0.0
        while not self._stop.is_set():
            try:
                settings = self._settings()
                retention = int(settings.get("retention_hours", 24))
                segment_seconds = int(settings.get("segment_seconds", 30))
                storage_limit_gb = int(settings.get("storage_limit_gb", 500))
                recording_enabled = settings.get("recording_enabled", True)
                space_available = recording_space_available(self.output_dir, int(settings.get("min_free_gb", 5)))
                per_camera = settings.get("camera_recording", {})
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                desired: dict[str, tuple[int, int, int]] = {}
                schedule_active = recording_schedule_active(settings.get("recording_schedule", {}))
                if recording_enabled and space_available:
                    for camera in self.store.devices("camera"):
                        camera_schedule = settings.get("camera_schedules", {}).get(camera["id"])
                        is_scheduled = recording_schedule_active(camera_schedule) if camera_schedule else schedule_active
                        if is_scheduled and camera.get("status") == "online" and per_camera.get(camera["id"], True):
                            desired[camera["id"]] = (segment_seconds, retention, storage_limit_gb)

                for camera_id, (process, current_settings, started_day) in list(self._processes.items()):
                    if camera_id not in desired or current_settings != desired[camera_id] or started_day != today or process.poll() is not None:
                        self._stop_recorder(camera_id)

                for camera_id, config_key in desired.items():
                    if camera_id in self._processes:
                        continue
                    camera_dir = self.output_dir / camera_id
                    (camera_dir / today).mkdir(parents=True, exist_ok=True)
                    config = RecorderConfig(camera_id, self.stream_url_template.format(camera_id=camera_id), self.output_dir, *config_key)
                    process = subprocess.Popen(config.ffmpeg_command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._processes[camera_id] = (process, config_key, today)
                    print(f"NVR recorder started for {camera_id} (pid {process.pid})", flush=True)

                # Prepare one closed segment at a time without blocking the
                # stream-copy recorder or opening another camera connection.
                if space_available and (not self._trickplay_job or self._trickplay_job.done()):
                    source = next_scrub_frame_source(self.output_dir, oldest_first=self._scrub_oldest_first)
                    if source:
                        fps = SCRUB_BACKFILL_FPS if self._scrub_oldest_first else SCRUB_ATLAS_FPS
                        self._scrub_oldest_first = not self._scrub_oldest_first
                        self._trickplay_job = self._trickplay_pool.submit(
                            prepare_scrub_frames, source, scrub_frames_dir(self.output_dir, source), fps)

                if time.monotonic() - last_cleanup >= 300:
                    prune_recordings(self.output_dir, retention)
                    enforce_storage_limit(self.output_dir, storage_limit_gb)
                    last_cleanup = time.monotonic()
            except Exception as exc:
                print(f"NVR recorder supervisor error: {exc}", flush=True)
            self._stop.wait(self.poll_seconds)

        for camera_id in list(self._processes):
            self._stop_recorder(camera_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pi CCTV V1 centralized recorder")
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--stream-url", required=True)
    parser.add_argument("--output-dir", default="recordings")
    parser.add_argument("--segment-seconds", type=int, default=30)
    parser.add_argument("--retention-hours", type=int, default=24)
    parser.add_argument("--storage-limit-gb", type=int, default=500)
    args = parser.parse_args()
    if args.segment_seconds < 10 or args.retention_hours < 1 or args.storage_limit_gb < 1:
        parser.error("segment-seconds, retention-hours, and storage-limit-gb must be positive (segments at least 10)")
    run(RecorderConfig(args.camera_id, args.stream_url, Path(args.output_dir), args.segment_seconds, args.retention_hours, args.storage_limit_gb))


if __name__ == "__main__":
    main()
