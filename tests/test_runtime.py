import unittest

from cctv.runtime import command_for_assignment


class RuntimeTests(unittest.TestCase):
    def test_empty_assignment_stops_monitor_pipeline(self):
        self.assertIsNone(command_for_assignment({"layout": "single", "cameras": "[]"}, "media"))

    def test_single_assignment_targets_camera_path(self):
        command = command_for_assignment({"layout": "single", "cameras": '["cam-7"]'}, "media")
        self.assertIn("rtsp://media:8554/cam-7", command)

    def test_quad_assignment_builds_four_stream_layout(self):
        command = command_for_assignment({"layout": "2x2", "cameras": '["a", "b", "c", "d"]'}, "media")
        self.assertIn("rtsp://media:8554/a", command)
        self.assertIn("sink_3::xpos=960 sink_3::ypos=540", command)
