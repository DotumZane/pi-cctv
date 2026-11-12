# V1 architecture

The system separates control traffic from live video:

```text
Pi camera --H.264--> local RTSP relay --RTSP/H.264--> Pi monitor(s)
                                |
                                \-- stream copy --> server MediaMTX --> NVR/dashboard
Pi agents -- heartbeat/config --> management API <-- dashboard
```

- The management API stores devices, assignments, settings, health, and audit entries in SQLite.
- Camera and monitor agents enroll, send heartbeats, and recover their media subprocesses.
- Monitor agents report connected displays, then poll assignments and build either a single-camera or 2×2 hardware-decoder pipeline for one display, or one single-camera pipeline per output for two displays.
- The recorder reads compressed RTSP streams independently and writes segmented files with retention cleanup.
- Media does not pass through the management API. One encoded camera stream is fanned out by the camera Pi relay to assigned monitors and one NVR forwarder. Browser viewers use server MediaMTX, not another camera Pi connection.

The current implementation is a V1 prototype. Production rollout still requires Pi hardware tests, network sizing, TLS/reverse-proxy deployment, a stronger identity/enrollment model, and storage failure testing.

## Baseline system goals

The original project goal is an industrial sawmill CCTV replacement for analog BNC cameras. These are design and validation targets, **not measured capabilities of the current prototype**:

- Run unattended 24/7 with automatic startup and recovery. Prioritize reliable operator viewing and the lowest practical camera-to-monitor delay: **under 250 ms end to end**, ideally **under 150 ms**, at a smooth **30 FPS**. Measure actual scene-to-display latency under representative load before claiming either target.
- Design initially for about **26 cameras** and **23 monitor Pis**, while allowing expansion. Camera hardware starts with a **PoE-powered Pi 4 (1 GB), IMX477/HQ camera, and wired Gigabit Ethernet**. Use 1080p/30 as the baseline; test higher resolutions against latency and resource use. Benchmark Pi 4 and Pi 5 monitor decoding separately, especially four simultaneous feeds in a 2×2 layout.
- Let an administrator assign **any camera to any monitor**, including the same camera on multiple monitors, and choose one-camera or 2×2 layouts remotely. Changes should apply without rebooting. Monitors must automatically restore their last assignment after power loss and keep it when the management server is unavailable; operators cannot change assignments locally.
- Keep live operator video independent of dashboard viewing and NVR recording. Use centrally managed, dedicated CCTV networking, with direct camera-to-monitor transport where it meets the latency and resilience targets. The router, dashboard, and recorder must not be single points of failure for local operator displays; physical switches and cross-area backhauls still define the actual failure domains.
- Record centrally for **at least 24 hours** with automatic retention cleanup, playback by camera and time, and clip export. Avoid continuous writes to camera Pi SD cards and avoid recording transcodes that add live-view latency.
- Provide authenticated administrator and read-only viewer roles; protect device management, credentials, and administrative actions. Support remote configuration, health, diagnostics, logs, and safe updates while minimizing camera Pi resource use and SD-card wear.
- Validate recovery, latency, frame drops, CPU/memory use, network bandwidth, and sustained operation with a representative 26-camera/23-monitor load. Hardware-specific performance remains unproven until measured.

## Direct operator viewing through router and NVR outages

The software now routes operator monitors to a local RTSP relay on each camera Pi. The camera encodes once; a separate stream-copy process forwards that stream to server MediaMTX for recording and browser viewing. The management server supplies direct camera addresses with each assignment, and monitors cache their last valid settings and route on disk. An unavailable management API no longer prevents the agent from checking and restarting its local media process.

```text
camera Pi --direct live video over area Ethernet--> monitor Pi
         \--separate NVR feed---------------> server / recorder / dashboard viewers

management server --camera assignments/settings--> monitor Pi
monitor Pi --saved last assignment-----------> local fallback when server is unreachable
```

- Camera-to-monitor viewing no longer depends on the management API, the NVR recorder, or the server's MediaMTX relay being available. The server still controls camera assignments when reachable; monitors retain the last valid assignment during an outage and reconcile when it returns. The server advertises a direct route only after a compatible camera agent reports a published local stream; older or unready cameras continue to use the central relay.
- Keep a separate camera-to-NVR feed for recording and dashboard live viewing. Dashboard viewers should consume the server-side feed, not cause a new camera-Pi stream per viewer. Losing the NVR/server may stop recording and browser viewing, but must not stop direct monitor viewing.
- All camera and monitor Pis are PoE powered and wired to area switches. Each area switch has a backhaul toward the central router and NVR. Use stable addresses and a local connection path that does not require router DHCP, DNS, or routing for camera-to-monitor traffic during an outage.
- **Commissioning requirement:** Give every camera Pi a stable address on the CCTV Ethernet subnet, ideally a static address configured on the Pi. A DHCP reservation alone does not guarantee service after a router/DHCP failure and lease expiry. Confirm monitors can reach each camera's TCP port 8554 directly while the router is powered off. Keep these RTSP endpoints on a restricted CCTV network; the local relay currently has no per-monitor authentication.
- A camera and monitor on the **same powered area switch** should keep displaying video if the router or server goes down. Cross-area camera viewing requires the area backhauls to remain connected through a switching path independent of the router; it cannot be guaranteed when the router is the only path between area switches. Design the central switching topology accordingly.
- Failure of an area switch or its power affects the Pis attached to that switch. Separate area switches limit the failure to that area; PoE power backup and, where required, redundant switching/backhauls should be considered separately. Direct video cannot survive a broken physical path between camera and monitor.
- After network or server recovery, camera feeds and management should reconnect automatically and monitors should reconcile with the current server assignment. Recording gaps from a disconnected NVR are expected and should remain visible in playback.

The software path is implemented but hardware resilience and latency remain unverified. Commission on actual Pi and switch hardware, then test direct transport, fan-out capacity, authentication, assignment caching, router power loss, server/relay loss, an area-switch outage, cross-area paths, and restoration without manual intervention. A lost area switch or backhaul cannot be repaired by software.
