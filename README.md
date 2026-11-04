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

For a Raspberry Pi, the initial installer is intentionally dependency-light:

```bash
sudo bash install.sh
python3 -m cctv.agent --role camera --name "Saw 1" --location "Line 1" --server http://SERVER:8080
```

Use `--role monitor` for a monitor Pi. The agent registers the device and retries heartbeats through temporary network outages. V1 does not yet include live H.264 capture/playback, authentication, systemd service generation, recording, or production security hardening; those are subsequent milestones.
