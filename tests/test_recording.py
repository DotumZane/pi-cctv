import tempfile
import unittest
import struct
import json
import os
import shutil
import subprocess
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cctv.recording import (RecorderConfig, RecorderSupervisor, SCRUB_ATLAS_BUILD_SLOT, mp4_duration,
                            next_scrub_frame_source, prepare_scrub_frames, scrub_frames_ready,
                            prune_recordings, recording_files, recording_schedule_active, recording_space_available)


class RecordingTests(unittest.TestCase):
    def test_preview_completion_wakes_backfill_but_busy_decoder_does_not(self):
        from concurrent.futures import Future
        supervisor = RecorderSupervisor(None, Path('/unused'))
        try:
            busy = Future()
            busy.set_result(False)
            supervisor._preview_finished(busy)
            self.assertFalse(supervisor._preview_wake.is_set())
            finished = Future()
            finished.set_result(None)
            supervisor._preview_finished(finished)
            self.assertTrue(supervisor._preview_wake.is_set())
        finally:
            supervisor.stop()

    def test_preview_backfill_includes_old_files_and_retries_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = []
            for i in range(30):
                source = root / f'{i:02}.mp4'
                source.touch()
                os.utime(source, (1000 + i, 1000 + i))
                files.append(source)
            active = root / 'active.mp4'
            active.touch()
            os.utime(active, (1999, 1999))
            self.assertEqual(next_scrub_frame_source(root, oldest_first=False, now=2000), files[-1])
            self.assertEqual(next_scrub_frame_source(root, oldest_first=True, now=2000), files[0])
            failure = root / '.scrub-frames' / '00.failed'
            failure.parent.mkdir()
            failure.touch()
            os.utime(failure, (1900, 1900))
            self.assertEqual(next_scrub_frame_source(root, oldest_first=True, now=2000), files[1])
            self.assertEqual(next_scrub_frame_source(root, oldest_first=True, now=2601), files[0])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
    def test_compact_preview_atlas_is_generated_and_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'recording.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc=size=160x90:rate=10:duration=4', '-c:v', 'mpeg4', str(source)],
                           check=True, capture_output=True)
            target = root / '.scrub-frames' / 'recording'
            prepare_scrub_frames(source, target, fps=1)
            self.assertTrue(scrub_frames_ready(source, target))
            manifest = json.loads((target / 'manifest.json').read_text())
            self.assertEqual(manifest['fps'], 1)
            self.assertGreaterEqual(manifest['frames'], 4)
            self.assertTrue((target / '000.jpg').is_file())

    def test_daily_schedule_handles_midnight_and_timezone(self):
        schedule = {"mode": "daily", "start": "22:00", "end": "06:00", "timezone": "America/Los_Angeles"}
        self.assertTrue(recording_schedule_active(schedule, datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)))
        self.assertFalse(recording_schedule_active(schedule, datetime(2026, 1, 2, 20, 0, tzinfo=timezone.utc)))
        self.assertTrue(recording_schedule_active({"mode": "always"}))
        self.assertFalse(recording_schedule_active({"mode": "daily", "timezone": "invalid"}))

    def test_space_reserve_fails_closed_and_stops_at_threshold(self):
        from collections import namedtuple
        usage = namedtuple('usage', 'total used free')
        with patch('cctv.recording.shutil.disk_usage', return_value=usage(100, 90, 5 * 1024**3)):
            self.assertFalse(recording_space_available(Path('/test'), 5))
        with patch('cctv.recording.shutil.disk_usage', side_effect=OSError('unavailable')):
            self.assertFalse(recording_space_available(Path('/test'), 5))
        with patch('cctv.recording.shutil.disk_usage', return_value=usage(100, 90, 6 * 1024**3)):
            self.assertTrue(recording_space_available(Path('/test'), 5))

    def test_busy_preview_worker_does_not_mark_recording_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'recording.mp4'
            source.touch()
            target = root / '.scrub-frames' / 'recording'
            self.assertTrue(SCRUB_ATLAS_BUILD_SLOT.acquire(blocking=False))
            try:
                with patch('cctv.recording.subprocess.run', side_effect=AssertionError('worker should stay idle')):
                    prepare_scrub_frames(source, target)
            finally:
                SCRUB_ATLAS_BUILD_SLOT.release()
            self.assertFalse(target.with_suffix('.failed').exists())

    def test_reads_duration_from_version_one_movie_header(self):
        def box(kind, payload):
            return struct.pack('>I4s', len(payload) + 8, kind) + payload
        mvhd = bytes([1, 0, 0, 0]) + bytes(16) + struct.pack('>IQ', 1000, 123456)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'recording.mp4'
            path.write_bytes(box(b'free', b'') + box(b'mdat', b'video') + box(b'moov', box(b'mvhd', mvhd)))
            self.assertAlmostEqual(mp4_duration(path), 123.456)

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

    def test_pruning_removes_compatible_playback_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'camera' / 'day' / 'video.mp4'
            cached = root / '.playback-cache' / 'camera' / 'day' / 'video.mp4'
            source.parent.mkdir(parents=True)
            cached.parent.mkdir(parents=True)
            source.write_bytes(b'video')
            cached.write_bytes(b'converted')
            old = (datetime.now(timezone.utc) - timedelta(hours=25)).timestamp()
            os.utime(source, (old, old))
            self.assertEqual(prune_recordings(root, 24), 1)
            self.assertFalse(cached.exists())
