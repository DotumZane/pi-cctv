import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cctv import apt_cache
from cctv.runtime import DeviceRuntime


class PackageCacheConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.conf_dir = root / "apt.conf.d"
        self.conf_dir.mkdir()
        self.host_file = root / "etc" / "apt-cache-host"
        self.helper = root / "lib" / "apt-cache-detect"

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, server):
        return apt_cache.configure(server, self.conf_dir, self.host_file, self.helper)

    def test_points_apt_at_the_dashboard_host_through_the_detect_helper(self):
        self.assertTrue(self.configure("https://cctv-server.lan:8443"))
        self.assertEqual(self.host_file.read_text(), "cctv-server.lan\n")
        conf = (self.conf_dir / apt_cache.APT_CONF_NAME).read_text()
        self.assertEqual(conf, f'Acquire::http::Proxy-Auto-Detect "{self.helper}";\n')
        self.assertEqual(self.helper.stat().st_mode & 0o777, 0o755)
        self.assertIn(str(self.host_file), self.helper.read_text())

    def test_unchanged_settings_are_not_rewritten(self):
        self.assertTrue(self.configure("https://192.168.10.10:8443"))
        self.assertFalse(self.configure("https://192.168.10.10:8443"))
        self.assertTrue(self.configure("https://192.168.10.20:8443"))
        self.assertEqual(self.host_file.read_text(), "192.168.10.20\n")

    def test_skips_machines_without_apt_and_unusable_hosts(self):
        self.assertFalse(apt_cache.configure("https://server", self.conf_dir / "missing", self.host_file, self.helper))
        for server in ("http://127.0.0.1:8080", "http://localhost:8080", "not a url", "https://[::1]:8443"):
            with self.subTest(server=server):
                self.assertFalse(self.configure(server))
        self.assertFalse(self.host_file.exists())

    def test_agent_startup_failure_to_configure_does_not_raise(self):
        runtime = DeviceRuntime("camera", "cam-1", "Camera", "", "https://server", "media")
        with patch.object(apt_cache, "configure", side_effect=PermissionError("read-only")):
            runtime.configure_package_cache()


@unittest.skipUnless(shutil.which("bash") and shutil.which("timeout"), "needs bash and timeout")
class DetectHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.host_file = root / "host"
        self.helper = root / "detect"
        self.helper.write_text(apt_cache.DETECT_SCRIPT.replace(str(apt_cache.HOST_FILE), str(self.host_file)))

    def tearDown(self):
        self.tmp.cleanup()

    def detect(self):
        return subprocess.run(["bash", str(self.helper)], capture_output=True, text=True, timeout=10).stdout.strip()

    def test_connects_directly_without_a_host_or_when_the_cache_does_not_answer(self):
        self.assertEqual(self.detect(), "DIRECT")
        self.host_file.write_text("bad;host\n")
        self.assertEqual(self.detect(), "DIRECT")
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", apt_cache.CACHE_PORT)) == 0:
                self.skipTest("port 3142 is in use on this machine")
        self.host_file.write_text("127.0.0.1\n")
        self.assertEqual(self.detect(), "DIRECT")

    def test_uses_the_cache_when_it_answers(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", apt_cache.CACHE_PORT))
            except OSError:
                self.skipTest("port 3142 is in use on this machine")
            listener.listen(1)
            self.host_file.write_text("127.0.0.1\n")
            self.assertEqual(self.detect(), f"http://127.0.0.1:{apt_cache.CACHE_PORT}")


if __name__ == "__main__":
    unittest.main()
