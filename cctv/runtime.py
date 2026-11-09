"""Unattended device runtime for camera and monitor Raspberry Pis."""

from __future__ import annotations

import json
import glob
import re
import subprocess
import time
import urllib.request
import os
import signal
import shutil
import socket
import platform
import datetime
import sys
from dataclasses import dataclass
from typing import Callable

from .media import CameraMedia, MonitorMedia, quad_pipeline


def system_metrics() -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    try:
        metrics["cpu_temperature_c"] = round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000, 1)
    except (OSError, ValueError):
        pass
    try:
        metrics["load_1m"] = float(open("/proc/loadavg").read().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        memory = {line.split(":", 1)[0]: int(line.split()[1]) for line in open("/proc/meminfo") if ":" in line}
        if "MemTotal" in memory and "MemAvailable" in memory:
            metrics["memory_used_percent"] = round((1 - memory["MemAvailable"] / memory["MemTotal"]) * 100, 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        metrics["uptime_seconds"] = int(float(open("/proc/uptime").read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    return metrics


def connected_displays() -> list[dict[str, str]]:
    """Return connected DRM display outputs, using stable connector names."""
    def add_mode(display: dict[str, str]) -> dict[str, str]:
        connector = display.get("id", "")
        if connector.startswith("card"):
            paths = [f"/sys/class/drm/{connector}/mode", f"/sys/class/drm/{connector}/modes"]
        else:
            paths = sorted(glob.glob(f"/sys/class/drm/card*-{connector}/mode"))
            paths += sorted(glob.glob(f"/sys/class/drm/card*-{connector}/modes"))
        for path in paths:
            try:
                lines = open(path).read().splitlines()
            except OSError:
                continue
            for line in lines:
                match = re.fullmatch(r"\s*(\d+)x(\d+)\s*", line)
                if match:
                    display["width"], display["height"] = match.groups()
                    return display
        return display

    displays = []
    try:
        modetest = subprocess.run(["modetest", "-M", "vc4", "-c"], capture_output=True, text=True, timeout=5, check=False).stdout
        for match in re.finditer(r"^\s*(\d+)\s+\d+\s+connected\s+(\S+)", modetest, re.MULTILINE):
            displays.append(add_mode({"id": match.group(2), "name": match.group(2).replace("-", " "), "connector_id": match.group(1)}))
    except (OSError, subprocess.SubprocessError):
        pass
    if displays:
        return displays
    for status_path in sorted(glob.glob("/sys/class/drm/card*-*/status")):
        try:
            if open(status_path).read().strip() != "connected":
                continue
        except OSError:
            continue
        connector = status_path.rsplit("/", 2)[-2]
        displays.append(add_mode({"id": connector, "name": connector.replace("-", " ")}))
    return displays or [{"id": "primary", "name": "Display 1"}]


def collect_diagnostics(role: str, device_id: str, media_server: str, processes: list | None = None) -> dict:
    """Collect bounded, read-only health details suitable for a dashboard report."""
    result = {"collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "device_id": device_id,
              "hostname": socket.gethostname(), "role": role, "platform": platform.platform(),
              "metrics": system_metrics(), "displays": connected_displays() if role == "monitor" else []}
    disks=[]
    for path in ("/", "/data", "/var/lib/mediamtx"):
        if os.path.exists(path):
            try:
                usage=shutil.disk_usage(path); disks.append({"path":path,"total_bytes":usage.total,"free_bytes":usage.free,"used_percent":round(100*usage.used/usage.total,1)})
            except OSError: pass
    result["storage"]=disks
    try:
        with socket.create_connection((media_server,8554),timeout=2): reachable=True
    except OSError: reachable=False
    result["network"]={"media_server":media_server,"rtsp_port_8554_reachable":reachable}
    result["media_processes"]=[{"running":p.poll() is None,"pid":p.pid} for p in (processes or [])]
    checks=[]
    for service in ("mediamtx", "pi-cctv-agent", "pi-cctv-recorder"):
        try:
            p=subprocess.run(["systemctl","is-active",service],capture_output=True,text=True,timeout=3,check=False)
            checks.append({"service":service,"active":p.stdout.strip()=="active","state":p.stdout.strip() or "unknown"})
        except (OSError,subprocess.SubprocessError): checks.append({"service":service,"active":False,"state":"unavailable"})
    result["services"]=checks
    try:
        p=subprocess.run(["journalctl","-u","pi-cctv-agent","-n","40","--no-pager","-o","short-iso"],capture_output=True,text=True,timeout=5,check=False)
        result["recent_logs"]=p.stdout[-12000:].splitlines()
    except (OSError,subprocess.SubprocessError): result["recent_logs"]=[]
    return result


def recent_logs() -> list[str]:
    try:
        result = subprocess.run(
            ["journalctl", "-u", "pi-cctv-agent", "-n", "80", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return result.stdout[-16000:].splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def http_json(url: str, method: str = "GET", payload: dict | None = None, token: str = "") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def command_for_assignment(assignment: dict, media_server: str, settings: dict | None = None) -> str | None:
    settings = settings or {}
    cameras = json.loads(assignment.get("cameras", "[]")) if isinstance(assignment.get("cameras"), str) else assignment.get("cameras", [])
    if not cameras:
        return None
    urls = [f"rtsp://{media_server}:8554/{camera}" for camera in cameras]
    width, height = _monitor_output_size(settings)
    if assignment.get("layout", "single") == "2x2":
        return quad_pipeline(urls, width or 1920, height or 1080)
    return MonitorMedia(
        urls[0], fullscreen=settings.get("kiosk", True), width=width, height=height,
        scale_mode=settings.get("scale_mode", "fit"),
        orientation=settings.get("orientation", 0), show_camera_name=settings.get("show_camera_name", False),
        show_timestamp=settings.get("show_timestamp", False), camera_name=settings.get("camera_name", ""),
    ).pipeline()


def commands_for_assignment(assignment: dict, media_server: str, settings: dict | None = None) -> list[str]:
    """Build one monitor pipeline per physical output when configured."""
    try:
        outputs = json.loads(assignment.get("outputs", "[]")) if isinstance(assignment.get("outputs"), str) else assignment.get("outputs", [])
    except json.JSONDecodeError:
        outputs = []
    if not outputs:
        command = command_for_assignment(assignment, media_server, settings)
        return [command] if command else []
    commands = []
    for output in outputs:
        cameras = output.get("cameras", []) if isinstance(output, dict) else []
        if cameras:
            connector_id = output.get("connector_id")
            settings = settings or {}
            width, height = _monitor_output_size(settings, connector_id)
            commands.append(MonitorMedia(
                f"rtsp://{media_server}:8554/{cameras[0]}", connector_id=connector_id,
                fullscreen=settings.get("kiosk", True), width=width, height=height,
                scale_mode=settings.get("scale_mode", "fit"),
                orientation=settings.get("orientation", 0), show_camera_name=settings.get("show_camera_name", False),
                show_timestamp=settings.get("show_timestamp", False), camera_name=settings.get("camera_name", ""),
            ).pipeline())
    return commands


def _monitor_output_size(settings: dict | None, connector_id: str | None = None) -> tuple[int | None, int | None]:
    settings = settings or {}
    if settings.get("auto_resolution", True):
        modes = settings.get("display_modes", {})
        mode = modes.get(str(connector_id)) if connector_id is not None else None
        mode = mode or settings.get("display_mode") or next(iter(modes.values()), None)
        if isinstance(mode, dict):
            try:
                width, height = int(mode.get("width")), int(mode.get("height"))
            except (TypeError, ValueError):
                return None, None
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                return width, height
        return None, None
    return settings.get("width"), settings.get("height")


@dataclass
class DeviceRuntime:
    role: str
    device_id: str
    name: str
    location: str
    server: str
    media_server: str
    heartbeat_interval: int = 15
    token: str = ""
    process_factory: Callable[..., subprocess.Popen] = subprocess.Popen

    def __post_init__(self) -> None:
        self.server = self.server.rstrip("/")
        self.media_server = self.media_server.rstrip("/")
        self.processes: list[subprocess.Popen] = []
        self.last_command: tuple[str, ...] | None = None

    def enroll(self) -> None:
        http_json(f"{self.server}/api/devices", "POST", {
            "id": self.device_id,
            "kind": self.role,
            "name": self.name,
            "location": self.location,
        }, token=self.token)

    def heartbeat(self) -> None:
        payload = {"runtime": "v1", "metrics": system_metrics(), "recent_logs": recent_logs()}
        if self.role == "monitor":
            payload["displays"] = connected_displays()
        http_json(f"{self.server}/api/devices/{self.device_id}/heartbeat", "POST", payload, self.token)

    def poll_commands(self) -> None:
        commands = http_json(f"{self.server}/api/devices/{self.device_id}/commands", token=self.token).get("commands", [])
        for command in commands:
            command_name = command.get("command")
            if command_name == "restart_media":
                self.stop_media()
                self.last_command = None
            elif command_name == "diagnostics":
                report = collect_diagnostics(self.role, self.device_id, self.media_server, self.processes)
                try:
                    http_json(f"{self.server}/api/devices/{self.device_id}/diagnostics", "POST", {"diagnostics": report}, self.token)
                except Exception as exc:
                    print(f"Unable to upload diagnostics: {exc}", flush=True)
            elif command_name == "update":
                print("Remote update requested", flush=True)
                subprocess.Popen([sys.executable, "-m", "cctv.update"], start_new_session=True)
                raise SystemExit("remote update requested")
            elif command_name == "restart_agent":
                self.stop_media()
                self.last_command = None
                raise SystemExit("agent restart requested")
            elif command_name == "reboot":
                print("Reboot requested; stopping media and asking systemd to reboot", flush=True)
                self.stop_media()
                subprocess.run(["systemctl", "reboot"], check=True, timeout=10)
                raise SystemExit("reboot requested")

    def desired_command(self) -> list[str]:
        if self.role == "camera":
            device = http_json(f"{self.server}/api/devices/{self.device_id}", token=self.token)
            try:
                settings = json.loads(device.get("metadata", "{}")).get("settings", {})
            except json.JSONDecodeError:
                settings = {}
            return [CameraMedia(
                f"rtsp://{self.media_server}:8554/{self.device_id}",
                width=settings.get("width", 1920), height=settings.get("height", 1080),
                fps=settings.get("fps", 30), bitrate=settings.get("bitrate", 6_000_000),
                flip_horizontal=settings.get("flip_horizontal", False), flip_vertical=settings.get("flip_vertical", False),
                brightness=settings.get("brightness", 0.0), contrast=settings.get("contrast", 1.0), saturation=settings.get("saturation", 1.0),
            ).pipeline()]
        device = http_json(f"{self.server}/api/devices/{self.device_id}", token=self.token)
        try:
            settings = json.loads(device.get("metadata", "{}")).get("settings", {})
        except json.JSONDecodeError:
            settings = {}
        settings["camera_name"] = self.name
        assignment = http_json(f"{self.server}/api/monitors/{self.device_id}/assignment", token=self.token)
        displays = connected_displays()
        settings["display_modes"] = {
            str(display.get("connector_id", display.get("id"))): {
                "width": display["width"], "height": display["height"],
            }
            for display in displays if display.get("width") and display.get("height")
        }
        settings["display_mode"] = next(iter(settings["display_modes"].values()), None)
        return commands_for_assignment(assignment, self.media_server, settings)

    def reconcile_media(self) -> None:
        commands = self.desired_command()
        signature = tuple(commands)
        if signature == self.last_command and self.processes and all(process.poll() is None for process in self.processes):
            return
        self.stop_media()
        self.last_command = signature
        for command in commands:
            print(f"Starting media pipeline for {self.device_id}", flush=True)
            self.processes.append(self.process_factory(command, shell=True, start_new_session=True))

    def stop_media(self) -> None:
        for process in self.processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
                process.wait(timeout=5)
        self.processes = []

    def run(self, enroll: bool = True) -> None:
        if enroll:
            while True:
                try:
                    self.enroll()
                    break
                except Exception as exc:
                    print(f"Enrollment failed: {exc}; retrying", flush=True)
                    time.sleep(5)
        try:
            while True:
                try:
                    self.heartbeat()
                    self.poll_commands()
                    self.reconcile_media()
                except Exception as exc:
                    print(f"Runtime check failed: {exc}; retrying", flush=True)
                time.sleep(self.heartbeat_interval)
        finally:
            self.stop_media()
