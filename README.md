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

With Docker Compose installed, set up local HTTPS and the server-managed setup Wi-Fi profile, then start the management API and RTSP relay:

```bash
cp .env.example .env
# Generate a private local CA and an HTTPS certificate for the server's LAN address.
sudo bash scripts/generate-local-tls.sh --ip SERVER_LAN_IP
# Edit .env: set the real tokens, enable the AP, and set a unique shared Wi-Fi password.
sudo chmod 600 .env
sudo docker compose up -d --build
# Install the fixed, allowlisted host updater once so dashboard updates can rebuild the management container and install Ubuntu package updates.
sudo bash scripts/install-server-updater.sh
```

The dashboard is available at `http://SERVER:8080`; device enrollment and private setup-Wi-Fi delivery use `https://SERVER:8443`. In `.env`, set `CCTV_TLS_DIR=/etc/pi-cctv/tls`, `CCTV_TLS_CERT=/run/pi-cctv/tls/server.crt`, `CCTV_TLS_KEY=/run/pi-cctv/tls/server.key`, and `CCTV_TLS_CA_CERT=/run/pi-cctv/tls/ca.crt`. Initial `CCTV_SETUP_AP_*` values seed the server's persistent settings on first start; afterward, manage the SSID, password, country, and channel in **System settings** over HTTPS. Keep `.env` and `/etc/pi-cctv/tls/ca.key` private. On first visit, create the dashboard administrator account. The RTSP port must be reachable by cameras and monitors on the CCTV VLAN.

Run `scripts/install-server-updater.sh` once from the server checkout after Docker Compose is installed. It installs fixed systemd path services for dashboard server software updates and Ubuntu package updates. **System settings → Server software** can then fast-forward a clean `main` checkout and rebuild the management container; **Ubuntu system updates** runs `apt-get update` and `apt-get -y upgrade` on the server without automatically rebooting it. The web container does not receive the Docker socket or an unrestricted host shell. On each Pi's device page, **Update CCTV software** updates the agent; **Update Pi OS** installs available Debian/Raspberry Pi OS package upgrades and reports the result. Update CCTV software first when the Pi is behind, and reboot from the dashboard later if an OS update reports that a reboot is required. OS updates may restart services but never reboot a device automatically. Use SSH for first-time bootstrap or recovery if a device cannot reach the dashboard, or if a host helper has not been installed.

Signed-in users can change their password in **Account** over HTTPS. Administrators can reset another account's password in **Users**; that signs out all of that user's active sessions. If the administrator password is forgotten and nobody can sign in, recover access locally on the dashboard server. The command prompts twice without displaying or storing the password:

```bash
python3 -m cctv.reset_password --username Admin
```

Use `--db /path/to/cctv.db` when the server uses a non-default database path. Resetting a password signs out all existing sessions for that user.

For adoption-based provisioning, `CCTV_BOOTSTRAP_TOKEN` is used only to place generic Pis into the pending-device list. It is not an administrator token. After adoption, the Pi receives a device-specific credential and the dashboard-selected role. Set `CCTV_PUBLIC_MEDIA_URL` and `CCTV_PUBLIC_WEBRTC_URL` to URLs reachable from the administrator's browser for live previews.

The V1 management API uses Python's standard library and SQLite. Docker installs FFmpeg in the management container for NVR recording:

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
  sudo bash -s -- --enroll --server https://SERVER:8443 \
  --media-server MEDIA_SERVER --server-ca /path/to/pi-cctv-server-ca.crt \
  --bootstrap-token BOOTSTRAP_TOKEN
```

The dashboard will show the Pi under **Pending devices**. Choose Camera or Monitor, set its name/location, and click **Adopt**.

To install and start a boot service in one command, provide the device role and endpoints:

```bash
sudo bash install.sh --role camera --name "Saw 1" --location "Line 1" \
  --server http://SERVER:8080 --media-server MEDIA_SERVER --token ADMIN_TOKEN
```

The installer creates and enables `pi-cctv-agent.service`, which restarts automatically after failures and starts after network-online.

### Server-managed phone setup Wi-Fi

Once an adopted Pi is using the server's HTTPS URL, its agent fetches the AP profile once at boot. An administrator can also save a new profile in **System settings** and explicitly queue an apply to online Pis. The profile is delivered only to an authenticated adopted Pi over HTTPS. The Wi-Fi password is stored server-side in SQLite and in the Pi's root-only NetworkManager/hostapd configuration; it is never returned to the dashboard or written to agent logs. Offline Pis fetch the latest profile at their next boot. Applying a profile uses the reversible bridge helper, which rolls back if the wired LAN address is lost.

Before trusting the local CA on a Pi, download the public certificate and compare its fingerprint to the SHA-256 fingerprint printed by the server certificate setup command:

```bash
curl -kfsS https://SERVER:8443/ca.crt -o /tmp/pi-cctv-server-ca.crt
openssl x509 -in /tmp/pi-cctv-server-ca.crt -noout -fingerprint -sha256
```

After confirming the fingerprint, migrate each already-adopted Pi to HTTPS (the command also installs/trusts the CA and restarts its agent):

```bash
sudo bash /opt/pi-cctv/install.sh --skip-packages --server-ca /tmp/pi-cctv-server-ca.crt \
  --enroll --server https://SERVER:8443 --media-server SERVER
