"""Small, temporary timeline overview sheets assembled from existing atlases."""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .recording import scrub_frames_dir, scrub_frames_ready

_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='scrub-overview')
_cache: OrderedDict[tuple[str, str, int, str], tuple[float, dict, list[Path]]] = OrderedDict()
_jobs: set[tuple[str, str, int, str]] = set()


def build_overview(root: Path, camera: str, hour: int, detail: bool = False, compact: bool = False) -> dict:
    """Sample one real frame every two minutes from closed segments."""
    candidates = {}
    days = {datetime.fromtimestamp(t).astimezone().strftime('%Y-%m-%d') for t in (hour, hour + 3599)}
    for day in days:
        for source in (root / camera / day).glob('*.*'):
            if source.suffix not in ('.mp4', '.mkv'):
                continue
            try:
                stamp = datetime.strptime(f'{day} {source.stem}', '%Y-%m-%d %H-%M-%S').astimezone().timestamp()
                if not hour <= stamp < hour + 3600 or time.time() - source.stat().st_mtime < 8:
                    continue
                directory = scrub_frames_dir(root, source)
                if not scrub_frames_ready(source, directory) or not (directory / '000.jpg').is_file():
                    continue
                manifest = json.loads((directory / 'manifest.json').read_text())
                if manifest.get('width') != 384 or manifest.get('height') != 216:
                    continue
                bucket = int((stamp - hour) // 120)
                if bucket not in candidates or stamp < candidates[bucket][0]:
                    candidates[bucket] = (stamp, directory / '000.jpg', source)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    items = [candidates[key] for key in sorted(candidates)]
    if not items:
        return {'ready': True, 'times': [], 'image': None}
    with tempfile.TemporaryDirectory(prefix='cctv-overview-') as tmp:
        width, height = (512, 288) if detail else (160, 90) if compact else (384, 216)
        if detail:
            extracted = []
            deadline = time.monotonic() + 12
            for index, (stamp, atlas, source) in enumerate(items):
                if time.monotonic() >= deadline:
                    break
                frame = Path(tmp) / f'{index:02}.jpg'
                try:
                    decoded = subprocess.run(
                        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '1',
                         '-i', str(source), '-map', '0:v:0', '-frames:v', '1', '-an',
                         '-vf', 'scale=512:288:force_original_aspect_ratio=decrease,pad=512:288:(ow-iw)/2:(oh-ih)/2',
                         '-q:v', '5', '-y', str(frame)], capture_output=True, timeout=2)
                    if decoded.returncode == 0 and frame.is_file():
                        extracted.append((stamp, frame, atlas, source))
                except (OSError, subprocess.TimeoutExpired):
                    continue
            # Never replace a complete fast index with a much sparser one.
            if len(extracted) < len(items) * 0.8:
                return {'ready': True, 'times': [], 'image': None}
        else:
            extracted = [(stamp, atlas, atlas, source) for stamp, atlas, source in items]
        listing = Path(tmp) / 'images.txt'
        listing.write_text(''.join("file '" + str(path).replace("'", "'\\''") + "'\n" for _, path, _, _ in extracted))
        filters = 'tile=6x5' if detail else 'crop=384:216:0:0,scale=160:90,tile=6x5' if compact else 'crop=384:216:0:0,tile=6x5'
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-f', 'concat', '-safe', '0',
             '-i', str(listing), '-vf', filters,
             '-frames:v', '1', '-q:v', '9', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'],
            capture_output=True, timeout=45,
        )
    if result.returncode or not result.stdout.startswith(b'\xff\xd8'):
        raise RuntimeError('overview image generation failed')
    return {'ready': True, 'times': [item[0] for item in extracted], 'image': base64.b64encode(result.stdout).decode('ascii'),
            'width': width, 'height': height, 'columns': 6,
            '_sources': [path for _, _, atlas, source in extracted for path in (atlas, source)]}


def overview(root: Path, camera: str, hour: int, quality: str = 'standard') -> dict:
    key = (str(root), camera, hour, quality)
    with _lock:
        cached = _cache.get(key)
        if (cached and time.monotonic() - cached[0] < (20 if not cached[1]['times'] or hour + 3600 > time.time() else 300)
                and all(path.is_file() for path in cached[2])):
            _cache.move_to_end(key)
            return cached[1]
        if key not in _jobs and len(_jobs) < 12:
            _jobs.add(key)
            _pool.submit(_build_job, root, camera, hour, quality, key)
    return {'ready': False}


def _build_job(root: Path, camera: str, hour: int, quality: str, key: tuple[str, str, int, str]) -> None:
    try:
        result = build_overview(root, camera, hour, detail=quality == 'high', compact=quality == 'compact')
        sources = result.pop('_sources', [])
        with _lock:
            _cache[key] = (time.monotonic(), result, sources)
            _cache.move_to_end(key)
            while len(_cache) > 32:
                _cache.popitem(last=False)
    except (OSError, subprocess.TimeoutExpired, RuntimeError):
        pass
    finally:
        with _lock:
            _jobs.discard(key)
