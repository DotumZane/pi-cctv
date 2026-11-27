"""Segmented, stream-copy recording helpers for the centralized recorder."""

from __future__ import annotations

import argparse
import json
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

# Caches of the scrub previews used before the preview track. Nothing reads
# them any more; they are deleted on the next pruning pass.
LEGACY_PREVIEW_CACHES = (".scrub-frames", ".trickplay-cache")


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


def playback_cache_path(root: Path, recording: Path) -> Path:
    relative = recording.relative_to(root)
    return root / '.playback-cache' / relative.parent / f'{relative.stem}.mp4'


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


def remove_legacy_preview_caches(output_dir: Path) -> None:
    for name in LEGACY_PREVIEW_CACHES:
        shutil.rmtree(output_dir / name, ignore_errors=True)


def prune_recordings(output_dir: Path, retention_hours: int, now: datetime | None = None) -> int:
    from .preview_track import prune_previews, remove_preview
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
    removed = 0
    for path in recording_files(output_dir):
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()
            playback_cache_path(output_dir, path).unlink(missing_ok=True)
            remove_preview(output_dir, path)
            removed += 1
    prune_previews(output_dir, retention_hours, (now or datetime.now(timezone.utc)).timestamp())
    for cached in (output_dir / '.playback-cache').glob('**/*.mp4'):
        source = output_dir / cached.relative_to(output_dir / '.playback-cache')
        if not source.is_file() and not source.with_suffix('.mkv').is_file():
            cached.unlink(missing_ok=True)
    remove_legacy_preview_caches(output_dir)
    return removed


def enforce_storage_limit(output_dir: Path, limit_gb: int) -> int:
    from .preview_track import preview_size, remove_preview
    files = sorted(recording_files(output_dir), key=lambda path: path.stat().st_mtime)
    def assets_size(path: Path) -> int:
        playback = playback_cache_path(output_dir, path)
        return (playback.stat().st_size if playback.is_file() else 0) + preview_size(output_dir, path)
    total = sum(path.stat().st_size + assets_size(path) for path in files)
    removed = 0
    limit = max(1, limit_gb) * 1024**3
    for path in files:
        if total <= limit: break
        size = path.stat().st_size + assets_size(path)
        path.unlink()
        playback_cache_path(output_dir, path).unlink(missing_ok=True)
        remove_preview(output_dir, path)
        total -= size
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


class RecorderSupervisor:
    """Keep one stream-copy recorder running for each enabled online camera."""

    def __init__(self, store: Store, output_dir: Path, stream_url_template: str = "rtsp://media:8554/{camera_id}", poll_seconds: int = 5) -> None:
        self.store = store
        self.output_dir = output_dir
        self.stream_url_template = stream_url_template
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._preview_wake = threading.Event()
        self._processes: dict[str, tuple[subprocess.Popen, tuple[int, int, int], str]] = {}
        self._thread: threading.Thread | None = None
        self._preview_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cctv-preview-index")
        self._preview_job = None
        self._lock = threading.Lock()
        self._started_at: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}

    def start(self) -> None:
        if not shutil.which("ffmpeg"):
            print("FFmpeg is missing; NVR recording is disabled", flush=True)
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="cctv-recorder-supervisor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 6.0) -> None:
        """Stop every recorder so FFmpeg can finish its current segment.

        Docker allows the container a short grace period after SIGTERM, so
        all recorders are signalled together rather than one at a time.
        """
        self._stop.set()
        self._preview_wake.set()
        self._stop_recorders(list(self._processes), timeout)
        if self._thread:
            self._thread.join(timeout=2)
        self._preview_pool.shutdown(wait=False, cancel_futures=True)

    def _settings(self) -> dict:
        try:
            return json.loads((self.output_dir / "nvr-settings.json").read_text())
        except (OSError, ValueError, TypeError):
            return {}

    def _stop_recorder(self, camera_id: str) -> None:
        self._stop_recorders([camera_id], 8)

    def _stop_recorders(self, camera_ids: list[str], timeout: float) -> None:
        # SIGTERM makes FFmpeg write the open segment's index (moov) and
        # exit; a killed FFmpeg leaves that segment unplayable.
        with self._lock:
            recorders = [self._processes.pop(camera_id) for camera_id in camera_ids if camera_id in self._processes]
        running = [recorder[0] for recorder in recorders if recorder[0].poll() is None]
        for process in running:
            process.terminate()
        deadline = time.monotonic() + timeout
        for process in running:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _preview_finished(self, future) -> None:
        try:
            # A busy shared worker is retried on the normal poll, avoiding a
            # tight loop when a requested preview owns the decoder slot.
            if future.result() is not False:
                self._preview_wake.set()
        except Exception:
            pass

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

                now = time.monotonic()
                for camera_id, (process, current_settings, started_day) in list(self._processes.items()):
                    if process.poll() is not None:
                        # A recorder that keeps failing (camera stream not
                        # published yet, network down) backs off instead of
                        # being relaunched on every loop.
                        ran = now - self._started_at.get(camera_id, now)
                        failures = self._failures.get(camera_id, 0) + 1 if ran < 60 else 1
                        delay = min(60, 2 ** min(failures, 6))
                        self._failures[camera_id] = failures
                        self._retry_at[camera_id] = now + delay
                        print(f"NVR recorder for {camera_id} exited with code {process.returncode} after {ran:.0f}s; retrying in {delay}s", flush=True)
                        self._stop_recorder(camera_id)
                    elif camera_id not in desired or current_settings != desired[camera_id] or started_day != today:
                        self._stop_recorder(camera_id)
                    elif now - self._started_at.get(camera_id, now) >= 60:
                        self._failures.pop(camera_id, None)

                for camera_id, config_key in desired.items():
                    if camera_id in self._processes or self._stop.is_set() or now < self._retry_at.get(camera_id, 0):
                        continue
                    camera_dir = self.output_dir / camera_id
                    (camera_dir / today).mkdir(parents=True, exist_ok=True)
                    config = RecorderConfig(camera_id, self.stream_url_template.format(camera_id=camera_id), self.output_dir, *config_key)
                    process = subprocess.Popen(config.ffmpeg_command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    with self._lock:
                        self._processes[camera_id] = (process, config_key, today)
                    self._started_at[camera_id] = time.monotonic()
                    print(f"NVR recorder started for {camera_id} (pid {process.pid})", flush=True)

                # Index one closed segment at a time into the preview track,
                # newest first, without touching the stream-copy recorders or
                # opening another camera connection. The worker thread is the
                # persistent indexer; FFmpeg runs once per closed segment.
                if space_available and (not self._preview_job or self._preview_job.done()):
                    from .preview_track import build_segment_preview, next_preview_source
                    source = next_preview_source(self.output_dir)
                    if source:
                        self._preview_job = self._preview_pool.submit(build_segment_preview, self.output_dir, source)
                        self._preview_job.add_done_callback(self._preview_finished)

                if time.monotonic() - last_cleanup >= 300:
                    prune_recordings(self.output_dir, retention)
                    enforce_storage_limit(self.output_dir, storage_limit_gb)
                    last_cleanup = time.monotonic()
            except Exception as exc:
                print(f"NVR recorder supervisor error: {exc}", flush=True)
            self._preview_wake.wait(self.poll_seconds)
            self._preview_wake.clear()

        self._stop_recorders(list(self._processes), 6)


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
