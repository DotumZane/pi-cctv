"""End-to-end checks for Pis updating from their dashboard instead of GitHub."""

import importlib.util
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cctv import __agent_version__, __version__, update
from cctv.server import Handler
from cctv.store import Store


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "pi-cctv-server-update.py"
SPEC = importlib.util.spec_from_file_location("pi_cctv_server_update_software", MODULE_PATH)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-C", str(repo), *arguments],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


class PiSoftwareFromDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.server_repo = root / "server"
        self.server_repo.mkdir()
        git(self.server_repo, "init", "-q", "-b", "main")
        (self.server_repo / "docker-compose.yml").write_text("services: {}\n")
        (self.server_repo / "cctv").mkdir()
        self.write_version("0.0.1")
        git(self.server_repo, "add", ".")
        git(self.server_repo, "commit", "-q", "-m", "old")
        self.old_commit = git(self.server_repo, "rev-parse", "HEAD")
        self.write_version(__version__)
        git(self.server_repo, "commit", "-q", "-am", "current")
        self.new_commit = git(self.server_repo, "rev-parse", "HEAD")

        self.pi_repo = root / "pi"
        subprocess.run(["git", "clone", "-q", str(self.server_repo), str(self.pi_repo)], check=True)
        git(self.pi_repo, "reset", "-q", "--hard", self.old_commit)

        self.host_dir = root / "host-update"
        self.host_dir.mkdir()
        self.software = patch.object(updater, "SOFTWARE_DIR", self.host_dir / "software")
        self.software.start()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.store = Store(":memory:")
        self.httpd.host_update_dir = str(self.host_dir)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.device_state = root / "device.json"
        self.device_state.write_text(json.dumps({"server": f"http://127.0.0.1:{self.httpd.server_port}"}))
        self.pi = patch.multiple(update, INSTALL_DIR=self.pi_repo, DEVICE_STATE=self.device_state)
        self.pi.start()

    def tearDown(self):
        self.pi.stop()
        self.software.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.store.close()
        self.tmp.cleanup()

    def write_version(self, version: str) -> None:
        (self.server_repo / "cctv" / "__init__.py").write_text(
            f'__version__ = "{version}"\n__agent_version__ = "{__agent_version__}"\n'
        )

    def test_pi_installs_the_dashboards_exact_commit_without_github(self):
        manifest = updater.publish_software(self.server_repo)
        self.assertEqual(manifest["commit"], self.new_commit)
        self.assertEqual(manifest["agent_version"], __agent_version__)
        git(self.pi_repo, "remote", "set-url", "origin", "https://github.invalid/unreachable.git")

        update.download_software()

        self.assertEqual(git(self.pi_repo, "rev-parse", "HEAD"), self.new_commit)
        self.assertEqual(list((self.host_dir / "software").glob("*.bundle")), [self.host_dir / "software" / manifest["bundle"]])

    def test_newer_pi_is_never_downgraded_to_the_dashboard_version(self):
        updater.publish_software(self.server_repo)
        git(self.pi_repo, "reset", "-q", "--hard", self.new_commit)
        (self.pi_repo / "newer.txt").write_text("newer\n")
        git(self.pi_repo, "add", "newer.txt")
        git(self.pi_repo, "commit", "-q", "-m", "newer")
        newer = git(self.pi_repo, "rev-parse", "HEAD")

        update.download_software()

        self.assertEqual(git(self.pi_repo, "rev-parse", "HEAD"), newer)

    def test_tampered_package_is_rejected_and_checkout_is_unchanged(self):
        manifest = updater.publish_software(self.server_repo)
        bundle = self.host_dir / "software" / manifest["bundle"]
        data = bytearray(bundle.read_bytes())
        data[-1] ^= 0xFF
        bundle.write_bytes(bytes(data))

        with self.assertRaisesRegex(RuntimeError, "integrity"):
            update.download_software()
        self.assertEqual(git(self.pi_repo, "rev-parse", "HEAD"), self.old_commit)

    def test_out_of_date_package_is_not_offered_and_pi_falls_back_to_github(self):
        updater.publish_software(self.server_repo)
        manifest_path = self.host_dir / "software" / "pi-cctv.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "0.0.1"
        manifest_path.write_text(json.dumps(manifest))

        with patch.object(update, "git", return_value=subprocess.CompletedProcess([], 0, "", "")) as pull:
            update.download_software()
        pull.assert_called_once_with("pull", "--ff-only", "origin", "main", timeout=180)

    def test_dashboard_without_package_reports_not_found(self):
        self.assertIsNone(update.download_server_software(json.loads(self.device_state.read_text())["server"]))


class ServerReleaseBranchTests(unittest.TestCase):
    def test_older_configs_follow_main(self):
        self.assertEqual(updater.configured_branch({"project_dir": "/srv/pi-cctv"}), "main")

    def test_configured_branch_is_used(self):
        self.assertEqual(updater.configured_branch({"branch": "stable"}), "stable")

    def test_unsafe_branch_names_are_rejected(self):
        for branch in ("-x", "../main", "main;rm", "", 7):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                updater.configured_branch({"branch": branch})


if __name__ == "__main__":
    unittest.main()
