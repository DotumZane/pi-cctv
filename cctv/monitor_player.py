"""GStreamer monitor player with an on-demand, non-disruptive stats overlay."""

from __future__ import annotations

import argparse
import json
import shlex
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path


OVERLAY_SECONDS = 60
OFFLINE_AFTER_SECONDS = 3.0
RECONNECT_AFTER_SECONDS = 8.0


def _format_rate(bits_per_second: float) -> str:
    if bits_per_second >= 1_000_000:
        return f"{bits_per_second / 1_000_000:.1f} Mbps"
    if bits_per_second >= 1_000:
        return f"{bits_per_second / 1_000:.0f} Kbps"
    return f"{bits_per_second:.0f} bps"


def _fallback_to_gst_launch(pipeline: str) -> int:
    """Keep the monitor video working when optional Python GI bindings are absent."""
    if "cctv_selector_" in pipeline:
        # The source selection and recovery loop require GI. Running this as a
        # plain gst-launch pipeline would leave the offline pad selected forever.
        print("Monitor requires python3-gi to switch camera video and offline slates.", flush=True)
        return 1
    print("Python GStreamer bindings are missing; running video without the stats overlay.", flush=True)
    child = subprocess.Popen(["gst-launch-1.0", "-e", *shlex.split(pipeline)])

    def forward(signum: int, _frame: object) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    return child.wait()


class MonitorPlayer:
    def __init__(self, pipeline_description: str, control_file: str, reconnect_seconds: int = 5) -> None:
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
        self.last_video_buffer_at: float | None = None
        self.started_at = time.monotonic()
        self.reconnect_seconds = max(1, reconnect_seconds)
        self.splash_selector = self.pipeline.get_by_name("cctv_splash_selector")
        self.splash_until = self.started_at + 4.0
        if self.splash_selector is not None:
            self.splash_selector.set_property("active-pad", self.splash_selector.get_static_pad("sink_1"))
        self.cameras = {}
        index = 0
        while True:
            selector = self.pipeline.get_by_name(f"cctv_selector_{index}")
            if selector is None:
                break
            live = self.pipeline.get_by_name(f"cctv_live_{index}")
            offline = self.pipeline.get_by_name(f"cctv_offline_{index}")
            source = self.pipeline.get_by_name(f"cctv_source_{index}")
            if live is None or offline is None or source is None:
                raise RuntimeError(f"monitor camera branch {index} is incomplete")
            heading = offline.get_property("text").split(" — Camera offline", 1)[0]
            self.cameras[index] = {
                "selector": selector, "offline": offline, "source": source,
                "heading": heading, "last_frame": None, "last_received": None,
                "showing_live": False, "restarting": False, "last_retry_at": self.started_at,
                "last_slate_text": None,
            }
            pad = live.get_static_pad("src")
            pad.add_probe(Gst.PadProbeType.BUFFER, self._on_camera_buffer, index)
            # The standby pad is linked first. It is the only source allowed on
            # screen until a decoded frame from this camera actually arrives.
            selector.set_property("active-pad", selector.get_static_pad("sink_0"))
            index += 1
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
        GLib.timeout_add(500, self._check_cameras)
        if self.splash_selector is not None:
            GLib.timeout_add(250, self._hide_splash)

    def _hide_splash(self) -> bool:
        if self.splash_selector is None or time.monotonic() < self.splash_until:
            return True
        self.splash_selector.set_property("active-pad", self.splash_selector.get_static_pad("sink_0"))
        print("Monitor splash finished; showing normal display output", flush=True)
        return False

    def _on_camera_buffer(self, _pad, info, index):
        if info.get_buffer():
            with self.lock:
                camera = self.cameras[index]
                camera["last_frame"] = time.monotonic()
                camera["last_received"] = datetime.now().astimezone()
                should_switch = not camera["showing_live"]
            if should_switch:
                self.GLib.idle_add(self._show_live, index)
        return self.Gst.PadProbeReturn.OK

    def _show_live(self, index: int) -> bool:
        camera = self.cameras[index]
        if not camera["showing_live"] and not camera["restarting"]:
            camera["selector"].set_property("active-pad", camera["selector"].get_static_pad("sink_1"))
            camera["showing_live"] = True
            print(f"Camera {camera['heading']} delivering video", flush=True)
        return False

    def _show_offline(self, index: int) -> None:
        camera = self.cameras[index]
        with self.lock:
            received = camera["last_received"]
        last_text = received.strftime("%Y-%m-%d %H:%M:%S %Z") if received else "never"
        slate_text = f"{camera['heading']}\nCamera offline — reconnecting\nLast received: {last_text}"
        if camera["last_slate_text"] != slate_text:
            camera["offline"].set_property("text", slate_text)
            camera["last_slate_text"] = slate_text
        if camera["showing_live"]:
            camera["selector"].set_property("active-pad", camera["selector"].get_static_pad("sink_0"))
            camera["showing_live"] = False
            print(f"Camera {camera['heading']} offline; showing local slate", flush=True)

    def _check_cameras(self) -> bool:
        now = time.monotonic()
        for index, camera in self.cameras.items():
            with self.lock:
                last_frame = camera["last_frame"]
            if last_frame is None or now - last_frame > OFFLINE_AFTER_SECONDS:
                self._show_offline(index)
            retry_anchor = max(last_frame or 0, camera["last_retry_at"])
            if not camera["restarting"] and now - retry_anchor > RECONNECT_AFTER_SECONDS:
                self._restart_source(index)
        return True

    def _restart_source(self, index: int) -> None:
        camera = self.cameras[index]
        camera["restarting"] = True
        camera["last_retry_at"] = time.monotonic()
        self._show_offline(index)
        camera["source"].set_state(self.Gst.State.NULL)
        print(f"Reconnecting camera {camera['heading']}", flush=True)
        self.GLib.timeout_add(self.reconnect_seconds * 1000, self._resume_source, index)

    def _resume_source(self, index: int) -> bool:
        camera = self.cameras[index]
        with self.lock:
            camera["last_frame"] = None
        camera["source"].set_state(self.Gst.State.PLAYING)
        camera["last_retry_at"] = time.monotonic()
        camera["restarting"] = False
        return False

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
            self.last_video_buffer_at = time.monotonic()
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
            element = message.src
            while element is not None:
                for index, camera in self.cameras.items():
                    if element == camera["source"]:
                        if not camera["restarting"]:
                            self._restart_source(index)
                        return
                element = element.get_parent()
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
            if age is None and self.last_video_buffer_at is not None:
                # Some live sources expose PTS values in a different clock domain
                # than the pipeline clock. Use the most recent buffer arrival as a
                # safe low-latency fallback instead of leaving the overlay stuck.
                age = max(0.0, min(10.0, now - self.last_video_buffer_at))
        visible = now < self.visible_until
        self.overlay.set_property("silent", not visible)
        if visible:
            resolution = f"{width}×{height}" if width and height else "measuring"
            age_text = f"{age * 1000:.0f} ms" if age is not None else "unavailable"
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
    parser.add_argument("--reconnect-seconds", type=int, default=5)
    args = parser.parse_args()
    try:
        player = MonitorPlayer(args.pipeline, args.control_file, args.reconnect_seconds)
    except (ImportError, ValueError):
        return _fallback_to_gst_launch(args.pipeline)
    return player.run()


if __name__ == "__main__":
    raise SystemExit(main())
