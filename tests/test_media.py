import unittest

from cctv.media import CameraMedia, MonitorMedia, quad_pipeline


class MediaTests(unittest.TestCase):
    def test_camera_pipeline_is_low_latency_and_hardware_encoded(self):
        command = CameraMedia("rtsp://media:8554/cam-1").pipeline()
        self.assertIn("v4l2h264enc", command)
        self.assertIn("latency=0", command)
        self.assertIn("rtspclientsink", command)

    def test_monitor_pipeline_uses_hardware_decode(self):
        command = MonitorMedia("rtsp://media:8554/cam-1").pipeline()
        self.assertIn("v4l2h264dec", command)
        self.assertIn("drop-on-latency=true", command)

    def test_quad_requires_at_most_four_streams(self):
        command = quad_pipeline(["rtsp://media:8554/cam-1", "rtsp://media:8554/cam-2"])
        self.assertIn("sink_0::xpos=0 sink_0::ypos=0", command)
        self.assertIn("sink_1::xpos=960 sink_1::ypos=0", command)
        with self.assertRaises(ValueError):
            quad_pipeline([])
        with self.assertRaises(ValueError):
            quad_pipeline(["rtsp://x"] * 5)
