# V1 architecture

The system separates control traffic from live video:

```text
Pi camera --RTSP/H.264--> MediaMTX / CCTV network --RTSP/H.264--> Pi monitor
     \                         |
      \-- heartbeat/config --> management API <-- dashboard
                               |
                               \--> recorder (stream copy)
```

- The management API stores devices, assignments, settings, health, and audit entries in SQLite.
- Camera and monitor agents enroll, send heartbeats, and recover their media subprocesses.
- Monitor agents report connected displays, then poll assignments and build either a single-camera or 2×2 hardware-decoder pipeline for one display, or one single-camera pipeline per output for two displays.
- The recorder reads compressed RTSP streams independently and writes segmented files with retention cleanup.
- Media does not pass through the management API, avoiding unnecessary central-server latency and bandwidth.

The current implementation is a V1 prototype. Production rollout still requires Pi hardware tests, network sizing, TLS/reverse-proxy deployment, a stronger identity/enrollment model, and storage failure testing.

## Target: keep local monitors running through router and NVR outages

This is an agreed design goal, **not the current implementation**. Today both camera publishers and monitor decoders use the server-hosted MediaMTX relay, so loss of that relay interrupts monitor video even when the dashboard is not being used.

```text
camera Pi --direct live video over area Ethernet--> monitor Pi
         \--separate NVR feed---------------> server / recorder / dashboard viewers

management server --camera assignments/settings--> monitor Pi
monitor Pi --saved last assignment-----------> local fallback when server is unreachable
```

- Camera-to-monitor viewing must not depend on the management API, the NVR recorder, or the server's MediaMTX relay being available. The server still controls which camera each monitor displays when reachable; monitors persist the last valid assignment and continue it during a server outage.
- Keep a separate camera-to-NVR feed for recording and dashboard live viewing. Dashboard viewers should consume the server-side feed, not cause a new camera-Pi stream per viewer. Losing the NVR/server may stop recording and browser viewing, but must not stop direct monitor viewing.
- All camera and monitor Pis are PoE powered and wired to area switches. Each area switch has a backhaul toward the central router and NVR. Use stable addresses and a local connection path that does not require router DHCP, DNS, or routing for camera-to-monitor traffic during an outage.
- A camera and monitor on the **same powered area switch** should keep displaying video if the router or server goes down. Cross-area camera viewing requires the area backhauls to remain connected through a switching path independent of the router; it cannot be guaranteed when the router is the only path between area switches. Design the central switching topology accordingly.
- Failure of an area switch or its power affects the Pis attached to that switch. Separate area switches limit the failure to that area; PoE power backup and, where required, redundant switching/backhauls should be considered separately. Direct video cannot survive a broken physical path between camera and monitor.
- After network or server recovery, camera feeds and management should reconnect automatically and monitors should reconcile with the current server assignment. Recording gaps from a disconnected NVR are expected and should remain visible in playback.

Before implementation, validate the direct transport, stream fan-out and encoder capacity for multiple monitors, authentication, assignment caching, address management, and failover/recovery on actual Pi and switch hardware. Acceptance testing must cover router power loss, server/relay loss, an area-switch outage, cross-area paths, and restoration without manual intervention.
