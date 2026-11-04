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
- Monitor agents poll assignments and build either a single-camera or 2×2 hardware-decoder pipeline.
- The recorder reads compressed RTSP streams independently and writes segmented files with retention cleanup.
- Media does not pass through the management API, avoiding unnecessary central-server latency and bandwidth.

The current implementation is a V1 prototype. Production rollout still requires Pi hardware tests, network sizing, TLS/reverse-proxy deployment, a stronger identity/enrollment model, and storage failure testing.
