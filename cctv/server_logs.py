"""Keep a bounded copy of management output for administrator downloads."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SafeRotatingFileHandler(RotatingFileHandler):
    def handleError(self, record):
        # stderr is also captured, so logging's default traceback would recurse
        # indefinitely when the filesystem is full or unwritable.
        pass


class TeeStream:
    def __init__(self, original, handler):
        self.original = original
        self.handler = handler

    def write(self, value):
        result = self.original.write(value)
        if value:
            record = logging.LogRecord("cctv.server", logging.INFO, "", 0, value, (), None)
            self.handler.handle(record)
        return result

    def flush(self):
        self.original.flush()
        self.handler.flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


def capture_server_output(path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = SafeRotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    except OSError as exc:
        print(f"Server log file unavailable: {exc}", file=sys.stderr)
        return None
    handler.terminator = ""
    sys.stdout = TeeStream(sys.stdout, handler)
    sys.stderr = TeeStream(sys.stderr, handler)
    return handler


def read_server_logs(path: Path, host_update_dir: Path) -> str:
    sections = ["Pi CCTV management server logs (up to the last 6 MB)"]
    for candidate in (Path(str(path) + ".2"), Path(str(path) + ".1"), path):
        try:
            with candidate.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 2_000_000))
                sections.append(stream.read().decode("utf-8", errors="replace"))
        except OSError:
            continue
    for name, label in (("status.json", "Server software update"), ("os-status.json", "Ubuntu update"), ("reboot-status.json", "Server reboot")):
        try:
            import json
            with (host_update_dir / name).open("rb") as stream:
                data = stream.read(100_001)
            if len(data) > 100_000:
                continue
            status = json.loads(data)
            sections.append(f"\n--- {label} ---\nState: {status.get('state', 'unknown')}\n{status.get('log', '')}\n{status.get('error', '')}")
        except (OSError, ValueError, AttributeError):
            continue
    return "\n".join(sections) + "\n"
