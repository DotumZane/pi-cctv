import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cctv.recording import RecorderConfig, prune_recordings, recording_files


class RecordingTests(unittest.TestCase):
    def test_recording_scan_skips_preview_cache_directories(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recording = root / 'camera' / 'day' / 'video.mp4'
            cache = root / '.scrub-frames' / 'camera' / 'day' / 'video'
            recording.parent.mkdir(parents=True)
            cache.mkdir(parents=True)
            recording.touch()
            (cache / 'hidden.mp4').touch()
            visited = []
            scandir = os.scandir
            def track(path):
                visited.append(str(path))
                return scandir(path)
            with patch('cctv.recording.os.scandir', side_effect=track):
                self.assertEqual(recording_files(root), [recording])
            self.assertFalse(any('.scrub-frames' in path for path in visited))

    def test_uses_stream_copy_and_segmented_mp4_output(self):
        command = RecorderConfig("cam-1", "rtsp://media:8554/cam-1", Path("recordings")).ffmpeg_command()
        self.assertIn("-c", command)
        self.assertIn("copy", command)
        self.assertIn("segment", command)
        self.assertEqual(command[command.index("-segment_format_options") + 1], "movflags=+faststart")
        self.assertTrue(command[-1].endswith("%H-%M-%S.mp4"))

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
