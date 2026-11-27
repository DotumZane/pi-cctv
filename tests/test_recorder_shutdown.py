import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cctv.recording import RecorderConfig, RecorderSupervisor, mp4_duration

ROOT = Path(__file__).resolve().parents[1]


def synthetic_command(self):
    """The production segment/MP4 options, fed by a synthetic camera."""
    output = self.output_dir / self.camera_id / "%Y-%m-%d" / "%H-%M-%S.mp4"
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re", "-f", "lavfi",
            "-i", "testsrc2=size=320x180:rate=15", "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
            "-f", "segment", "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
            "-segment_time", str(self.segment_seconds), "-strftime", "1", "-reset_timestamps", "1", str(output)]


class OneCamera:
    def devices(self, kind):
        return [{"id": "cam-1", "status": "online"}] if kind == "camera" else []


def segments(root: Path) -> list[Path]:
    return sorted((root / "cam-1").rglob("*.mp4"))


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class RecorderShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_stopping_the_supervisor_finishes_the_open_segment(self):
        with patch.object(RecorderConfig, "ffmpeg_command", synthetic_command):
            supervisor = RecorderSupervisor(OneCamera(), self.root, poll_seconds=0.2)
            supervisor.start()
            self.assertTrue(wait_for(lambda: segments(self.root) and segments(self.root)[0].stat().st_size > 20000))
            time.sleep(2)
            started = time.monotonic()
            supervisor.stop()
            self.assertLess(time.monotonic() - started, 6, "recorders stop well within Docker's grace period")
        recording = segments(self.root)[-1]
        self.assertIsNotNone(mp4_duration(recording), "the interrupted segment is playable (has its moov index)")
        self.assertGreater(mp4_duration(recording), 1)

    def test_a_killed_recorder_leaves_the_segment_unplayable(self):
        # Shows the failure the clean stop prevents.
        process = subprocess.Popen(synthetic_command(RecorderConfig("cam-1", "", self.root)),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.root / "cam-1" / time.strftime("%Y-%m-%d")).mkdir(parents=True, exist_ok=True)
        try:
            self.assertTrue(wait_for(lambda: segments(self.root) and segments(self.root)[0].stat().st_size > 20000))
        finally:
            process.kill()
            process.wait()
        self.assertIsNone(mp4_duration(segments(self.root)[-1]))

    def test_a_failing_recorder_backs_off(self):
        starts = []
        def failing(self):
            starts.append(time.monotonic())
            return ["false"]
        with patch.object(RecorderConfig, "ffmpeg_command", failing), patch("builtins.print"):
            supervisor = RecorderSupervisor(OneCamera(), self.root, poll_seconds=0.1)
            supervisor.start()
            time.sleep(7.5)
            supervisor.stop()
        # Relaunching on every 0.1 s loop would mean ~70 starts; the back-off
        # waits 2, 4, then 8 seconds.
        self.assertLessEqual(len(starts), 4, starts)
        self.assertGreaterEqual(len(starts), 2)
        gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
        self.assertGreaterEqual(gaps[0], 1.9)


@unittest.skipUnless(shutil.which("ffmpeg") and os.name == "posix", "FFmpeg on POSIX is required")
class ServerSigtermTests(unittest.TestCase):
    def test_sigterm_shuts_the_server_down_with_playable_recordings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launcher = root / "launch.py"
            launcher.write_text(textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(ROOT)!r})
                sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
                from unittest.mock import patch
                from cctv.recording import RecorderConfig
                from cctv.store import Store
                from test_recorder_shutdown import synthetic_command
                store = Store({str(root / 'cctv.db')!r})
                store.register({{"id": "cam-1", "kind": "camera", "name": "Camera"}})
                store.close()
                with patch.object(RecorderConfig, "ffmpeg_command", synthetic_command):
                    from cctv import server
                    sys.argv = ["server", "--host", "127.0.0.1", "--port", "0", "--db", {str(root / 'cctv.db')!r},
                                "--recordings-dir", {str(root / 'recordings')!r}]
                    server.main()
            """))
            env = {**os.environ, "CCTV_ADMIN_TOKEN": "", "CCTV_READ_TOKEN": ""}
            process = subprocess.Popen([sys.executable, str(launcher)], cwd=temp, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                recordings = root / "recordings"
                self.assertTrue(wait_for(lambda: segments(recordings) and segments(recordings)[0].stat().st_size > 20000, 15))
                time.sleep(2)
                started = time.monotonic()
                process.send_signal(signal.SIGTERM)
                output, _ = process.communicate(timeout=15)
            finally:
                if process.poll() is None:
                    process.kill()
            self.assertLess(time.monotonic() - started, 8, output)
            self.assertIn("NVR recorders stopped", output)
            self.assertIsNotNone(mp4_duration(segments(recordings)[-1]), "SIGTERM leaves the open segment playable")


if __name__ == "__main__":
    unittest.main()
