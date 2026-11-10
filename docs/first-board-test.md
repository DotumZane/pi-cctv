# First Raspberry Pi board test

This is the shortest path to validate one camera Pi and one monitor Pi before expanding the system. For a fresh Raspberry Pi OS Lite install, follow the [Pi install guide](pi-install-guide.md) for the no-key discovery and dashboard adoption flow.

## Before powering the board

- Use a Raspberry Pi 4 or 5 with wired Ethernet and the intended PoE HAT.
- Connect the HQ/IMX477 camera and verify the ribbon cable orientation.
- Prepare a monitor Pi with HDMI output and a wired network connection.
- Put the test Pi devices, management server, and MediaMTX host on the same isolated test network.
- Update the dashboard first so its discovery service is running, and put the Pis on the same LAN/VLAN as the dashboard. The dashboard creates its administrator account on first visit.

## Camera Pi

Install the CCTV software on the fresh Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/DotumZane/pi-cctv/main/install.sh | \
  sudo bash -s -- --discover
```

Then adopt it from **Devices → Pending devices** as a Camera, name it **Test Camera 1**, and set location to **Bench**.

Verify the service and camera stack:

```bash
systemctl status pi-cctv-agent --no-pager
gst-inspect-1.0 libcamerasrc
gst-inspect-1.0 v4l2h264enc
```

The camera installer enables the IMX477 overlay automatically. For a 2K recording test, set the camera to 2560×1440 in the dashboard; leave the monitor at its native 1080p output and verify that it scales the feed without changing the recorded source resolution.

The camera should appear in the dashboard and show `online` after its first heartbeat.

## Monitor Pi

Install the CCTV software on the monitor Pi with the same command:

```bash
curl -fsSL https://raw.githubusercontent.com/DotumZane/pi-cctv/main/install.sh | \
  sudo bash -s -- --discover
```

Adopt it from **Devices → Pending devices** as a Monitor, name it **Test Monitor 1**, and set location to **Bench**.

Assign the test camera to the monitor in the dashboard. Confirm that the monitor service starts after the assignment is saved and that `systemctl status pi-cctv-agent` remains active.

## First measurements

1. Confirm the camera RTSP path is publishing: `rtsp://MEDIA_SERVER:8554/<camera-id>`.
2. Confirm the monitor displays the camera without a desktop UI or operator controls.
3. Use [`latency-test.md`](latency-test.md) for measured end-to-end latency; do not infer latency from service status.
4. Change camera FPS/bitrate in the dashboard and confirm the stream recovers with the new setting.
5. Use the dashboard/API restart action and confirm the media process recovers.
6. Disconnect Ethernet for 30 seconds, reconnect it, and confirm heartbeat and video recover.

## Capture for troubleshooting

```bash
journalctl -u pi-cctv-agent -n 100 --no-pager
curl -H "Authorization: Bearer ADMIN_TOKEN" http://SERVER:8080/api/devices
curl -H "Authorization: Bearer ADMIN_TOKEN" http://SERVER:8080/api/health
```

Record the Pi model, OS image, camera module, lens, resolution, FPS, bitrate, switch/PoE hardware, and measured latency with the first test results.
