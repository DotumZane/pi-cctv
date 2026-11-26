"""Checks for installing a brand-new Pi from its dashboard without internet."""

import hashlib
import importlib.util
import io
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cctv import apt_cache, pi_bootstrap, time_sync
from tests import test_pi_software
from tests.test_pi_software import updater

ROOT = Path(__file__).parents[1]


class DashboardOriginTests(unittest.TestCase):
    def test_uses_the_address_the_pi_connected_to(self):
        self.assertEqual(pi_bootstrap.dashboard_origin("192.168.10.10:8080", False),
                         ("http://192.168.10.10:8080", "192.168.10.10"))
        self.assertEqual(pi_bootstrap.dashboard_origin("cctv-server.lan", True),
                         ("https://cctv-server.lan", "cctv-server.lan"))

    def test_rejects_unusable_or_unsafe_hosts(self):
        for host in ("", "localhost:8080", "127.0.0.1:8080", "[::1]:8080", "a b:80", "x;rm -rf /:80",
                     "server:99999", "server:8o", "$(id):80"):
            with self.subTest(host=host):
                self.assertIsNone(pi_bootstrap.dashboard_origin(host, False))


class BootstrapScriptTests(unittest.TestCase):
    def setUp(self):
        self.script = pi_bootstrap.script("http://192.168.10.10:8080", "192.168.10.10")

    def test_is_valid_bash(self):
        subprocess.run(["bash", "-n"], input=self.script, text=True, check=True)

    def test_writes_the_same_cache_and_clock_settings_as_the_agent(self):
        self.assertIn(apt_cache.DETECT_SCRIPT, self.script)
        self.assertIn(apt_cache.APT_CONF, self.script)
        self.assertIn(time_sync.config_text("192.168.10.10"), self.script)
        self.assertIn("install.sh\" --discover --skip-download", self.script)
        self.assertTrue(self.script.rstrip().endswith('main "$@"'))

    def test_quotes_values_for_bash(self):
        script = pi_bootstrap.script("http://server:8080", "server", "https://example.invalid/a b.git")
        self.assertIn("REPO_URL='https://example.invalid/a b.git'", script)


class BootstrapDownloadTests(test_pi_software.PiSoftwareFromDashboardTests):
    """Run the installer's embedded downloader against a real dashboard."""

    def downloader(self) -> str:
        script = pi_bootstrap.script("http://127.0.0.1", "127.0.0.1")
        return re.search(r"<<'PI_CCTV_PY'\n(.*?)\nPI_CCTV_PY\n", script, re.DOTALL).group(1)

    def download(self, target: Path):
        return subprocess.run(["python3", "-", f"http://127.0.0.1:{self.httpd.server_port}", str(target)],
                              input=self.downloader(), capture_output=True, text=True, timeout=60)

    def test_downloads_and_verifies_the_dashboards_software(self):
        updater.publish_software(self.server_repo)
        target = Path(self.tmp.name) / "download.bundle"
        result = self.download(target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.new_commit)
        new_pi = Path(self.tmp.name) / "new-pi"
        subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(new_pi)], check=True)
        subprocess.run(["git", "-C", str(new_pi), "fetch", "-q", "--no-tags", str(target), "HEAD"], check=True)
        subprocess.run(["git", "-C", str(new_pi), "checkout", "-q", "-B", "main", self.new_commit], check=True)
        self.assertEqual((new_pi / "cctv" / "__init__.py").read_text(),
                         (self.server_repo / "cctv" / "__init__.py").read_text())

    def test_explains_a_dashboard_without_software(self):
        result = self.download(Path(self.tmp.name) / "download.bundle")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no Pi software to install", result.stderr)

    def test_dashboard_serves_the_installer_and_relay_mirror(self):
        base = f"http://127.0.0.1:{self.httpd.server_port}"
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(f"{base}/install", timeout=10)
        self.assertEqual(rejected.exception.code, 400)  # 127.0.0.1 is not an address a Pi can use
        request = urllib.request.Request(f"{base}/install", headers={"Host": "192.168.10.10:8080"})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertIn("SERVER=http://192.168.10.10:8080", response.read().decode())

        archive = "mediamtx_v1.21.3_linux_arm64.tar.gz"
        for name in (archive, "..%2Fpi-cctv.json", "other.tar.gz"):
            with self.subTest(name=name), self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(f"{base}/software/mediamtx/{name}", timeout=10)
            self.assertEqual(missing.exception.code, 404)
        (self.host_dir / "software" / "mediamtx").mkdir(parents=True)
        (self.host_dir / "software" / "mediamtx" / archive).write_bytes(b"relay")
        with urllib.request.urlopen(f"{base}/software/mediamtx/{archive}", timeout=10) as response:
            self.assertEqual(response.read(), b"relay")


class RelayMirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.software = Path(self.tmp.name) / "software"
        patcher = patch.object(updater, "SOFTWARE_DIR", self.software)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        self.payload = b"pretend relay archive"
        digest = hashlib.sha256(self.payload).hexdigest()
        (self.repo / "scripts" / "install-camera-relay.sh").write_text(
            f"version=1.2.3\ncase x in\n  aarch64) arch=arm64; checksum={digest} ;;\nesac\n")

    def opener(self, payload):
        calls = []

        def open_url(url, timeout):
            calls.append(url)
            return io.BytesIO(payload)
        return open_url, calls

    def test_reads_the_real_pinned_release(self):
        version, archives = updater.relay_releases(ROOT)
        self.assertRegex(version, r"^[0-9.]+$")
        self.assertIn(f"mediamtx_v{version}_linux_arm64.tar.gz", archives)
        self.assertEqual(len(archives), 3)

    def test_downloads_verified_archives_once_and_removes_old_ones(self):
        mirror = self.software / "mediamtx"
        mirror.mkdir(parents=True)
        (mirror / "mediamtx_v1.0.0_linux_arm64.tar.gz").write_bytes(b"old")
        opener, calls = self.opener(self.payload)
        self.assertEqual(updater.mirror_relay(self.repo, opener), ["mediamtx_v1.2.3_linux_arm64.tar.gz"])
        self.assertEqual(calls, ["https://github.com/bluenviron/mediamtx/releases/download/v1.2.3/"
                                 "mediamtx_v1.2.3_linux_arm64.tar.gz"])
        self.assertEqual((mirror / "mediamtx_v1.2.3_linux_arm64.tar.gz").read_bytes(), self.payload)
        self.assertFalse((mirror / "mediamtx_v1.0.0_linux_arm64.tar.gz").exists())
        updater.mirror_relay(self.repo, opener)
        self.assertEqual(len(calls), 1)

    def test_rejects_a_download_with_the_wrong_checksum(self):
        opener, _ = self.opener(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            updater.mirror_relay(self.repo, opener)
        self.assertEqual(list((self.software / "mediamtx").iterdir()), [])


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class InstallerFlagTests(unittest.TestCase):
    def test_install_scripts_are_valid_bash(self):
        for script in ("install.sh", "scripts/install-camera-relay.sh"):
            with self.subTest(script=script):
                subprocess.run(["bash", "-n", str(ROOT / script)], check=True)


if __name__ == "__main__":
    unittest.main()
