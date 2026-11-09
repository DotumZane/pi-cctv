import unittest

from cctv.media import CameraMedia, MonitorMedia, quad_pipeline


class MediaTests(unittest.TestCase):
    def test_camera_pipeline_uses_native_hardware_encoder_and_rtsp_publish(self):
        command = CameraMedia("rtsp://media:8554/cam-1").pipeline()
        self.assertIn("rpicam-vid -n -t 0 --inline --codec h264 -o -", command)
        self.assertIn("fdsrc fd=0 do-timestamp=true ! h264parse", command)
        self.assertIn("latency=0", command)
        self.assertIn("rtspclientsink", command)
        self.assertIn("h264parse config-interval=1 ! rtspclientsink", command)
        self.assertNotIn("rtph264pay", command)
        self.assertNotIn("v4l2h264enc", command)

    def test_monitor_pipeline_uses_hardware_decode(self):
        command = MonitorMedia("rtsp://media:8554/cam-1").pipeline()
        self.assertIn("v4l2h264dec", command)
        self.assertIn("drop-on-latency=true", command)

    def test_monitor_fits_video_to_detected_display_aspect(self):
        command = MonitorMedia("rtsp://media:8554/cam-1", width=800, height=480).pipeline()
        self.assertIn("videoscale add-borders=true ! video/x-raw,width=800,height=480", command)

    def test_monitor_fill_crops_to_detected_display_aspect(self):
        command = MonitorMedia("rtsp://media:8554/cam-1", width=1024, height=768, scale_mode="fill").pipeline()
        self.assertIn("aspectratiocrop aspect-ratio=1024/768", command)
        self.assertIn("videoscale add-borders=false", command)

    def test_quad_requires_at_most_four_streams(self):
        command = quad_pipeline(["rtsp://media:8554/cam-1", "rtsp://media:8554/cam-2"])
        self.assertIn("sink_0::xpos=0 sink_0::ypos=0", command)
        self.assertIn("sink_1::xpos=960 sink_1::ypos=0", command)
        with self.assertRaises(ValueError):
            quad_pipeline([])
        with self.assertRaises(ValueError):
            quad_pipeline(["rtsp://x"] * 5)

    def test_quad_layout_uses_detected_output_dimensions(self):
        command = quad_pipeline(["rtsp://media:8554/cam-1", "rtsp://media:8554/cam-2"], width=1280, height=720)
        self.assertIn("sink_1::xpos=640 sink_1::ypos=0", command)
        self.assertIn("video/x-raw,width=1280,height=720 ! tee name=monitor_out", command)
        self.assertIn("monitor_out. ! queue max-size-buffers=2 leaky=downstream ! kmssink", command)
