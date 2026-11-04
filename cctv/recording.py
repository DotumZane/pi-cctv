"""Segmented, stream-copy recording helpers for the centralized recorder."""

from __future__ import annotations

import argparse
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


def run(config: RecorderConfig, cleanup_interval: int = 300) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    last_cleanup = 0.0
    while True:
        now = time.monotonic()
        if now - last_cleanup >= cleanup_interval:
            prune_recordings(config.output_dir, config.retention_hours)
            last_cleanup = now
        config.output_dir.joinpath(config.camera_id).mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(config.ffmpeg_command())
        return_code = process.wait()
        print(f"Recorder for {config.camera_id} exited with {return_code}; restarting", flush=True)
        time.sleep(3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pi CCTV V1 centralized recorder")
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--stream-url", required=True)
    parser.add_argument("--output-dir", default="recordings")
    parser.add_argument("--segment-seconds", type=int, default=900)
    parser.add_argument("--retention-hours", type=int, default=24)
    args = parser.parse_args()
    if args.segment_seconds < 10 or args.retention_hours < 1:
        parser.error("segment-seconds must be at least 10 and retention-hours at least 1")
    run(RecorderConfig(args.camera_id, args.stream_url, Path(args.output_dir), args.segment_seconds, args.retention_hours))
