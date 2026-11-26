"""Time-indexed preview track for recorded footage.

Each closed recording segment gets a small sheet of 256x144 frames taken from
its keyframes (about one per second), plus a few 160x90 thumbnails on a fixed
15-second wall-clock grid. Hour manifests list every frame with its absolute
timestamp, so the dashboard can show the frame nearest to the timeline cursor
without asking the server to decode anything while the user scrolls.

The original recordings are never modified.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import quote
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from .recording import mp4_duration, recording_files

PREVIEW_ROOT = ".preview-track"
FINE_WIDTH, FINE_HEIGHT, FINE_COLUMNS, FINE_ROWS = 256, 144, 6, 5
COARSE_WIDTH, COARSE_HEIGHT, COARSE_COLUMNS, COARSE_INTERVAL = 160, 90, 16, 15
MANIFEST_VERSION = 1
CLOSED_AFTER_SECONDS = 8
RETRY_FAILED_SECONDS = 600
BUILD_TIMEOUT_SECONDS = 240

_build_slot = threading.BoundedSemaphore(1)
_manifest_lock = threading.Lock()
_manifest_cache: OrderedDict[tuple[str, int], dict] = OrderedDict()
_coarse_lock = threading.Lock()
_PTS = re.compile(r"Parsed_showinfo.*?\bpts_time:\s*(-?[0-9.]+)")


def preview_dir(root: Path, recording: Path) -> Path:
    relative = recording.relative_to(root)
    return root / PREVIEW_ROOT / relative.parent / relative.stem


def preview_ready(source: Path, directory: Path) -> bool:
    manifest = directory / "manifest.json"
    try:
        return manifest.is_file() and manifest.stat().st_mtime_ns >= source.stat().st_mtime_ns
    except OSError:
        return False


def preview_failed_recently(directory: Path, now: float | None = None) -> bool:
    failure = directory.with_suffix(".failed")
    try:
        return failure.is_file() and (now or time.time()) - failure.stat().st_mtime < RETRY_FAILED_SECONDS
    except OSError:
        return False


def segment_start(root: Path, recording: Path) -> float | None:
    """Absolute start of a segment named <camera>/<YYYY-MM-DD>/<HH-MM-SS>.ext.

    The recorder names files from the server's local clock, matching the
    interpretation used by /api/recordings.
    """
    try:
        relative = recording.relative_to(root)
        stamp = datetime.strptime(f"{relative.parts[-2]} {recording.stem}", "%Y-%m-%d %H-%M-%S")
        return stamp.astimezone(timezone.utc).timestamp()
    except (IndexError, ValueError):
        return None


def _nice() -> None:  # pragma: no cover - runs in the child process
    try:
        os.nice(10)
    except OSError:
        pass


def _extract(source: Path, temporary: Path, keyframes: bool) -> tuple[int, list[float]]:
    """Run one FFmpeg pass. Returns (exit code, frame offsets in seconds)."""
    pick = ("select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,0.9)'" if keyframes else "fps=1")
    graph = (
        f"[0:v:0]{pick},showinfo,"
        f"scale={FINE_WIDTH}:{FINE_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={FINE_WIDTH}:{FINE_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,split=2[f][c];"
        f"[f]tile={FINE_COLUMNS}x{FINE_ROWS}[sheet];[c]scale={COARSE_WIDTH}:{COARSE_HEIGHT}[thumb]"
    )
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info", "-y", "-threads", "2"]
    if keyframes:
        command += ["-skip_frame", "nokey"]
    command += [
        "-i", str(source), "-an", "-filter_complex", graph,
        "-map", "[sheet]", "-fps_mode", "vfr", "-q:v", "6", "-start_number", "0", str(temporary / "%03d.jpg"),
        "-map", "[thumb]", "-fps_mode", "passthrough", "-q:v", "7", "-start_number", "0",
        str(temporary / "thumbs" / "%04d.jpg"),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=BUILD_TIMEOUT_SECONDS,
                            preexec_fn=_nice if os.name == "posix" else None)
    times = [max(0.0, float(match)) for match in _PTS.findall(result.stderr or "")]
    return result.returncode, times


def build_segment_preview(root: Path, source: Path) -> bool | None:
    """Build the preview for one closed segment.

    Returns True when built, None when already current or unusable, and False
    when another build holds the decoder slot.
    """
    directory = preview_dir(root, source)
    if preview_ready(source, directory):
        return None
    if not _build_slot.acquire(blocking=False):
        return False
    temporary = directory.with_name(f"{directory.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    failure = directory.with_suffix(".failed")
    success = False
    try:
        if preview_ready(source, directory):
            return None
        start = segment_start(root, source)
        if start is None:
            return None
        duration = mp4_duration(source) if source.suffix == ".mp4" else None
        shutil.rmtree(temporary, ignore_errors=True)
        (temporary / "thumbs").mkdir(parents=True)
        code, times = _extract(source, temporary, keyframes=True)
        expected = duration or (times[-1] + 1 if times else 0)
        # Cameras configured with long keyframe intervals give sparse
        # keyframes. Fall back to decoding every frame at 1 fps for those.
        keyframes = True
        if code == 0 and expected > 5 and len(times) < expected / 2.5:
            shutil.rmtree(temporary, ignore_errors=True)
            (temporary / "thumbs").mkdir(parents=True)
            code, times = _extract(source, temporary, keyframes=False)
            keyframes = False
        sheets = sorted(temporary.glob("*.jpg"))
        thumbs = sorted((temporary / "thumbs").glob("*.jpg"))
        if code != 0 or not times or not sheets or len(thumbs) < len(times):
            return None
        per_sheet = FINE_COLUMNS * FINE_ROWS
        times = times[: min(len(times), len(sheets) * per_sheet)]
        duration = duration or (times[-1] + 1)
        # Keep the thumbnail nearest to each 15-second wall-clock mark.
        nearest: dict[int, tuple[float, int]] = {}
        for index, offset in enumerate(times):
            absolute = start + offset
            mark = round(absolute / COARSE_INTERVAL)
            distance = abs(absolute - mark * COARSE_INTERVAL)
            if distance <= COARSE_INTERVAL / 2 and (mark not in nearest or distance < nearest[mark][0]):
                nearest[mark] = (distance, index)
        coarse_dir = temporary / "coarse"
        coarse_dir.mkdir()
        coarse = []
        for mark in sorted(nearest):
            index = nearest[mark][1]
            name = f"{index:04d}.jpg"
            thumbs[index].replace(coarse_dir / name)
            coarse.append({"offset": round(times[index], 3), "file": f"coarse/{name}"})
        shutil.rmtree(temporary / "thumbs", ignore_errors=True)
        (temporary / "manifest.json").write_text(json.dumps({
            "version": MANIFEST_VERSION, "width": FINE_WIDTH, "height": FINE_HEIGHT,
            "columns": FINE_COLUMNS, "rows": FINE_ROWS, "sheets": math.ceil(len(times) / per_sheet),
            "times": [round(value, 3) for value in times], "duration": round(duration, 3),
            "keyframes": keyframes, "coarse": coarse,
        }, separators=(",", ":")))
        directory.parent.mkdir(parents=True, exist_ok=True)
        if directory.exists():
            shutil.rmtree(directory)
        temporary.replace(directory)
        failure.unlink(missing_ok=True)
        success = True
        return True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            shutil.rmtree(temporary, ignore_errors=True)
            if not success and not preview_ready(source, directory) and directory.parent.is_dir():
                failure.touch()
        finally:
            _build_slot.release()


def next_preview_source(root: Path, now: float | None = None) -> Path | None:
    """Pick the newest closed segment without a current preview."""
    now = time.time() if now is None else now
    candidates = []
    for source in recording_files(root):
        try:
            modified = source.stat().st_mtime
        except OSError:
            continue
        if now - modified >= CLOSED_AFTER_SECONDS:
            candidates.append((modified, source))
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, source in candidates:
        directory = preview_dir(root, source)
        if preview_ready(source, directory) or preview_failed_recently(directory, now):
            continue
        return source
    return None


def _segment_manifest(directory: Path) -> dict | None:
    path = directory / "manifest.json"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    key = (str(path), stamp)
    with _manifest_lock:
        cached = _manifest_cache.get(key)
        if cached is not None:
            _manifest_cache.move_to_end(key)
            return cached
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if data.get("version") != MANIFEST_VERSION:
        return None
    data["_stamp"] = stamp
    with _manifest_lock:
        _manifest_cache[key] = data
        while len(_manifest_cache) > 4096:
            _manifest_cache.popitem(last=False)
    return data


def _hour_sources(root: Path, camera: str, hour: int) -> list[tuple[float, Path]]:
    days = {datetime.fromtimestamp(stamp).astimezone().strftime("%Y-%m-%d") for stamp in (hour - 3600, hour, hour + 3599)}
    found = []
    for day in sorted(days):
        try:
            names = os.listdir(root / camera / day)
        except OSError:
            continue
        for name in names:
            if name.startswith(".") or not name.endswith((".mp4", ".mkv")):
                continue
            source = root / camera / day / name
            start = segment_start(root, source)
            # Segments may last up to an hour; include those overlapping it.
            if start is not None and hour - 3600 < start < hour + 3600:
                found.append((start, source))
    found.sort()
    return found


def hour_manifest(root: Path, camera: str, hour: int, now: float | None = None) -> dict:
    """List every preview frame in [hour, hour + 3600) with absolute times."""
    now = time.time() if now is None else now
    segments, pending, coarse = [], [], []
    for start, source in _hour_sources(root, camera, hour):
        try:
            stat = source.stat()
        except OSError:
            continue
        relative = source.relative_to(root).as_posix()
        directory = preview_dir(root, source)
        data = _segment_manifest(directory) if preview_ready(source, directory) else None
        if data is None:
            if start >= hour + 3600:
                continue
            duration = mp4_duration(source) if source.suffix == ".mp4" else None
            if start + (duration or 30) <= hour:
                continue
            state = ("recording" if now - stat.st_mtime < CLOSED_AFTER_SECONDS
                     else "failed" if preview_failed_recently(directory, now) else "pending")
            pending.append({"path": relative, "start": start, "state": state})
            continue
        if start + data["duration"] <= hour or start >= hour + 3600:
            continue
        base = "/recordings/" + directory.relative_to(root).as_posix() + "/"
        segments.append({
            "path": relative, "start": start, "duration": data["duration"], "base": base,
            "version": str(data["_stamp"]), "columns": data["columns"], "rows": data["rows"],
            "width": data["width"], "height": data["height"], "sheets": data["sheets"],
            "times": data["times"], "keyframes": data.get("keyframes", True),
        })
        for item in data.get("coarse", []):
            absolute = start + item["offset"]
            if hour <= absolute < hour + 3600:
                coarse.append((absolute, directory / item["file"]))
    # Neighbouring segments can both offer a frame for a boundary mark; keep
    # the one closest to the mark so the coarse level stays evenly spaced.
    marks: dict[int, tuple[float, Path]] = {}
    for absolute, path in coarse:
        mark = round(absolute / COARSE_INTERVAL)
        if mark not in marks or abs(absolute - mark * COARSE_INTERVAL) < abs(marks[mark][0] - mark * COARSE_INTERVAL):
            marks[mark] = (absolute, path)
    coarse = [marks[mark] for mark in sorted(marks)]
    coarse_entry = None
    if coarse:
        digest = hashlib.sha1("\n".join(f"{t:.3f}:{p}" for t, p in coarse).encode()).hexdigest()[:16]
        coarse_entry = {
            "url": f"/api/recordings/preview-coarse?camera_id={quote(camera)}&hour={hour}&v={digest}",
            "version": digest, "width": COARSE_WIDTH, "height": COARSE_HEIGHT,
            "columns": COARSE_COLUMNS, "times": [round(t, 3) for t, _ in coarse],
        }
    return {"camera_id": camera, "hour": hour, "interval": 1, "coarse_interval": COARSE_INTERVAL,
            "segments": segments, "pending": pending, "coarse": coarse_entry,
            "_coarse_files": [path for _, path in coarse]}


def coarse_sheet(root: Path, camera: str, hour: int) -> tuple[Path, str] | None:
    """Tile the hour's 15-second thumbnails into one cached JPEG."""
    manifest = hour_manifest(root, camera, hour)
    entry, files = manifest["coarse"], manifest["_coarse_files"]
    if not entry or not files:
        return None
    cache_dir = root / PREVIEW_ROOT / camera / ".hours"
    target = cache_dir / f"{hour}-{entry['version']}.jpg"
    with _coarse_lock:
        if target.is_file():
            return target, entry["version"]
        cache_dir.mkdir(parents=True, exist_ok=True)
        rows = max(1, math.ceil(len(files) / COARSE_COLUMNS))
        listing = cache_dir / f".{hour}-{os.getpid()}-{threading.get_ident()}.txt"
        temporary = cache_dir / f".{hour}-{os.getpid()}-{threading.get_ident()}.jpg"
        try:
            listing.write_text("".join("file '" + str(path).replace("'", "'\\''") + "'\n" for path in files))
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(listing), "-vf", f"tile={COARSE_COLUMNS}x{rows}", "-frames:v", "1", "-q:v", "7",
                 str(temporary)], capture_output=True, timeout=60)
            if result.returncode != 0 or not temporary.is_file():
                return None
            temporary.replace(target)
            for old in cache_dir.glob(f"{hour}-*.jpg"):
                if old != target:
                    old.unlink(missing_ok=True)
            return target, entry["version"]
        except (OSError, subprocess.TimeoutExpired):
            return None
        finally:
            listing.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)


