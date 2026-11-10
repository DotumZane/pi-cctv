"""GStreamer monitor player with an on-demand, non-disruptive stats overlay."""

from __future__ import annotations

import argparse
import json
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path


OVERLAY_SECONDS = 60


def _format_rate(bits_per_second: float) -> str:
    if bits_per_second >= 1_000_000:
        return f"{bits_per_second / 1_000_000:.1f} Mbps"
    if bits_per_second >= 1_000:
        return f"{bits_per_second / 1_000:.0f} Kbps"
    return f"{bits_per_second:.0f} bps"


def _fallback_to_gst_launch(pipeline: str) -> int:
    """Keep the monitor video working when optional Python GI bindings are absent."""
    print("Python GStreamer bindings are missing; running video without the stats overlay.", flush=True)
    child = subprocess.Popen(["gst-launch-1.0", "-e", *shlex.split(pipeline)])

    def forward(signum: int, _frame: object) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    return child.wait()


class MonitorPlayer:
    def __init__(self, pipeline_description: str, control_file: str) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst

        self.GLib = GLib
        self.Gst = Gst
        Gst.init(None)
        self.pipeline = Gst.parse_launch(pipeline_description)
        self.overlay = self.pipeline.get_by_name("cctv_stats")
        if self.overlay is None:
            raise RuntimeError("monitor pipeline is missing its stats text overlay")
        self.control_file = Path(control_file)
        self.loop = GLib.MainLoop()
        self.lock = threading.Lock()
        self.request_id = self._read_request_id()
        self.visible_until = 0.0
        self.bytes_in_window = 0
        self.window_started = time.monotonic()
        self.bitrate = 0.0
        self.frames_in_window = 0
        self.last_fps_update = time.monotonic()
        self.fps = 0.0
        self.width = 0
        self.height = 0
        self.age_seconds: float | None = None
        self.started_at = time.monotonic()
        self.rtspsrcs = self._find_elements("rtspsrc")
        for source in self.rtspsrcs:
            source.connect("pad-added", self._on_rtp_pad)
        tee = self.pipeline.get_by_name("monitor_out")
        if tee:
            sink_pad = tee.get_static_pad("sink")
            if sink_pad:
                sink_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_video_buffer)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        GLib.timeout_add(200, self._read_control)
        GLib.timeout_add(1000, self._update_overlay)

    def _find_elements(self, factory: str) -> list:
        elements = []
        iterator = self.pipeline.iterate_recurse()
        while True:
            result, element = iterator.next()
            if result == self.Gst.IteratorResult.DONE:
                break
            if result == self.Gst.IteratorResult.OK and element.get_factory():
                if element.get_factory().get_name() == factory:
                    elements.append(element)
            elif result == self.Gst.IteratorResult.RESYNC:
                iterator.resync()
        return elements

    def _read_request_id(self) -> str:
        try:
            data = json.loads(self.control_file.read_text())
            return str(data.get("request_id", ""))[:100]
        except (OSError, ValueError, TypeError):
            return ""

    def _on_rtp_pad(self, _source, pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps or caps.get_size() == 0:
            return
        structure = caps.get_structure(0)
        if structure.get_name() != "application/x-rtp" or structure.get_string("media") != "video":
            return
        pad.add_probe(self.Gst.PadProbeType.BUFFER, self._on_rtp_buffer)

    def _on_rtp_buffer(self, _pad, info):
        buffer = info.get_buffer()
        if buffer:
            with self.lock:
                self.bytes_in_window += buffer.get_size()
        return self.Gst.PadProbeReturn.OK

    def _on_video_buffer(self, pad, info):
        buffer = info.get_buffer()
        if not buffer:
            return self.Gst.PadProbeReturn.OK
        with self.lock:
            self.frames_in_window += 1
            caps = pad.get_current_caps()
            if caps and caps.get_size():
                structure = caps.get_structure(0)
                width_ok, width = structure.get_int("width")
                height_ok, height = structure.get_int("height")
                if width_ok and height_ok:
                    self.width, self.height = width, height
            if buffer.pts != self.Gst.CLOCK_TIME_NONE:
                clock = self.pipeline.get_clock()
                base_time = self.pipeline.get_base_time()
                if clock and base_time != self.Gst.CLOCK_TIME_NONE:
                    running_time = clock.get_time() - base_time
                    age = (running_time - buffer.pts) / self.Gst.SECOND
                    self.age_seconds = age if 0 <= age <= 10 else None
        return self.Gst.PadProbeReturn.OK

    def _on_bus_message(self, _bus, message) -> None:
        if message.type == self.Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            print(f"Monitor media error: {error}; {debug or ''}", flush=True)
            self.loop.quit()
        elif message.type == self.Gst.MessageType.EOS:
            self.loop.quit()

    def _read_control(self) -> bool:
        request_id = self._read_request_id()
        if request_id and request_id != self.request_id:
            self.request_id = request_id
            self.visible_until = time.monotonic() + OVERLAY_SECONDS
            print("Monitor stats overlay enabled for 60 seconds", flush=True)
        return True

    def _update_overlay(self) -> bool:
        now = time.monotonic()
        with self.lock:
            elapsed = max(0.001, now - self.window_started)
            self.bitrate = self.bytes_in_window * 8 / elapsed
            self.bytes_in_window = 0
            self.window_started = now
            fps_elapsed = max(0.001, now - self.last_fps_update)
            self.fps = self.frames_in_window / fps_elapsed
            self.frames_in_window = 0
            self.last_fps_update = now
            width, height, age, bitrate, fps = self.width, self.height, self.age_seconds, self.bitrate, self.fps
        visible = now < self.visible_until
        self.overlay.set_property("silent", not visible)
        if visible:
            resolution = f"{width}×{height}" if width and height else "measuring"
            age_text = f"{age * 1000:.0f} ms" if age is not None else "measuring"
            uptime = int(now - self.started_at)
            stats = (
                f"STATS FOR NERDS  ·  {resolution} @ {fps:.1f} fps\n"
                f"Incoming bitrate  {_format_rate(bitrate)}\n"
                f"Pipeline age estimate  {age_text}  ·  Stream uptime  {uptime}s"
            )
            self.overlay.set_property("text", stats)
        return True

    def run(self) -> int:
        result = self.pipeline.set_state(self.Gst.State.PLAYING)
        if result == self.Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer could not start the monitor pipeline")
        try:
            self.loop.run()
        finally:
            self.pipeline.set_state(self.Gst.State.NULL)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", required=True, help="GStreamer launch description without gst-launch-1.0")
    parser.add_argument("--control-file", required=True, help="Agent-written file used to request the timed overlay")
    args = parser.parse_args()
    try:
        player = MonitorPlayer(args.pipeline, args.control_file)
    except (ImportError, ValueError):
        return _fallback_to_gst_launch(args.pipeline)
    return player.run()


if __name__ == "__main__":
    raise SystemExit(main())
