"""Unattended device runtime for camera and monitor Raspberry Pis."""

from __future__ import annotations

import json
import glob
import re
import fcntl
import subprocess
from . import __agent_version__
import time
import urllib.request
import urllib.parse
import os
import signal
import shutil
import socket
import platform
import datetime
import sys
import tempfile
from pathlib import Path
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
              "hostname": socket.gethostname(), "role": role, "platform": platform.platform(), "agent_version": __agent_version__,
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
        return quad_pipeline(urls, width or 1920, height or 1080, preview_id=settings.get("preview_id", "default"))
    return MonitorMedia(
        urls[0], fullscreen=settings.get("kiosk", True), width=width, height=height,
        scale_mode=settings.get("scale_mode", "fit"),
        orientation=settings.get("orientation", 0), show_camera_name=settings.get("show_camera_name", False),
        show_timestamp=settings.get("show_timestamp", False), camera_name=settings.get("camera_name", ""),
        preview_id=settings.get("preview_id", "default"),
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
    for output_index, output in enumerate(outputs):
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
                preview_id=f"{settings.get('preview_id', 'monitor')}-{connector_id or output_index}",
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


def apply_monitor_schedule(assignment: dict, settings: dict, now: datetime.datetime | None = None) -> dict:
    """Return the schedule-selected camera layout, if an active weekly row matches."""
    if not settings.get("schedule_enabled"):
        return assignment
    current = now or datetime.datetime.now().astimezone()
    weekday = current.weekday()
    clock = current.strftime("%H:%M")
    for row in settings.get("schedules", []):
        if not isinstance(row, dict):
            continue
        day = row.get("day")
        start = row.get("start", "")
        end = row.get("end", "")
        if day == weekday and start < end and start <= clock < end:
            active = True
        elif start > end and ((day == weekday and clock >= start)
                              or (day == (weekday - 1) % 7 and clock < end)):
            active = True
        else:
            active = False
        if active:
            return {
                **assignment,
                "layout": row["layout"],
                "cameras": json.dumps(row["cameras"]),
                "outputs": "[]",
            }
    return assignment


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
        self.last_preview_mtime_ns = 0
        self.media_retry_seconds = 5
        self.monitor_watchdog_seconds = 30
        self.next_media_retry = 0.0
        self.last_preview_progress_at = 0.0
        self.last_preview_seen_mtime_ns = 0
        self.monitor_blanking_value: bool | None = None

    def enroll(self) -> None:
        http_json(f"{self.server}/api/devices", "POST", {
            "id": self.device_id,
            "kind": self.role,
            "name": self.name,
            "location": self.location,
        }, token=self.token)

    def heartbeat(self) -> None:
        payload = {"runtime": "v1", "agent_version": __agent_version__, "metrics": system_metrics(), "recent_logs": recent_logs()}
        if self.role == "monitor":
            payload["displays"] = connected_displays()
        http_json(f"{self.server}/api/devices/{self.device_id}/heartbeat", "POST", payload, self.token)

    def report_maintenance(self, task: str, state: str, message: str) -> None:
        """Report an allowlisted maintenance task result to the dashboard."""
        try:
            http_json(
                f"{self.server}/api/devices/{urllib.parse.quote(self.device_id, safe='')}/maintenance/{task}",
                "POST", {"state": state, "details": {"message": message[:1000]}}, self.token,
            )
        except Exception as exc:
            print(f"Unable to report {task} status to dashboard: {exc}", flush=True)

    def change_server_address(self, new_server: str, new_media_server: str | None = None) -> tuple[bool, str]:
        """Verify the new dashboard with this device credential, then persist endpoints."""
        try:
            parsed = urllib.parse.urlsplit(new_server)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                return False, "The management address must be an HTTPS origin, such as https://cctv.example:8443."
            parsed.port
            if new_media_server is not None:
                media = urllib.parse.urlsplit("//" + new_media_server)
                if (not media.hostname or media.username or media.password or media.path or media.query or media.fragment
                        or not re.fullmatch(r"[A-Za-z0-9.-]+", new_media_server)
                        or new_media_server.startswith(".") or new_media_server.endswith(".")
                        or ".." in new_media_server):
                    return False, "The media server must be a hostname or IP address without a scheme or path."
            target = new_server.rstrip("/")
            target_media = new_media_server or self.media_server
            payload = {
                "runtime": "v1", "agent_version": __agent_version__, "metrics": system_metrics(),
                "recent_logs": recent_logs(),
            }
            if self.role == "monitor":
                payload["displays"] = connected_displays()
            http_json(
                f"{target}/api/devices/{urllib.parse.quote(self.device_id, safe='')}/heartbeat",
                "POST", payload, self.token,
            )
            state_path = Path(os.environ.get("CCTV_AGENT_STATE_FILE", "/var/lib/pi-cctv/device.json"))
            state = {
                "kind": self.role, "id": self.device_id, "name": self.name,
                "location": self.location, "server": target,
                "media_server": target_media, "device_token": self.token,
            }
            state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(state_path.parent, 0o700)
            descriptor, temporary = tempfile.mkstemp(prefix=".device-", dir=state_path.parent, text=True)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(state, stream)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, state_path)
                os.chmod(state_path, 0o600)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.server = target
            self.media_server = target_media
            return True, f"Management and media addresses updated; authenticated heartbeat reached {target}."
        except Exception as exc:
            return False, f"The Pi stayed on its current server address: {exc}"

    def sync_setup_ap(self) -> tuple[bool, str]:
        """Fetch and apply the server-managed bridged Wi-Fi profile at startup."""
        if not self.server.startswith("https://"):
            return False, "Setup Wi-Fi profile sync requires an HTTPS server connection."
        try:
            profile = http_json(
                f"{self.server}/api/devices/{urllib.parse.quote(self.device_id, safe='')}/setup-ap",
                token=self.token,
            )
        except Exception as exc:
            print(f"Unable to sync setup Wi-Fi profile: {exc}", flush=True)
            return False, f"Could not download the server profile: {exc}"
        if not isinstance(profile, dict):
            print("Server returned an invalid setup Wi-Fi profile; it was not applied.", flush=True)
            return False, "The server returned an invalid setup Wi-Fi profile."

        state_dir = Path("/var/lib/pi-cctv/setup-ap")
        profile_path = Path("/var/lib/pi-cctv/setup-ap-profile.json")
        state_path = state_dir / "state"
        helper = Path("/opt/pi-cctv/scripts/setup-bridged-ap.sh")
        if not profile.get("enabled"):
            if state_path.exists():
                result = subprocess.run(["bash", str(helper), "--remove"], capture_output=True, text=True, timeout=90)
                if result.returncode:
                    print("Unable to remove disabled setup Wi-Fi profile; check NetworkManager logs.", flush=True)
                    return False, "Could not remove the setup Wi-Fi bridge. Check NetworkManager and hostapd."
            profile_path.unlink(missing_ok=True)
            return True, "The server profile is disabled; the Pi setup access point is removed."

        ssid = profile.get("ssid")
        password = profile.get("password")
        country = profile.get("country")
        channel = profile.get("channel")
        if (not isinstance(ssid, str) or not 1 <= len(ssid) <= 32
                or not isinstance(password, str) or not re.fullmatch(r"[\x20-\x7e]{8,63}", password)
                or not isinstance(country, str) or len(country) != 2
                or not isinstance(channel, int) or not 1 <= channel <= 11):
            print("Server returned an invalid setup Wi-Fi profile; it was not applied.", flush=True)
            return False, "The server returned invalid Wi-Fi settings; nothing was applied."

        serialized = json.dumps(profile, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            if profile_path.read_text() == serialized and state_path.exists():
                health = subprocess.run(
                    ["bash", str(helper), "--status"],
                    capture_output=True, text=True, timeout=15,
                )
                if health.returncode == 0:
                    return True, "The saved setup Wi-Fi profile is already active and healthy."
                print("Saved setup Wi-Fi profile is present but its bridge is unhealthy; repairing it.", flush=True)
        except OSError:
            pass

        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state_dir, 0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".server-profile-", dir=state_dir, text=True)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, profile_path)
            os.chmod(profile_path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

        if state_path.exists():
            result = subprocess.run(["bash", str(helper), "--remove"], capture_output=True, text=True, timeout=90)
            if result.returncode:
                print("Unable to replace previous setup Wi-Fi profile; check NetworkManager logs.", flush=True)
                return False, "Could not remove the previous setup Wi-Fi profile. Check NetworkManager and hostapd."
        result = subprocess.run(
            ["bash", str(helper), "--apply", "--config-file", str(profile_path), "--noninteractive"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode:
            print("Setup Wi-Fi profile could not be applied; inspect hostapd and NetworkManager service logs.", flush=True)
            return False, "Setup Wi-Fi could not be applied. Check NetworkManager and hostapd."
        print("Server-managed setup Wi-Fi profile applied.", flush=True)
        return True, "Server-managed setup Wi-Fi profile applied successfully."

    def sync_setup_ap_once_per_boot(self) -> None:
        """Avoid reconfiguring networking when systemd restarts the agent."""
        safe_device_id = re.sub(r"[^A-Za-z0-9_.-]", "_", self.device_id)[:96] or "device"
        marker_dir = Path("/run/pi-cctv")
        marker = marker_dir / f"setup-ap-synced-{safe_device_id}"
        try:
            marker_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
            with marker.open("x", encoding="ascii") as marker_file:
                marker_file.write("checked\n")
        except FileExistsError:
            return
        except OSError as exc:
            print(f"Unable to record setup Wi-Fi boot check; skipping network changes: {exc}", flush=True)
            return
        self.report_maintenance("setup-ap-sync", "running", "Checking the saved server profile.")
        ok, message = self.sync_setup_ap()
        self.report_maintenance("setup-ap-sync", "succeeded" if ok else "failed", message)

    def poll_commands(self) -> None:
        commands = http_json(f"{self.server}/api/devices/{self.device_id}/commands", token=self.token).get("commands", [])
        for command in commands:
            command_name = command.get("command")
            if command_name == "restart_media":
                self.stop_media()
                self.last_command = None
            elif command_name == "sync_setup_ap":
                print("Dashboard requested setup Wi-Fi synchronization", flush=True)
                self.report_maintenance("setup-ap-sync", "running", "Checking the saved server profile.")
                ok, message = self.sync_setup_ap()
                self.report_maintenance("setup-ap-sync", "succeeded" if ok else "failed", message)
            elif command_name == "change_server_address":
                payload = command.get("payload", {})
                new_server = payload.get("server") if isinstance(payload, dict) else None
                new_media_server = payload.get("media_server") if isinstance(payload, dict) else None
                previous_server = self.server
                self.report_maintenance("server-address", "running", "Checking the new dashboard address.")
                ok, message = self.change_server_address(new_server, new_media_server) if isinstance(new_server, str) else (False, "The command did not include a valid server address.")
                if ok:
                    try:
                        http_json(
                            f"{previous_server}/api/devices/{urllib.parse.quote(self.device_id, safe='')}/maintenance/server-address",
                            "POST", {"state": "succeeded", "details": {"message": message[:1000]}}, self.token,
                        )
                    except Exception as exc:
                        print(f"Unable to report server-address status to previous dashboard: {exc}", flush=True)
                else:
                    self.report_maintenance("server-address", "failed", message)
            elif command_name == "diagnostics":
                report = collect_diagnostics(self.role, self.device_id, self.media_server, self.processes)
                try:
                    http_json(f"{self.server}/api/devices/{self.device_id}/diagnostics", "POST", {"diagnostics": report}, self.token)
                except Exception as exc:
                    print(f"Unable to upload diagnostics: {exc}", flush=True)
            elif command_name == "update":
                print("Remote update requested", flush=True)
                update_unit = f"pi-cctv-agent-update-{os.getpid()}"
                result = subprocess.run([
                    "systemd-run", "--quiet", "--no-block", "--collect",
                    f"--unit={update_unit}", "--working-directory=/opt/pi-cctv",
                    sys.executable, "-m", "cctv.update",
                ], check=False, capture_output=True, text=True, timeout=10)
                if result.returncode:
                    detail = result.stderr.strip() or result.stdout.strip()
                    print(f"Unable to start update service: {detail or result.returncode}", flush=True)
                    continue
                print(f"Update service started as {update_unit}", flush=True)
                raise SystemExit(0)
            elif command_name == "update_os":
                print("Pi OS package update requested", flush=True)
                suffix = re.sub(r"[^A-Za-z0-9_.-]", "_", self.device_id)[:40] or "device"
                update_unit = f"pi-cctv-os-update-{suffix}"
                result = subprocess.run([
                    "systemd-run", "--quiet", "--no-block", "--collect",
                    f"--unit={update_unit}", "--working-directory=/opt/pi-cctv",
                    sys.executable, "-m", "cctv.os_update",
                ], check=False, capture_output=True, text=True, timeout=10)
                if result.returncode:
                    detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
                    print(f"Unable to start Pi OS update service: {detail}", flush=True)
                    try:
                        http_json(
                            f"{self.server}/api/devices/{urllib.parse.quote(self.device_id, safe='')}/maintenance/os-update",
                            "POST", {"state": "failed", "details": {"output": f"Unable to start update service: {detail}"}}, self.token,
                        )
                    except Exception as exc:
                        print(f"Unable to report Pi OS update launch failure: {exc}", flush=True)
                    continue
                print(f"Pi OS update service started as {update_unit}", flush=True)
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
            legacy_iso = settings.get("iso", 0)
            gain = settings.get("gain")
            if gain is None:
                gain = max(1.0, float(legacy_iso) / 100.0) if legacy_iso else 1.0
            exposure_profile = settings.get("exposure_profile")
            if exposure_profile is None:
                exposure_profile = "long" if settings.get("low_light_mode") == "on" else "normal"
            return [CameraMedia(
                f"rtsp://{self.media_server}:8554/{self.device_id}",
                width=settings.get("width", 1920), height=settings.get("height", 1080),
                fps=settings.get("fps", 30), bitrate=settings.get("bitrate", 6_000_000),
                flip_horizontal=settings.get("flip_horizontal", False), flip_vertical=settings.get("flip_vertical", False),
                brightness=settings.get("brightness", 0.0), contrast=settings.get("contrast", 1.0), saturation=settings.get("saturation", 1.0),
                sharpness=settings.get("sharpness", 1.0), exposure_mode=settings.get("exposure_mode", "auto"),
                shutter_us=settings.get("shutter_us", 10_000), gain=gain, exposure_profile=exposure_profile,
                white_balance=settings.get("white_balance", "auto"),
                keyframe_interval=settings.get("keyframe_interval", settings.get("fps", 30)),
                h264_profile=settings.get("live_profile", "main"),
            ).pipeline()]
        device = http_json(f"{self.server}/api/devices/{self.device_id}", token=self.token)
        try:
            settings = json.loads(device.get("metadata", "{}")).get("settings", {})
        except json.JSONDecodeError:
            settings = {}
        try:
            self.media_retry_seconds = max(1, min(120, int(settings.get("reconnect_seconds", 5))))
        except (TypeError, ValueError):
            self.media_retry_seconds = 5
        try:
            self.monitor_watchdog_seconds = max(5, min(600, int(settings.get("watchdog_seconds", 30))))
        except (TypeError, ValueError):
            self.monitor_watchdog_seconds = 30
        self.apply_monitor_blanking(settings.get("prevent_blanking", True))
        settings["camera_name"] = self.name
        settings["preview_id"] = self.device_id
        assignment = http_json(f"{self.server}/api/monitors/{self.device_id}/assignment", token=self.token)
        assignment = apply_monitor_schedule(assignment, settings)
        offline_cameras = set(assignment.get("offline_cameras", []))
        fallback_camera = assignment.get("fallback_camera")
        if fallback_camera and offline_cameras:
            try:
                assigned_cameras = json.loads(assignment.get("cameras", "[]"))
                assignment["cameras"] = json.dumps([
                    fallback_camera if camera_id in offline_cameras else camera_id
                    for camera_id in assigned_cameras
                ])
                outputs = json.loads(assignment.get("outputs", "[]"))
                for output in outputs:
                    output["cameras"] = [
                        fallback_camera if camera_id in offline_cameras else camera_id
                        for camera_id in output.get("cameras", [])
                    ]
                assignment["outputs"] = json.dumps(outputs)
                print(f"Using fallback camera {fallback_camera} for offline route(s)", flush=True)
            except (TypeError, json.JSONDecodeError):
                print("Could not apply monitor fallback camera to the current assignment", flush=True)
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
        watchdog_stalled = False
        if signature == self.last_command and self.processes and all(process.poll() is None for process in self.processes):
            if (self.role != "monitor" or not commands
                    or time.monotonic() - self.last_preview_progress_at < self.monitor_watchdog_seconds):
                return
            watchdog_stalled = True
            print(f"Monitor preview stalled for {self.monitor_watchdog_seconds}s; restarting media pipeline", flush=True)
        if signature == self.last_command and not watchdog_stalled and time.monotonic() < self.next_media_retry:
            return
        self.stop_media()
        self.last_command = signature
        for command in commands:
            print(f"Starting media pipeline for {self.device_id}", flush=True)
            self.processes.append(self.process_factory(command, shell=True, start_new_session=True))
        if commands:
            self.next_media_retry = time.monotonic() + self.media_retry_seconds
            self.last_preview_progress_at = time.monotonic()
            self.last_preview_seen_mtime_ns = 0

    def apply_monitor_blanking(self, prevent_blanking: bool) -> None:
        """Apply the saved console blanking policy on the monitor's active VT."""
        if self.role != "monitor":
            return
        prevent_blanking = bool(prevent_blanking)
        if self.monitor_blanking_value is prevent_blanking:
            return
        self.monitor_blanking_value = prevent_blanking
        terminal = None
        try:
            terminal = os.open("/dev/tty0", os.O_WRONLY | os.O_NOCTTY)
            args = ["setterm", "--blank", "0" if prevent_blanking else "10"]
            if prevent_blanking:
                args.extend(["--powerdown", "0", "--powersave", "off"])
            subprocess.run(args, check=True, stdout=terminal, stderr=subprocess.PIPE, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Unable to apply monitor screen blanking setting: {exc}", flush=True)
        finally:
            if terminal is not None:
                os.close(terminal)

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

    def upload_monitor_preview(self) -> None:
        """Upload the newest completed, low-rate display pipeline JPEG."""
        if self.role != "monitor" or not self.token:
            return
        candidates = []
        for filename in glob.glob("/tmp/pi-cctv-monitor-preview-*.jpg"):
            try:
                stat = os.stat(filename)
            except OSError:
                continue
            if stat.st_mtime_ns > self.last_preview_seen_mtime_ns and 0 < time.time() - stat.st_mtime < 10 and stat.st_size <= 1_500_000:
                candidates.append((stat.st_mtime_ns, stat.st_mtime, filename))
        if not candidates:
            return
        mtime_ns, mtime, filename = max(candidates)
        # multifilesink writes each JPEG at 0.2 fps; avoid reading a file mid-write.
        if time.time() - mtime < 1:
            return
        try:
            with open(filename, "rb") as preview_file:
                image = preview_file.read(1_500_001)
            if len(image) > 1_500_000 or not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
                return
            self.last_preview_seen_mtime_ns = mtime_ns
            self.last_preview_progress_at = time.monotonic()
            request = urllib.request.Request(
                f"{self.server}/api/devices/{urllib.parse.quote(self.device_id, safe='')}/preview",
                data=image, method="POST",
                headers={"Content-Type": "image/jpeg", "Authorization": f"Bearer {self.token}"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
            self.last_preview_mtime_ns = mtime_ns
        except (OSError, urllib.error.URLError) as exc:
            print(f"Unable to upload monitor preview: {exc}", flush=True)

    def activate_monitor_display(self) -> None:
        """Move away from the Linux login console before starting the video plane."""
        if self.role != "monitor":
            return
        terminal = None
        try:
            terminal = os.open("/dev/tty0", os.O_RDWR | os.O_NOCTTY)
            fcntl.ioctl(terminal, 0x5606, 7)  # VT_ACTIVATE
            fcntl.ioctl(terminal, 0x5607, 7)  # VT_WAITACTIVE
        except OSError as exc:
            print(f"Unable to switch to the kiosk display VT: {exc}", flush=True)
        finally:
            if terminal is not None:
                os.close(terminal)

    def run(self, enroll: bool = True) -> None:
        if enroll:
            while True:
                try:
                    self.enroll()
                    break
                except Exception as exc:
                    print(f"Enrollment failed: {exc}; retrying", flush=True)
                    time.sleep(5)
        self.activate_monitor_display()
        try:
            try:
                self.sync_setup_ap_once_per_boot()
            except Exception as exc:
                print(f"Unable to apply setup Wi-Fi profile at startup: {exc}", flush=True)
            while True:
                try:
                    self.heartbeat()
                    self.poll_commands()
                    self.reconcile_media()
                    self.upload_monitor_preview()
                except Exception as exc:
                    print(f"Runtime check failed: {exc}; retrying", flush=True)
                time.sleep(self.heartbeat_interval)
        finally:
            self.stop_media()
