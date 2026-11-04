"""Helpers for generating repeatable low-latency test pipelines."""

from __future__ import annotations

import argparse


def test_pattern_pipeline(publish_url: str, width: int = 1920, height: int = 1080, fps: int = 30) -> str:
    """Create a timestamped moving test pattern for end-to-end measurement."""
    return (
        "gst-launch-1.0 -e videotestsrc is-live=true pattern=ball ! "
        f"video/x-raw,width={width},height={height},framerate={fps}/1 ! "
        "timeoverlay halignment=left valignment=top ! videoconvert ! "
        "v4l2h264enc ! h264parse config-interval=1 ! rtph264pay pt=96 config-interval=1 ! "
        f"rtspclientsink location={publish_url} protocols=tcp latency=0"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a latency test-pattern pipeline")
    parser.add_argument("publish_url")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()
    print(test_pattern_pipeline(args.publish_url, args.width, args.height, args.fps))
