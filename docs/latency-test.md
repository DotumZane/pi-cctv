# V1 latency validation

The 150–250 ms target must be measured on installed hardware. Software configuration alone is not evidence of end-to-end latency.

## Repeatable procedure

1. Put the camera, monitor, and MediaMTX host on the production-equivalent wired CCTV VLAN.
2. Use a high-speed camera, or a photodiode/LED trigger and HDMI capture device, with a common time reference. A stopwatch or browser playback timestamp is not accurate enough.
3. Publish a moving timestamped test pattern:

   ```bash
   python3 -m cctv.latency rtsp://MEDIA_SERVER:8554/latency-test
   ```

4. Display the stream using the same monitor pipeline and resolution intended for operation.
5. Capture the physical event at the camera and the HDMI output simultaneously for at least 60 seconds under idle load.
6. Repeat while recording is active and while all planned cameras/monitors are active.
7. For each sample, calculate `display_time - source_time`. Report median, 95th percentile, maximum, dropped frames, and reconnects.

## Acceptance gates

- Median latency below 150 ms is the preferred result.
- 95th percentile and maximum must remain below 250 ms for the system to be considered acceptable for the intended operation.
- No sustained frame loss, accumulating delay, or unrecovered stream interruption is acceptable.
- Pi 4 and Pi 5 results must be recorded separately; a result on one model does not validate the other.

Record the exact Pi model, OS image, camera, lens, bitrate, resolution, FPS, MediaMTX version, GStreamer pipeline, switch, and recorder load with every test result.
