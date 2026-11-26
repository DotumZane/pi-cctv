import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cctv import __agent_version__
from cctv.fleet import FleetAgentUpdateCoordinator, FleetOsUpdateCoordinator
from cctv.preview_track import (COARSE_INTERVAL, build_segment_preview, coarse_sheet, hour_manifest,
                                next_preview_source, preview_dir, preview_ready, preview_size, prune_previews)
from cctv.recording import enforce_storage_limit, prune_recordings
from cctv.server import Handler
from cctv.store import Store


def make_segment(root: Path, start: float, seconds: int = 30, gop: int = 10, rate: int = 10) -> Path:
    stamp = datetime.fromtimestamp(start).astimezone()
    path = root / "cam-1" / stamp.strftime("%Y-%m-%d") / (stamp.strftime("%H-%M-%S") + ".mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc2=s=320x180:r={rate}:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-bf", "0",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)], check=True)
    os.utime(path, (time.time() - 60, time.time() - 60))
    return path


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class PreviewTrackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # A whole hour, two hours ago, so segments are closed and local-time
        # day boundaries do not matter.
        self.hour = int(time.time() // 3600) * 3600 - 7200

    def tearDown(self):
        self.temp.cleanup()

    def test_keyframes_become_one_frame_per_second_with_absolute_times(self):
        first = make_segment(self.root, self.hour)
        second = make_segment(self.root, self.hour + 30)
        self.assertEqual(next_preview_source(self.root), second, "newest closed segment is indexed first")
        self.assertTrue(build_segment_preview(self.root, second))
        self.assertEqual(next_preview_source(self.root), first)
        self.assertTrue(build_segment_preview(self.root, first))
        self.assertIsNone(next_preview_source(self.root))
        self.assertIsNone(build_segment_preview(self.root, first), "a current preview is not rebuilt")
        manifest = json.loads((preview_dir(self.root, first) / "manifest.json").read_text())
        self.assertEqual(manifest["times"], [float(i) for i in range(30)])
        self.assertTrue(manifest["keyframes"])
        self.assertEqual((manifest["width"], manifest["height"], manifest["columns"], manifest["rows"]), (256, 144, 6, 5))
        sheet = preview_dir(self.root, first) / "000.jpg"
        size = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(sheet)],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(size, "1536,720")
        hour = hour_manifest(self.root, "cam-1", self.hour)
        self.assertEqual([segment["start"] for segment in hour["segments"]], [self.hour, self.hour + 30])
        self.assertEqual(hour["pending"], [])
        # Coarse frames sit on the 15-second wall-clock grid, one per mark,
        # even where two segments meet; the last frame stands in for the
        # mark one second after the recording ends.
        self.assertEqual(hour["coarse"]["times"], [self.hour + i * COARSE_INTERVAL for i in range(4)] + [self.hour + 59])
        built = coarse_sheet(self.root, "cam-1", self.hour)
        self.assertIsNotNone(built)
        self.assertEqual(built[1], hour["coarse"]["version"])
        self.assertTrue(built[0].read_bytes().startswith(b"\xff\xd8"))
        self.assertEqual(coarse_sheet(self.root, "cam-1", self.hour)[0], built[0], "the hour sheet is cached")

    def test_sparse_keyframes_fall_back_to_one_frame_per_second(self):
        source = make_segment(self.root, self.hour, seconds=20, gop=100)
        self.assertTrue(build_segment_preview(self.root, source))
        manifest = json.loads((preview_dir(self.root, source) / "manifest.json").read_text())
        self.assertFalse(manifest["keyframes"])
        self.assertGreaterEqual(len(manifest["times"]), 19)

    def test_unusable_recordings_are_marked_and_retried_later(self):
        source = self.root / "cam-1" / datetime.fromtimestamp(self.hour).astimezone().strftime("%Y-%m-%d") / "06-00-00.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"not a video")
        os.utime(source, (time.time() - 60, time.time() - 60))
        self.assertIsNone(build_segment_preview(self.root, source))
        self.assertFalse(preview_ready(source, preview_dir(self.root, source)))
        self.assertTrue(preview_dir(self.root, source).with_suffix(".failed").is_file())
        self.assertIsNone(next_preview_source(self.root), "failed segments wait before retrying")

    def test_open_segments_are_reported_as_recording(self):
        source = make_segment(self.root, self.hour)
        os.utime(source, None)
        self.assertIsNone(next_preview_source(self.root))
        self.assertEqual(hour_manifest(self.root, "cam-1", self.hour)["pending"][0]["state"], "recording")

    def test_previews_follow_retention_and_storage_limits(self):
        source = make_segment(self.root, self.hour)
        self.assertTrue(build_segment_preview(self.root, source))
        directory = preview_dir(self.root, source)
        coarse_sheet(self.root, "cam-1", self.hour)
        os.utime(source, (time.time() - 7200, time.time() - 7200))
        self.assertEqual(prune_recordings(self.root, 1), 1)
        self.assertFalse(directory.exists())
        orphan = make_segment(self.root, self.hour + 30)
        build_segment_preview(self.root, orphan)
        orphan.unlink()
        self.assertEqual(prune_previews(self.root, 24), 1)
        kept = make_segment(self.root, self.hour + 60)
        build_segment_preview(self.root, kept)
        self.assertGreater(preview_size(self.root, kept), 0, "previews count towards the storage limit")
        with patch("cctv.recording.shutil.disk_usage"):
            self.assertEqual(enforce_storage_limit(self.root, 1), 0)
        self.assertTrue(preview_dir(self.root, kept).exists())


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class PreviewTrackApiTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.store = Store(":memory:")
        self.httpd.fleet_agent_updater = FleetAgentUpdateCoordinator(self.httpd.store, __agent_version__)
        self.httpd.fleet_os_updater = FleetOsUpdateCoordinator(self.httpd.store, __agent_version__)
        self.recordings = tempfile.TemporaryDirectory()
        self.httpd.recordings_dir = self.recordings.name
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.root = Path(self.recordings.name)
        self.hour = int(time.time() // 3600) * 3600 - 7200

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.fleet_agent_updater.stop()
        self.httpd.fleet_os_updater.stop()
        self.httpd.store.close()
        self.recordings.cleanup()

    def get(self, path, headers=None):
        connection = HTTPConnection("127.0.0.1", self.httpd.server_port)
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_manifest_and_sheets_are_cacheable(self):
        source = make_segment(self.root, self.hour)
        response, body = self.get(f"/api/recordings/preview-track?camera_id=cam-1&hour={self.hour}")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["pending"][0]["state"], "pending")
        build_segment_preview(self.root, source)
        response, body = self.get(f"/api/recordings/preview-track?camera_id=cam-1&hour={self.hour}")
        manifest = json.loads(body)
        etag = response.getheader("ETag")
        self.assertEqual(response.getheader("Cache-Control"), "private, no-cache")
        self.assertNotIn("_coarse_files", manifest)
        response, _ = self.get(f"/api/recordings/preview-track?camera_id=cam-1&hour={self.hour}", {"If-None-Match": etag})
        self.assertEqual(response.status, 304, "an unchanged manifest revalidates without a body")
        segment = manifest["segments"][0]
        sheet = f"{segment['base']}000.jpg?v={segment['version']}"
        response, body = self.get(sheet)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
        self.assertEqual(response.getheader("Cache-Control"), "private, max-age=31536000, immutable")
        self.assertTrue(body.startswith(b"\xff\xd8"))
        response, _ = self.get(sheet, {"If-None-Match": response.getheader("ETag")})
        self.assertEqual(response.status, 304)
        response, _ = self.get(f"{segment['base']}000.jpg?v=old")
        self.assertEqual(response.getheader("Cache-Control"), "private, no-cache", "only versioned URLs are immutable")
        response, body = self.get(manifest["coarse"]["url"])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "private, max-age=31536000, immutable")
        self.assertTrue(body.startswith(b"\xff\xd8"))
        # Removing the recording makes its preview unavailable.
        source.unlink()
        self.assertEqual(self.get(sheet)[0].status, 404)

    def test_requests_are_validated_and_authorized(self):
        self.assertEqual(self.get("/api/recordings/preview-track?camera_id=../x&hour=0")[0].status, 400)
        self.assertEqual(self.get(f"/api/recordings/preview-track?camera_id=cam-1&hour={self.hour + 1}")[0].status, 400)
        self.assertEqual(self.get(f"/api/recordings/preview-coarse?camera_id=cam-1&hour={self.hour}")[0].status, 404)
        self.httpd.store.create_user("admin", "correct-horse", "admin")
        self.assertEqual(self.get(f"/api/recordings/preview-track?camera_id=cam-1&hour={self.hour}")[0].status, 401)
        self.assertEqual(self.get("/recordings/.preview-track/cam-1/2026-11-04/06-00-00/000.jpg")[0].status, 401)


if __name__ == "__main__":
    unittest.main()
