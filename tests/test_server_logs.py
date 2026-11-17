import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cctv.server_logs import SafeRotatingFileHandler, capture_server_output


class ServerLogTests(unittest.TestCase):
    def test_disk_full_does_not_recurse_into_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = SafeRotatingFileHandler(Path(directory) / "server.log")
            try:
                with patch.object(handler, "shouldRollover", side_effect=OSError(28, "No space left on device")), patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    handler.handle(logging.LogRecord("test", logging.ERROR, "", 0, "error", (), None))
                    self.assertEqual(stderr.getvalue(), "")
            finally:
                handler.close()

    def test_server_can_start_if_log_file_cannot_be_opened(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("cctv.server_logs.SafeRotatingFileHandler", side_effect=OSError(28, "No space left on device")), patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertIsNone(capture_server_output(Path(directory) / "server.log"))
                self.assertIn("Server log file unavailable", stderr.getvalue())
