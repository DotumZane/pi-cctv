import argparse
import subprocess
import unittest
from unittest.mock import patch

from cctv.timestamp_encoder import command, hardware_available


class TimestampEncoderTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(size="1920x1080", fps=30, bitrate=6000000, gop=30,
                                       profile="main", filter="null")

    def test_probe_checks_real_configuration_and_requires_success(self):
        with patch("cctv.timestamp_encoder.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertTrue(hardware_available(self.args))
            args = run.call_args.args[0]
            self.assertIn("color=size=1920x1080:rate=30", args)
            self.assertIn("h264_v4l2m2m", args)
            self.assertIn("6000000", args)
            run.return_value.returncode = 1
            self.assertFalse(hardware_available(self.args))
            run.side_effect = subprocess.TimeoutExpired("ffmpeg", 10)
            self.assertFalse(hardware_available(self.args))

    def test_encoding_paths_keep_camera_settings(self):
        for hardware, encoder in ((True, "h264_v4l2m2m"), (False, "libx264")):
            args = command(self.args, hardware)
            self.assertIn(encoder, args)
            for value in ("1920x1080", "6000000", "30", "77" if hardware else "main", "null"):
                self.assertIn(value, args)
            self.assertEqual(args[-2:], ["h264", "pipe:1"])

    def test_hardware_profiles_use_h264_numeric_ids(self):
        for name, value in (("baseline", "66"), ("main", "77"), ("high", "100")):
            self.args.profile = name
            args = command(self.args, True)
            self.assertEqual(args[args.index("-profile:v") + 1], value)


if __name__ == "__main__":
    unittest.main()
