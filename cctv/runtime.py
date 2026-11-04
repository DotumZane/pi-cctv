"""Unattended device runtime for camera and monitor Raspberry Pis."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .media import CameraMedia, MonitorMedia, quad_pipeline


def http_json(url: str, method: str = "GET", payload: dict | None = None, token: str = "") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def command_for_assignment(assignment: dict, media_server: str) -> str | None:
    cameras = json.loads(assignment.get("cameras", "[]")) if isinstance(assignment.get("cameras"), str) else assignment.get("cameras", [])
    if not cameras:
        return None
    urls = [f"rtsp://{media_server}:8554/{camera}" for camera in cameras]
    if assignment.get("layout", "single") == "2x2":
        return quad_pipeline(urls)
    return MonitorMedia(urls[0]).pipeline()


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
        self.process: subprocess.Popen | None = None
        self.last_command: str | None = None

    def enroll(self) -> None:
        http_json(f"{self.server}/api/devices", "POST", {
            "id": self.device_id,
            "kind": self.role,
            "name": self.name,
            "location": self.location,
        }, token=self.token)

    def heartbeat(self) -> None:
        http_json(f"{self.server}/api/devices/{self.device_id}/heartbeat", "POST", {"runtime": "v1"}, self.token)

    def poll_commands(self) -> None:
        commands = http_json(f"{self.server}/api/devices/{self.device_id}/commands", token=self.token).get("commands", [])
        for command in commands:
            if command.get("command") == "restart_media":
                self.stop_media()
                self.last_command = None

    def desired_command(self) -> str | None:
        if self.role == "camera":
            device = http_json(f"{self.server}/api/devices/{self.device_id}", token=self.token)
            try:
                settings = json.loads(device.get("metadata", "{}")).get("settings", {})
            except json.JSONDecodeError:
                settings = {}
            return CameraMedia(
                f"rtsp://{self.media_server}:8554/{self.device_id}",
                width=settings.get("width", 1920), height=settings.get("height", 1080),
                fps=settings.get("fps", 30), bitrate=settings.get("bitrate", 6_000_000),
            ).pipeline()
        assignment = http_json(f"{self.server}/api/monitors/{self.device_id}/assignment", token=self.token)
        return command_for_assignment(assignment, self.media_server)

    def reconcile_media(self) -> None:
        command = self.desired_command()
        if command == self.last_command and self.process and self.process.poll() is None:
            return
        self.stop_media()
        self.last_command = command
        if command:
            print(f"Starting media pipeline for {self.device_id}", flush=True)
            self.process = self.process_factory(command, shell=True)

    def stop_media(self) -> None:
        if not self.process or self.process.poll() is not None:
            self.process = None
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None

    def run(self) -> None:
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
