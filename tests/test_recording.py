import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cctv.recording import RecorderConfig, prune_recordings


class RecordingTests(unittest.TestCase):
    def test_uses_stream_copy_and_segmented_mkv_output(self):
        command = RecorderConfig("cam-1", "rtsp://media:8554/cam-1", Path("recordings")).ffmpeg_command()
        self.assertIn("-c", command)
        self.assertIn("copy", command)
        self.assertIn("segment", command)
        self.assertTrue(command[-1].endswith("%H-%M-%S.mkv"))

    def test_prunes_only_expired_recordings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.mkv"
            fresh = root / "fresh.mkv"
            old.write_bytes(b"old")
            fresh.write_bytes(b"fresh")
            reference = datetime.now(timezone.utc)
            old_time = (reference - timedelta(hours=25)).timestamp()
            import os
            os.utime(old, (old_time, old_time))
            self.assertEqual(prune_recordings(root, 24, reference), 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