def preview_size(root: Path, recording: Path) -> int:
    directory = preview_dir(root, recording)
    try:
        return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
    except OSError:
        return 0


def remove_preview(root: Path, recording: Path) -> None:
    directory = preview_dir(root, recording)
    shutil.rmtree(directory, ignore_errors=True)
    directory.with_suffix(".failed").unlink(missing_ok=True)


def prune_previews(root: Path, retention_hours: int, now: float | None = None) -> int:
    """Remove previews whose recording is gone and stale hour sheets."""
    now = time.time() if now is None else now
    base = root / PREVIEW_ROOT
    removed = 0
    for manifest in base.glob("*/*/*/manifest.json"):
        source = root / manifest.parent.relative_to(base)
        if not source.with_suffix(".mp4").is_file() and not source.with_suffix(".mkv").is_file():
            shutil.rmtree(manifest.parent, ignore_errors=True)
            removed += 1
    for failure in base.glob("*/*/*.failed"):
        source = root / failure.relative_to(base).with_suffix("")
        if not source.with_suffix(".mp4").is_file() and not source.with_suffix(".mkv").is_file():
            failure.unlink(missing_ok=True)
    cutoff = now - max(1, retention_hours) * 3600 - 7200
    for sheet in base.glob("*/.hours/*.jpg"):
        try:
            hour = int(sheet.stem.split("-", 1)[0])
        except ValueError:
            continue
        if hour < cutoff:
            sheet.unlink(missing_ok=True)
    for directory in sorted(base.glob("*/*"), reverse=True):
        if directory.is_dir() and directory.name != ".hours":
            try:
                directory.rmdir()  # Only succeeds when empty.
            except OSError:
                pass
    return removed
