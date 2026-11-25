"""Burn the camera clock into raw frames, preferring a working Pi encoder."""

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def encoder_options(args, hardware):
    options = ["-c:v", "h264_v4l2m2m" if hardware else "libx264"]
    if not hardware:
        options += ["-preset", "ultrafast", "-tune", "zerolatency"]
    # FFmpeg's V4L2 encoder accepts numeric H.264 profile IDs, whereas
    # libx264 accepts the named profiles used by camera settings.
    profile = {"baseline": "66", "main": "77", "high": "100"}[args.profile] if hardware else args.profile
    return options + ["-profile:v", profile, "-b:v", str(args.bitrate), "-g", str(args.gop)]


def hardware_available(args):
    # Test the actual requested format and encoder, not merely whether FFmpeg
    # lists a wrapper: the Pi may lack the device or permission to open it.
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               f"color=size={args.size}:rate={args.fps}", "-frames:v", "2", "-an"]
    command += encoder_options(args, True) + ["-f", "h264", "-y", "/dev/null"]
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def command(args, hardware):
    return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-f", "rawvideo",
            "-pixel_format", "yuv420p", "-video_size", args.size, "-framerate", str(args.fps),
            "-i", "pipe:0", "-vf", args.filter, "-an"] + encoder_options(args, hardware) + ["-f", "h264", "pipe:1"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", required=True)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--bitrate", type=int, required=True)
    parser.add_argument("--gop", type=int, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--filter", required=True)
    args = parser.parse_args()
    signature = f"{args.size}:{args.fps}:{args.bitrate}:{args.gop}:{args.profile}"
    digest = hashlib.sha256(signature.encode()).hexdigest()[:16]
    failure_marker = Path(tempfile.gettempdir()) / f"pi-cctv-encoder-failed-{os.getuid()}-{digest}"
    hardware = not failure_marker.exists() and hardware_available(args)
    print("Camera timestamp encoder: " + ("hardware H.264" if hardware else "software H.264 (hardware unavailable)"), file=sys.stderr, flush=True)
    result = subprocess.run(command(args, hardware), check=False).returncode
    if hardware and result:
        # Restart the entire camera pipeline before switching encoders. The
        # failed encoder may have stopped halfway through a raw frame, so its
        # remaining input cannot safely be passed to a fresh decoder.
        print("Hardware timestamp encoder failed; next camera restart will use software H.264", file=sys.stderr, flush=True)
        try:
            failure_marker.touch(mode=0o600)
        except OSError as error:
            print(f"Unable to record encoder failure: {error}", file=sys.stderr, flush=True)
    return result


if __name__ == "__main__":
    sys.exit(main())
