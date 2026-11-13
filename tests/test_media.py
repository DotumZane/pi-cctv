import unittest

from cctv.media import CameraMedia, MonitorMedia, idle_monitor_pipeline, quad_pipeline


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

    def test_camera_controls_are_applied_to_rpicam_encoder(self):
        command = CameraMedia(
            "rtsp://media:8554/cam-1", sharpness=0.5, exposure_mode="manual",
            shutter_us=8000, gain=2.5, exposure_profile="sport",
            white_balance="daylight", keyframe_interval=45, h264_profile="high",
        ).pipeline()
        for option in (
            "--sharpness 0.5", "--shutter 8000", "--gain 2.5", "--exposure sport",
            "--awb daylight", "--intra 45", "--profile high",
        ):
            self.assertIn(option, command)

    def test_automatic_exposure_does_not_force_manual_shutter_or_gain(self):
        command = CameraMedia("rtsp://media:8554/cam-1", exposure_mode="auto").pipeline()
        self.assertNotIn("--shutter", command)
        self.assertNotIn("--gain", command)

    def test_monitor_pipeline_uses_hardware_decode(self):
        command = MonitorMedia("rtsp://media:8554/cam-1").pipeline()
        self.assertIn("v4l2h264dec", command)
        self.assertIn("drop-on-latency=true", command)
        self.assertIn('textoverlay name=cctv_stats text="" silent=true', command)
        self.assertIn("input-selector name=cctv_selector_0", command)
        self.assertIn("cctv_selector_0.sink_0", command)
        self.assertIn("cctv_selector_0.sink_1", command)
        self.assertIn("cctv_splash_selector", command)
        self.assertIn("rsg-monitor-splash.tga", command)
        self.assertIn("video/x-raw,width=1920,height=1080,framerate=10/1", command)
        self.assertIn("identity name=cctv_live_0", command)
        self.assertIn("Camera offline", command)
        self.assertIn("cam-1", command)

    def test_idle_monitor_has_neutral_slate_and_preview(self):
        command = idle_monitor_pipeline(preview_id="monitor-1", connector_id="42", width=1024, height=768)
        self.assertIn("No camera assigned", command)
        self.assertIn("connector-id=42", command)
        self.assertIn("pi-cctv-monitor-preview-monitor-1", command)
        self.assertIn("video/x-raw,width=1024,height=768,framerate=10/1", command)

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
        for index in range(2):
            self.assertIn(f"input-selector name=cctv_selector_{index}", command)
            self.assertIn(f"textoverlay name=cctv_offline_{index}", command)
            self.assertIn(f"identity name=cctv_live_{index}", command)
            self.assertIn(f"rtspsrc name=cctv_source_{index}", command)
        with self.assertRaises(ValueError):
            quad_pipeline([])
        with self.assertRaises(ValueError):
            quad_pipeline(["rtsp://x"] * 5)

    def test_quad_layout_uses_detected_output_dimensions(self):
        command = quad_pipeline(["rtsp://media:8554/cam-1", "rtsp://media:8554/cam-2"], width=1280, height=720)
        self.assertIn("sink_1::xpos=640 sink_1::ypos=0", command)
        self.assertIn("video/x-raw,width=1280,height=720 ! textoverlay name=cctv_stats", command)
        self.assertIn("tee name=monitor_out", command)
        self.assertIn('textoverlay name=cctv_stats text="" silent=true', command)
        self.assertIn("monitor_out. ! queue max-size-buffers=2 leaky=downstream ! kmssink", command)
