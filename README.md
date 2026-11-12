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

The [baseline system goals and architecture](docs/architecture.md#baseline-system-goals) record the original industrial requirements and the planned direct camera-to-monitor outage behavior. Performance targets there require hardware testing.

## Planned components

- Camera software for Raspberry Pi camera devices
- Monitor decoder software for Raspberry Pi 4 and Raspberry Pi 5
- Central management server and web dashboard
- Device enrollment and one-command installation tooling
- Centralized recording and health monitoring
- Architecture, deployment, testing, and operations documentation

For the no-key first-time setup on a fresh Raspberry Pi OS Lite install, see the [Pi install guide](docs/pi-install-guide.md).

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

Run `scripts/install-server-updater.sh` once from the server checkout after Docker Compose is installed. It installs fixed systemd path services for dashboard server software updates, Ubuntu package updates, and an explicit host reboot action with reboot-completion reporting. **System settings → Server software** can then fast-forward a clean `main` checkout and rebuild the management container; **Ubuntu system updates** runs `apt-get update` and `apt-get -y upgrade` on the server without automatically rebooting it. Use **Reboot dashboard server** when an Ubuntu update reports that a reboot is required. The web container does not receive the Docker socket or an unrestricted host shell. On the **Devices** page, **Update outdated online Pis** and **Update online Pis one at a time** run as server-side, persisted jobs; their progress remains available if the browser closes or refreshes. Each device page also reports when its individual CCTV software update is queued, running, succeeded, or failed. Pi OS updates may restart services but never reboot a device automatically. Use SSH for first-time bootstrap or recovery if a device cannot reach the dashboard, or if a host helper has not been installed.

Administrators can use **System settings → Pi connection addresses** over HTTPS to migrate online Pis when the dashboard or MediaMTX address changes. Each Pi authenticates a heartbeat to the new HTTPS origin before atomically saving the new management and media addresses, so an unreachable server or untrusted certificate leaves its old configuration in place. The new address must lead to the same dashboard database and device credentials; this changes endpoints only and does not migrate device records between servers. Offline Pis must be brought online or recovered separately.

### Upgrade an existing dashboard server

An older server build without **System settings → Server software** cannot update itself from its dashboard. From the existing Git checkout on the Ubuntu server, run this one-time bootstrap:

```bash
cd ~/pi-cctv
git pull --ff-only origin main
sudo docker compose up -d --build management discovery
sudo bash scripts/install-server-updater.sh
```

The checkout must be owned by the normal server user (for example, `cctvops`), and Docker Compose must already work with `sudo`. The update installs the dashboard controls and their fixed host helper; it does not change the Pi enrollment or recording data. After the dashboard comes back, sign in over HTTPS and use **System settings → Server software** for future dashboard updates. Use **Ubuntu system updates** for server package updates, and the **Devices** page for fleet agent and Pi OS updates. SSH remains the recovery path if the server dashboard or its host updater is unavailable.

Signed-in users can change their password in **Account** over HTTPS. Administrators can reset another account's password in **Users**; that signs out all of that user's active sessions. If the administrator password is forgotten and nobody can sign in, recover access locally on the dashboard server. The command prompts twice without displaying or storing the password:

```bash
python3 -m cctv.reset_password --username Admin
```

Use `--db /path/to/cctv.db` when the server uses a non-default database path. Resetting a password signs out all existing sessions for that user.

Administrators can rename an adopted Pi and update its location from **Devices → Open → Device identity**. This changes only the dashboard name and location; the stable device ID, role, and agent settings remain intact.

To replace an offline camera, adopt a new pending Pi and select the offline camera in **Replace offline camera**. Disconnect the old Pi first so it cannot continue publishing to the same camera stream. The replacement keeps the camera's device ID, dashboard name/location, camera settings, and monitor assignments; the new Pi receives its own credential and changes its hostname to the retained device ID. Queued commands for the retired Pi are discarded.

For lens aiming, **Camera settings → Low-latency focus view** opens MediaMTX's WebRTC player directly in the browser. This avoids the HLS playback buffer and does not interrupt recording or monitor streams. The phone must be able to reach the configured WebRTC address from its network (including when connected through the Pi setup Wi-Fi). Docker publishes WebRTC media on UDP port 8189; `CCTV_WEBRTC_ADDITIONAL_HOSTS` in the server's `.env` must list the LAN and/or Tailscale IPs that clients use to reach the server. The dashboard's server updater recreates MediaMTX when applying server updates, so changes to these container settings take effect with an update.
Administrators can also use **Devices → Open → Remove from dashboard** to remove a retired or test device record and clear its saved monitor routes. This does not wipe the Pi; a still-running Pi must be enrolled again before it can report to the dashboard.

For legacy adoption-based provisioning, `CCTV_BOOTSTRAP_TOKEN` places generic Pis into the pending-device list. New installations can use LAN discovery instead and do not need a shared enrollment token. After adoption, each Pi receives a device-specific credential and the dashboard-selected role. Set `CCTV_PUBLIC_MEDIA_URL` and `CCTV_PUBLIC_WEBRTC_URL` to URLs reachable from the administrator's browser for live previews.

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

The [direct operator viewing architecture](docs/architecture.md#direct-operator-viewing-through-router-and-nvr-outages) sends one encoded camera feed through a relay on the camera Pi to assigned monitors. A stream-copy forwarder sends a separate copy to server MediaMTX for the NVR and browser viewers. Monitors save their last assignment for server outages. Camera Pis need stable IP addresses, and the switches must provide a working local path when the router is down. Update the camera Pi agents before relying on direct routes; the server advertises a direct route only after a compatible agent reports that its local stream is published. Validate outage behavior on the actual switches and Pis.

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

### New Pi: discover dashboards and adopt without a shared key

On a fresh Pi OS Lite install, connect Ethernet to the CCTV LAN and run this once:

```bash
curl -fsSL https://raw.githubusercontent.com/DotumZane/pi-cctv/main/install.sh | \
  sudo bash -s -- --discover
```

The agent discovers Pi CCTV dashboards on the same LAN, checks each dashboard's HTTPS certificate authority against the fingerprint in its LAN discovery response, and requests pending enrollment without a shared bootstrap token. Each reachable dashboard shows the Pi under **Pending devices**; adopt it on the dashboard that should own it and choose Camera or Monitor. That dashboard then issues the Pi's unique device credential. Discovery is local-network trust-on-first-use: use it only on a trusted CCTV LAN. A Pi is not controllable until an administrator adopts it. If dashboards are on different networks or LAN discovery is blocked, use the manual enrollment flow below with the selected dashboard address.

The dashboard server must have local HTTPS configured and run the `discovery` Compose service. After pulling this release, deploy it with `sudo docker compose up -d --build management discovery`; the dashboard's **Server software** updater also refreshes both services. The old bootstrap-token flow remains available for existing setups.

#### Legacy: explicit dashboard address and bootstrap token

To install a generic Pi using a shared bootstrap token and explicit dashboard address:

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

For a brand-new Pi, use the discovery-based [Pi install guide](docs/pi-install-guide.md); after adoption, it receives the setup AP profile automatically. The network must provide DHCP and allow the phone's additional MAC address on each Pi's Ethernet switch port. The setup SSID is hidden and WPA2 protected, but hiding the name is not an access-control measure. The AP bridges clients to Ethernet; it does not provide DHCP, NAT, or internet routing. Connect the phone manually to the hidden SSID. Use a 2.4 GHz channel from 1 to 11. The Pi may briefly disconnect as its Ethernet address moves onto the bridge.

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

Monitor settings are available from the selected monitor editor: scaling, orientation, output size, refresh rate, kiosk mode, screen blanking prevention, camera/timestamp overlays, reconnect delay, fallback camera, and scheduled-layout enablement. The agent restarts a monitor stream if its media process exits or its route changes; a stalled dashboard preview alone will not restart the display pipeline. Hardware-specific display behavior still needs validation on the first Pi board.

The dashboard **System settings → Live stream addresses** section controls the public MediaMTX URLs embedded in camera live-view responses. Set both the Media/HLS and WebRTC base URLs to addresses reachable from the browser. These values are stored in the management database and do not change the management server URL configured on the Pis. If unset, the server uses `CCTV_PUBLIC_MEDIA_URL` and `CCTV_PUBLIC_WEBRTC_URL` from its environment.

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
