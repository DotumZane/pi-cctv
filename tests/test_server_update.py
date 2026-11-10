import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "pi-cctv-server-update.py"
SPEC = importlib.util.spec_from_file_location("pi_cctv_server_update", MODULE_PATH)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


class ServerUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        (self.repo / "docker-compose.yml").write_text("services: {}\n")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"project_dir": str(self.repo)}))
        self.request_id = "a" * 32
        (self.state / "request.json").write_text(json.dumps({"id": self.request_id, "action": "update"}))
        self.constants = patch.multiple(
            updater,
            STATE_DIR=self.state,
            CONFIG=self.config,
            REQUEST=self.state / "request.json",
            STATUS=self.state / "status.json",
            LOCK=self.root / "lock",
        )
        self.constants.start()

    def tearDown(self):
        self.constants.stop()
        self.tmp.cleanup()

    def test_update_uses_only_fast_forward_git_and_compose_commands(self):
        responses = [
            (0, ""), (0, "main"), (0, "Fetched"), (0, "a" * 40),
            (0, "b" * 40), (0, "Fast-forward"), (0, "Rebuilt"), (0, "b" * 12),
        ]
        with patch.object(updater, "run", side_effect=responses) as run:
            self.assertEqual(updater.main(), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["git", "merge", "--ff-only", "origin/main"], commands)
        self.assertIn(["docker", "compose", "up", "-d", "--build", "management"], commands)
        self.assertTrue(all(isinstance(command, list) for command in commands))
        self.assertFalse((self.state / "request.json").exists())
        status = json.loads((self.state / "status.json").read_text())
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["updated_commit"], "b" * 12)

    def test_refuses_dirty_checkout_without_running_compose(self):
        with patch.object(updater, "run", side_effect=[(0, " M README.md")]) as run:
            self.assertEqual(updater.main(), 1)
        self.assertEqual(run.call_count, 1)
        status = json.loads((self.state / "status.json").read_text())
        self.assertEqual(status["state"], "failed")
        self.assertIn("local changes", status["error"])
