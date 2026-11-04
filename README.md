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

### Central server

With Docker Compose installed, bootstrap the management API and RTSP relay with:

```bash
cp .env.example .env
# edit .env and replace the example token
docker compose up -d --build
```

The dashboard is then available at `http://SERVER:8080`. Keep the management port restricted to the authorized administration network; the RTSP port must be reachable by cameras and monitors on the CCTV VLAN. `CCTV_ADMIN_TOKEN` can change configuration, while `CCTV_READ_TOKEN` is read-only.

For adoption-based provisioning, `CCTV_BOOTSTRAP_TOKEN` is used only to place generic Pis into the pending-device list. It is not an administrator token. After adoption, the Pi receives a device-specific credential and the dashboard-selected role. Set `CCTV_PUBLIC_MEDIA_URL` and `CCTV_PUBLIC_WEBRTC_URL` to URLs reachable from the administrator's browser for live previews.

The V1 management server uses only Python's standard library and SQLite:

```bash
./start.sh --host 0.0.0.0 --port 8080
```

Open `http://SERVER:8080` for the dashboard. The first V1 API supports device enrollment, heartbeats, camera inventory, monitor assignments, and single-camera or 2×2 assignment validation.

Run the local quality gate with `make check`. The same test and compile checks run automatically in GitHub Actions for pushes and pull requests.

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

On Raspberry Pi OS/Debian, the installer also installs the required GStreamer, libcamera, and plugin packages. Use `--skip-packages` only when those dependencies have already been installed.

For a fresh Pi directly from the repository, the complete one-command form is:

```bash
curl -fsSL https://raw.githubusercontent.com/DotumZane/pi-cctv/main/install.sh | \
  sudo bash -s -- --role camera --name "Saw 1" --location "Line 1" \
  --server http://SERVER:8080 --media-server MEDIA_SERVER --token ADMIN_TOKEN
```

To install a generic Pi that waits for dashboard adoption instead of choosing a role during installation:

```bash
curl -fsSL https://raw.githubusercontent.com/DotumZane/pi-cctv/main/install.sh | \
  sudo bash -s -- --enroll --server http://SERVER:8080 \
  --media-server MEDIA_SERVER --bootstrap-token BOOTSTRAP_TOKEN
```

The dashboard will show the Pi under **Pending devices**. Choose Camera or Monitor, set its name/location, and click **Adopt**.

To install and start a boot service in one command, provide the device role and endpoints:

```bash
sudo bash install.sh --role camera --name "Saw 1" --location "Line 1" \
  --server http://SERVER:8080 --media-server MEDIA_SERVER --token ADMIN_TOKEN
```

The installer creates and enables `pi-cctv-agent.service`, which restarts automatically after failures and starts after network-online.

Use `--role monitor` for a monitor Pi. The agent registers the device and retries heartbeats through temporary network outages. Add `--run-media --media-server MEDIA_SERVER` to launch and recover the camera or monitor media process automatically:

```bash
python3 -m cctv.agent --role camera --name "Saw 1" --location "Line 1" \
  --server http://SERVER:8080 --media-server MEDIA_SERVER --run-media
```

The monitor runtime polls its centrally stored assignment and restarts its pipeline when the assignment changes or the process exits. The server supports optional bearer-token authentication through `CCTV_ADMIN_TOKEN` or `--admin-token`. V1 still needs hardware validation, recording, and further production security hardening before deployment.

If `--id` is omitted, the agent uses the Pi hostname so the identity remains stable across reboot and service restart.

Pi heartbeats include available CPU temperature, one-minute load, memory-use percentage, and uptime metrics. Settings and health metadata are merged so recurring heartbeats cannot erase administrator configuration.

The centralized recorder uses FFmpeg stream copy and segmented files, so it does not transcode live video:

```bash
python3 -m cctv.recording --camera-id cam-1 \
  --stream-url rtsp://MEDIA_SERVER:8554/cam-1 \
  --output-dir /var/lib/pi-cctv/recordings --retention-hours 24
```

Files are organized by camera and date, and old segments are deleted automatically. Recording throughput and storage capacity must be sized and tested for the final camera bitrate and retention period.

Retained segments can be indexed with `GET /api/recordings?camera_id=cam-01` and downloaded from `/recordings/<relative-path>` using a read-authorized client.

Administrative mutations are written to the SQLite audit log. Authorized clients can inspect the most recent entries with `GET /api/audit?limit=100`.

Device lifecycle is also available through the authenticated API: `POST /api/devices` enrolls or updates a device, and `DELETE /api/devices/{id}` removes it and any saved monitor assignment.

An administrator can request a media restart without exposing arbitrary shell commands:

```bash
curl -X POST -H "Authorization: Bearer ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"command":"restart_media"}' \
  http://SERVER:8080/api/devices/cam-01/commands
```

To exercise the intended control-plane scale without physical Pis:

```bash
python3 -m cctv.simulate --server http://SERVER:8080 --token ADMIN_TOKEN
```

By default this provisions 26 virtual cameras, 23 virtual monitors, and a mix of single and 2×2 assignments.

See [`docs/latency-test.md`](docs/latency-test.md) for the required end-to-end measurement procedure and [`docs/architecture.md`](docs/architecture.md) for the V1 system boundaries.
