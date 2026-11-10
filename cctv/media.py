"""Low-latency media command builders.

The builders keep hardware-specific video work outside the management server.
They are pure functions so they can be inspected and tested on a development
machine before being run on a Raspberry Pi.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from shlex import quote


@dataclass(frozen=True)
class CameraMedia:
    publish_url: str
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bitrate: int = 6_000_000
    flip_horizontal: bool = False
    flip_vertical: bool = False
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0
    sharpness: float = 1.0
    exposure_mode: str = "auto"
    shutter_us: int = 10_000
    gain: float = 1.0
    exposure_profile: str = "normal"
    white_balance: str = "auto"
    keyframe_interval: int = 30
    h264_profile: str = "main"

    def pipeline(self) -> str:
        camera_args = [
            "rpicam-vid", "-n", "-t", "0", "--inline", "--codec", "h264", "-o", "-",
            "--width", str(self.width), "--height", str(self.height),
            "--framerate", str(self.fps), "--bitrate", str(self.bitrate),
        ]
        if self.flip_horizontal:
            camera_args.append("--hflip")
        if self.flip_vertical:
            camera_args.append("--vflip")
        if self.brightness:
            camera_args.extend(["--brightness", str(self.brightness)])
        if self.contrast != 1.0:
            camera_args.extend(["--contrast", str(self.contrast)])
        if self.saturation != 1.0:
            camera_args.extend(["--saturation", str(self.saturation)])
        if self.sharpness != 1.0:
            camera_args.extend(["--sharpness", str(self.sharpness)])
        if self.exposure_mode == "manual":
            camera_args.extend(["--shutter", str(self.shutter_us), "--gain", str(self.gain)])
        if self.exposure_profile != "normal":
            camera_args.extend(["--exposure", self.exposure_profile])
        if self.white_balance != "auto":
            camera_args.extend(["--awb", self.white_balance])
        if self.keyframe_interval:
            camera_args.extend(["--intra", str(self.keyframe_interval)])
        if self.h264_profile != "main":
            camera_args.extend(["--profile", self.h264_profile])
        camera_command = " ".join(quote(arg) for arg in camera_args)
        return (
            f"{camera_command} | gst-launch-1.0 -e "
            "fdsrc fd=0 do-timestamp=true ! h264parse config-interval=1 ! "
            f"rtspclientsink location={quote(self.publish_url)} protocols=tcp latency=0"
        )


@dataclass(frozen=True)
class MonitorMedia:
    stream_url: str
    fullscreen: bool = True
    connector_id: str | None = None
    width: int | None = None
    height: int | None = None
    scale_mode: str = "fit"
    orientation: int = 0
    show_camera_name: bool = False
    show_timestamp: bool = False
    camera_name: str = ""
    preview_id: str = "default"

    def pipeline(self) -> str:
        sink = "kmssink" + (f" connector-id={self.connector_id}" if self.connector_id else "") + " sync=false" if self.fullscreen else "autovideosink sync=false"
        filters = []
        if self.orientation in (90, 180, 270):
            filters.append(f"videoflip method=rotate-{self.orientation}")
        if self.width and self.height:
            if self.scale_mode == "fill":
                filters.append(f"aspectratiocrop aspect-ratio={self.width}/{self.height}")
            filters.extend([
                f"videoscale add-borders={'false' if self.scale_mode == 'fill' else 'true'}",
                f"video/x-raw,width={self.width},height={self.height}",
            ])
        if self.show_timestamp:
            filters.append('clockoverlay time-format="%Y-%m-%d %H:%M:%S" halignment=left valignment=top')
        if self.show_camera_name and self.camera_name:
            filters.append(f'textoverlay text="{self.camera_name}" halignment=right valignment=top')
        filter_chain = (" ! " + " ! ".join(filters)) if filters else ""
        preview_width, preview_height = _preview_size(self.width or 1920, self.height or 1080)
        preview = _preview_branch(self.preview_id, preview_width, preview_height)
        return (
            "gst-launch-1.0 -e "
            f"rtspsrc location={quote(self.stream_url)} protocols=tcp latency=0 drop-on-latency=true ! "
            "rtph264depay ! h264parse ! v4l2h264dec ! queue max-size-buffers=2 leaky=downstream ! "
            f"videoconvert{filter_chain} ! tee name=monitor_out "
            f"monitor_out. ! queue max-size-buffers=2 leaky=downstream ! {sink} "
            f"monitor_out. ! {preview}"
        )


def _preview_size(width: int, height: int, max_width: int = 640, max_height: int = 360) -> tuple[int, int]:
    scale = min(max_width / width, max_height / height, 1.0)
    out_width = max(2, int(width * scale) // 2 * 2)
    out_height = max(2, int(height * scale) // 2 * 2)
    return out_width, out_height


def _preview_branch(preview_id: str, width: int, height: int) -> str:
    key = re.sub(r"[^A-Za-z0-9_-]", "_", preview_id)[:48] or "default"
    path = quote(f"/tmp/pi-cctv-monitor-preview-{key}-%05d.jpg")
    return (
        "queue max-size-buffers=2 leaky=downstream ! videorate drop-only=true ! "
        "video/x-raw,framerate=1/5 ! videoscale add-borders=true ! "
        f"video/x-raw,width={width},height={height} ! jpegenc quality=55 ! "
        f"multifilesink location={path} max-files=1"
    )


def quad_pipeline(stream_urls: list[str], width: int = 1920, height: int = 1080, preview_id: str = "default") -> str:
    """Build a 2×2 decoder pipeline for one to four RTSP streams."""
    if not 1 <= len(stream_urls) <= 4:
        raise ValueError("a 2x2 layout requires between one and four streams")
    cell_width, cell_height = max(1, width // 2), max(1, height // 2)
    branches = []
    for index, url in enumerate(stream_urls):
        branches.append(
            f"rtspsrc location={quote(url)} protocols=tcp latency=0 drop-on-latency=true ! "
            f"rtph264depay ! h264parse ! v4l2h264dec ! videoconvert ! videoscale add-borders=true ! "
            f"video/x-raw,width={cell_width},height={cell_height} ! compositor.sink_{index}"
        )
    positions = " ".join(
        f"sink_{i}::xpos={(i % 2) * cell_width} sink_{i}::ypos={(i // 2) * cell_height}"
        for i in range(len(stream_urls))
    )
    preview_width, preview_height = _preview_size(width, height)
    return (
        "gst-launch-1.0 -e " + " ".join(branches)
        + f" compositor name=compositor {positions} ! video/x-raw,width={width},height={height} ! tee name=monitor_out "
        + "monitor_out. ! queue max-size-buffers=2 leaky=downstream ! kmssink sync=false "
        + f"monitor_out. ! {_preview_branch(preview_id, preview_width, preview_height)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a Pi CCTV media pipeline")
    subparsers = parser.add_subparsers(dest="role", required=True)
    camera = subparsers.add_parser("camera")
    camera.add_argument("publish_url")
    camera.add_argument("--width", type=int, default=1920)
    camera.add_argument("--height", type=int, default=1080)
    camera.add_argument("--fps", type=int, default=30)
    camera.add_argument("--bitrate", type=int, default=6_000_000)
    monitor = subparsers.add_parser("monitor")
    monitor.add_argument("stream_url")
    quad = subparsers.add_parser("quad")
    quad.add_argument("stream_urls", nargs="+")
    args = parser.parse_args()
    if args.role == "camera":
        print(CameraMedia(args.publish_url, args.width, args.height, args.fps, args.bitrate).pipeline())
    elif args.role == "monitor":
        print(MonitorMedia(args.stream_url).pipeline())
    else:
        if len(args.stream_urls) > 4:
            parser.error("quad accepts at most four stream URLs")
        print(quad_pipeline(args.stream_urls))


if __name__ == "__main__":
    main()
