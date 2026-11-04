import unittest

from cctv.runtime import command_for_assignment, commands_for_assignment


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

    def test_two_display_assignment_builds_one_pipeline_per_output(self):
        commands = commands_for_assignment({"outputs": '[{"cameras":["a"],"connector_id":"32"},{"cameras":["b"],"connector_id":"33"}]'}, "media")
        self.assertEqual(len(commands), 2)
        self.assertIn("rtsp://media:8554/a", commands[0])
        self.assertIn("connector-id=32", commands[0])
        self.assertIn("rtsp://media:8554/b", commands[1])