```

For a brand-new Pi, download and verify the same CA certificate first, then use the `--enroll` command above; once adopted, it will receive the AP configuration automatically. The network must provide DHCP and allow the phone's additional MAC address on each Pi's Ethernet switch port. The setup SSID is hidden and WPA2 protected, but hiding the name is not an access-control measure. The AP bridges clients to Ethernet; it does not provide DHCP, NAT, or internet routing. Connect the phone manually to the hidden SSID. Use a 2.4 GHz channel from 1 to 11. The Pi may briefly disconnect as its Ethernet address moves onto the bridge.

To roll back the managed setup AP on a Pi and restore its saved Ethernet profile:

```bash
sudo bash /opt/pi-cctv/scripts/setup-bridged-ap.sh --remove
```

If the site's switch port enforces a single client MAC or the wired network does not provide DHCP to bridged Wi-Fi clients, the phone will not reach the dashboard until network policy is adjusted.

Use `--role monitor` for a monitor Pi. The agent registers the device and retries heartbeats through temporary network outages. Add `--run-media --media-server MEDIA_SERVER` to launch and recover the camera or monitor media process automatically:

```bash
python3 -m cctv.agent --role camera --name "Saw 1" --location "Line 1" \
  --server http://SERVER:8080 --media-server MEDIA_SERVER --run-media
```

The monitor runtime polls its centrally stored assignment and restarts its pipeline when the assignment changes or the process exits. It reports connected DRM/HDMI displays automatically. A one-display Pi can use a single camera or 2×2 layout; a two-display Pi exposes two independent single-camera outputs. The server supports optional bearer-token authentication through `CCTV_ADMIN_TOKEN` or `--admin-token`. V1 still needs hardware validation and further production security hardening before deployment.

### Optional remote SSH with Tailscale

Tailscale works on Raspberry Pi OS Lite and provides private remote access without router port forwarding or exposing SSH to the public internet. Install it on each camera or monitor Pi and on the computer you will use to connect. Keep the Pis in the same Tailscale network (tailnet) as your admin computer, and give each one a clear device name such as `cctv-cam-01` in the Tailscale admin console.

On each Pi, install and sign in to Tailscale, then enable Tailscale SSH:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale set --ssh
```

Open the sign-in URL printed by `tailscale up` and authorize the Pi. Repeat with a unique device name for each Pi. Install and sign in to Tailscale on your admin computer too. Once the tailnet access policy permits your account to SSH to the Pi, connect using the Pi's Linux username and Tailscale device name:

```bash
ssh <pi-username>@cctv-cam-01
```

Use a non-root Pi username. If the device name does not resolve, use the Pi's Tailscale IP shown in the admin console or by `tailscale status`. Tailscale SSH is available for Linux devices and must be enabled on each Pi. If you later tag the Pis as a group, add an SSH policy permitting your admin account to reach that tag; the default self-device SSH rule does not automatically cover tagged devices. See the [Tailscale SSH guide](https://tailscale.com/docs/features/tailscale-ssh) and [tag guide](https://tailscale.com/docs/features/tags).

Monitor settings are available from the selected monitor editor: scaling, orientation, output size, refresh rate, kiosk mode, screen blanking prevention, camera/timestamp overlays, reconnect/watchdog limits, fallback camera, and scheduled-layout enablement. Hardware-specific display behavior still needs validation on the first Pi board.

If `--id` is omitted, the agent uses the Pi hostname so the identity remains stable across reboot and service restart.

Pi heartbeats include available CPU temperature, one-minute load, memory-use percentage, uptime metrics, and monitor display outputs when applicable. Settings and health metadata are merged so recurring heartbeats cannot erase administrator configuration.

For the IMX477 HQ camera, the camera installer automatically enables `dtoverlay=imx477` in the Pi boot configuration. Set a camera to 2560×1440 in **Camera settings** when 2K/QHD recording is desired. The recorder keeps that source resolution with stream copy, while a 1080p monitor scales the same stream down locally.

The Compose management service automatically starts an FFmpeg stream-copy recorder for each online camera enabled in **NVR settings**. Recordings are stored persistently in the `cctv-data` volume under `/data/recordings`, and the NVR timeline indexes completed segments. With the default 15-minute segment length, the first completed segment appears after up to 15 minutes. The recorder does not transcode live video. To use the standalone recorder manually:

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
