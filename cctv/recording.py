"""Segmented, stream-copy recording helpers for the centralized recorder."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class RecorderConfig:
    camera_id: str
    stream_url: str
    output_dir: Path
    segment_seconds: int = 900
    retention_hours: int = 24
    storage_limit_gb: int = 500

    def ffmpeg_command(self) -> list[str]:
        output = self.output_dir / self.camera_id / "%Y-%m-%d" / "%H-%M-%S.mkv"
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-rtsp_transport", "tcp", "-fflags", "nobuffer", "-i", self.stream_url,
            "-map", "0:v:0", "-c", "copy", "-f", "segment",
            "-segment_time", str(self.segment_seconds), "-strftime", "1",
            "-reset_timestamps", "1", str(output),
        ]

    def prune_before(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(hours=self.retention_hours)


def prune_recordings(output_dir: Path, retention_hours: int, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
    removed = 0
    for path in output_dir.glob("**/*.mkv"):
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()
            removed += 1
    return removed


def enforce_storage_limit(output_dir: Path, limit_gb: int) -> int:
    files = sorted(output_dir.glob("**/*.mkv"), key=lambda path: path.stat().st_mtime)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Pi CCTV V1 centralized recorder")
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--stream-url", required=True)
    parser.add_argument("--output-dir", default="recordings")
    parser.add_argument("--segment-seconds", type=int, default=900)
    parser.add_argument("--retention-hours", type=int, default=24)
    parser.add_argument("--storage-limit-gb", type=int, default=500)
    args = parser.parse_args()
    if args.segment_seconds < 10 or args.retention_hours < 1 or args.storage_limit_gb < 1:
        parser.error("segment-seconds, retention-hours, and storage-limit-gb must be positive (segments at least 10)")
    run(RecorderConfig(args.camera_id, args.stream_url, Path(args.output_dir), args.segment_seconds, args.retention_hours, args.storage_limit_gb))


if __name__ == "__main__":
    main()
