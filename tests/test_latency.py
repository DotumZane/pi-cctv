import unittest

from cctv.latency import test_pattern_pipeline


class LatencyTests(unittest.TestCase):
    def test_pattern_pipeline_has_timestamp_and_zero_buffer_settings(self):
        command = test_pattern_pipeline("rtsp://media:8554/latency-test")
        self.assertIn("timeoverlay", command)
        self.assertIn("latency=0", command)
        self.assertIn("rtspclientsink", command)
        self.assertNotIn("rtph264pay", command)
