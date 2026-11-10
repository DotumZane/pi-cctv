# Install a new Pi

Use this for a fresh Raspberry Pi camera or monitor. The Pi discovers dashboards on its local CCTV network, so you do not need to enter a server address, copy a bootstrap token, or install the dashboard CA by hand.

## 1. Prepare the dashboard

Before connecting a new Pi:

1. Update the dashboard from **System settings → Server software**.
2. Confirm the dashboard has local HTTPS enabled and its `discovery` service is running. The server update deploys both the management and discovery services.
3. Make sure the Pi can join the same local network as the dashboard. LAN discovery does not cross routers or VLAN boundaries by default.

## 2. Install Raspberry Pi OS Lite

In Raspberry Pi Imager, select **Raspberry Pi OS Lite (64-bit)**. Set a unique hostname in Customisation, such as `cctv-cam-03` or `cctv-mon-02`, and configure the user and network as needed. Connect the Pi to the CCTV network by Ethernet and power it on.

## 3. Install Pi CCTV

Sign in to the Pi locally or through Raspberry Pi Connect, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/DotumZane/pi-cctv/main/install.sh | \
  sudo bash -s -- --discover
```

The installer installs the required packages, downloads the Pi CCTV software, and starts its boot service. The agent generates a device-specific pairing identity on the Pi and searches the local network for dashboards. No shared enrollment secret is needed.

## 4. Adopt it from the dashboard

Open **Devices → Pending devices** on the dashboard you want this Pi to belong to. If several dashboards are on the same LAN, the Pi may appear on each one; adopt it on the correct dashboard. Choose **Camera** or **Monitor**, set the display name and location, then click **Adopt**. The Pi receives its unique device credential and begins reporting to that dashboard. Other dashboards remove their unadopted pending entry when the Pi completes adoption.

To replace an offline camera, choose that camera in **Replace offline camera** before adopting the new Pi. This preserves the camera's dashboard identity, camera settings, and monitor assignments, and changes the new Pi's hostname to the retained camera ID. Replacement is available only for offline cameras.

Check the Devices page to confirm it becomes online. For a camera, confirm a live view appears. For a monitor, assign a camera in Monitor routing and confirm its display.

## If the Pi does not appear

- Confirm the dashboard server and Pi are on the same LAN/VLAN and that the network allows local UDP discovery on port `42900` and HTTPS on port `8443`.
- Confirm the dashboard has HTTPS configured and the `discovery` container is running.
- Check the Pi agent with `sudo systemctl status pi-cctv-agent --no-pager` and `sudo journalctl -u pi-cctv-agent -n 50 --no-pager`.
- If the dashboard is on another network, use the [legacy explicit-address enrollment](../README.md#legacy-explicit-dashboard-address-and-bootstrap-token) with a reachable server address.

Discovery trusts the certificate fingerprint advertised on the local network, so use it only on a trusted CCTV LAN. The dashboard cannot control a Pi until an administrator adopts it.
