"""Segmented, stream-copy recording helpers for the centralized recorder."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store


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
            "-segment_time", str(self.segment_seconds), "-strftime", "1",
            "-reset_timestamps", "1", str(output),
        ]

    def prune_before(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(hours=self.retention_hours)


def prune_recordings(output_dir: Path, retention_hours: int, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
    removed = 0
    for path in (*output_dir.glob("**/*.mkv"), *output_dir.glob("**/*.mp4")):
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()
            removed += 1
    return removed


def enforce_storage_limit(output_dir: Path, limit_gb: int) -> int:
    files = sorted((*output_dir.glob("**/*.mkv"), *output_dir.glob("**/*.mp4")), key=lambda path: path.stat().st_mtime)
    total = sum(path.stat().st_size for path in files)
    removed = 0
    limit = max(1, limit_gb) * 1024**3
    for path in files:
        if total <= limit: break
        size = path.stat().st_size
        path.unlink()
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
                per_camera = settings.get("camera_recording", {})
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                desired: dict[str, tuple[int, int, int]] = {}
                if recording_enabled:
                    for camera in self.store.devices("camera"):
                        if camera.get("status") == "online" and per_camera.get(camera["id"], True):
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
