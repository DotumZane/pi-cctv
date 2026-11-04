# Raspberry Pi Industrial CCTV System

A low-latency, centrally managed industrial CCTV system for Raspberry Pi-based cameras and monitor decoders.

## Project goals

- Reliable 24/7 operation
- Extremely low camera-to-monitor latency
- Central camera and monitor management
- Flexible camera-to-monitor routing and 2×2 layouts
- Centralized recording without adding latency to live viewing
- Secure, repeatable one-command device installation
- Automatic recovery after network, software, or power interruptions

## Planned components

- Camera software for Raspberry Pi camera devices
- Monitor decoder software for Raspberry Pi 4 and Raspberry Pi 5
- Central management server and web dashboard
- Device enrollment and one-command installation tooling
- Centralized recording and health monitoring
- Architecture, deployment, testing, and operations documentation

Implementation will begin after the architecture and technical design are reviewed and approved.

## V1 quick start

The V1 management server uses only Python's standard library and SQLite:

```bash
./start.sh --host 0.0.0.0 --port 8080
```

Open `http://SERVER:8080` for the dashboard. The first V1 API supports device enrollment, heartbeats, camera inventory, monitor assignments, and single-camera or 2×2 assignment validation.

V1 also includes pure media-pipeline builders for the Raspberry Pi hardware path. They use GStreamer, hardware H.264 encode/decode, RTSP, and low-latency queue settings:

```bash
python3 -m cctv.media camera rtsp://MEDIA_SERVER:8554/cam-1
python3 -m cctv.media monitor rtsp://MEDIA_SERVER:8554/cam-1
python3 -m cctv.media quad rtsp://MEDIA_SERVER:8554/cam-1 rtsp://MEDIA_SERVER:8554/cam-2
```

`mediamtx.yml` provides the relay baseline. The management server is deliberately not placed in the live-video path. The exact GStreamer element names and supported decode throughput must be verified on the selected Pi OS image and Pi 4/Pi 5 hardware.

For a Raspberry Pi, the initial installer is intentionally dependency-light:

```bash
sudo bash install.sh
python3 -m cctv.agent --role camera --name "Saw 1" --location "Line 1" --server http://SERVER:8080
```

Use `--role monitor` for a monitor Pi. The agent registers the device and retries heartbeats through temporary network outages. V1 still needs hardware validation, service unit generation, authentication, recording, and production security hardening before deployment.
