/*
 * Recorded-video scrubbing engine for the camera workspace.
 *
 * Normal playback uses the native <video id="workspace-live"> element with one
 * MP4 segment at a time. While the timeline moves, that player stays paused
 * and a canvas overlay shows frames from the server's time-indexed preview
 * track:
 *
 *   fine    one 256x144 keyframe per second, in per-segment sheets
 *   coarse  one 160x90 frame per 15 s, one sheet per hour
 *   video   the original MP4 decoded by a hidden <video> (detailed zooms)
 *
 * The density follows the timeline zoom and the cursor speed. A single
 * requestAnimationFrame loop draws the available frame closest to the cursor.
 * A frame replaces the displayed one only when it is closer to the cursor, so
 * late responses never move the picture away from the requested time. The
 * last frame stays visible until a better one is ready.
 */
(function () {
  'use strict';

  const debug = new URLSearchParams(location.search).has('debugPlayback');
  const FINE = 'fine', COARSE = 'coarse', VIDEO = 'video';
  const INTERVAL = { fine: 1, coarse: 15, video: 1 / 30 };
  // Timeline seconds per pixel up to which per-second sheets are used, and
  // the cursor speed (footage seconds per second) up to which they are
  // fetched: 600 s/s is 20 segment sheets (about 2 MB) per second. When a
  // sheet is late, the coarse frame near the cursor stands in for it.
  const FINE_MAX_SPP = 10, FINE_MAX_SPEED = 600;
  // Detailed zooms decode the recording itself for sub-second positions.
  const VIDEO_MAX_SPP = 0.6, VIDEO_MAX_SPEED = 30;
  const LIMITS = { decodedFine: 16, decodedCoarse: 4, sheetFetches: 4, manifestFetches: 3, decodes: 2 };
  const COMMIT_IDLE_MS = 350, WHEEL_IDLE_MS = 300, SCROLLEND_GRACE_MS = 120, REST_AFTER_MS = 120, PREPARE_AFTER_MS = 120;
  // Start loading the segment under a slowly moving cursor once it has
  // stayed in that segment this long, so stopping only needs a seek.
  const PRELOAD_MAX_SPEED = 60, PRELOAD_DWELL_MS = 250;
  // Load the next segment in a hidden player this long before the end.
  const NEXT_PRELOAD_SECONDS = 8;

  const state = {
    canvas: null, ctx: null, message: null, proxy: null,
    active: false, loop: 0, camera: null,
    // Cursor: the requested timestamp. `t` is absolute epoch seconds.
    cursor: { t: null, selected: null, record: null, offset: 0, date: null },
    target: null, targetRecord: null, targetAbsolute: null, targetOffset: 0,
    velocity: 0, direction: 0, lastInputAt: 0, lastInputT: null,
    displayed: null, hasPicture: false, confirmedGap: false,
    hours: new Map(), manifestQueue: [], manifestsInFlight: 0,
    sheets: new Map(), decodedFine: new Map(), decodedCoarse: new Map(),
    sheetsInFlight: 0, decodesInFlight: 0,
    proxyPath: null, proxyReady: false, proxySeeking: false, proxyDirect: false,
    requestToken: 0, committing: false,
    gapPlaying: false, gapTimer: 0, gapLastTick: 0, gapTicking: false,
    commitTimer: 0, commitScheduleTimer: 0, pointerDown: false, lastWheelAt: 0,
    gesture: null, lastValue: null, lastTime: 0,
    recordSince: 0, next: null, nextUrl: null, bridgeToken: 0, bridging: false,
  };

  function log(event, details) { if (debug) console.debug('[playback]', event, details || ''); }

  // ---------------------------------------------------------------- metrics
  const metrics = {
    since: 0, ticks: 0, paints: 0, keys: new Set(), sources: {}, levels: {},
    latencies: [], errors: [], pendingInputs: [], hits: 0, misses: 0,
    manifestRequests: 0, sheetRequests: 0, aborted: 0, gestures: [],
  };
  function resetMetrics() {
    Object.assign(metrics, { since: performance.now(), ticks: 0, paints: 0, keys: new Set(), sources: {},
      levels: {}, latencies: [], errors: [], pendingInputs: [], hits: 0, misses: 0,
      manifestRequests: 0, sheetRequests: 0, aborted: 0, gestures: [] });
  }
  resetMetrics();
  function percentile(values, p) {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))];
  }
  function recordPaint(frame, now) {
    metrics.paints += 1;
    metrics.keys.add(frame.key);
    metrics.sources[frame.level] = (metrics.sources[frame.level] || 0) + 1;
    if (state.cursor.t !== null) {
      metrics.errors.push(Math.abs(frame.t - state.cursor.t));
      if (metrics.errors.length > 5000) metrics.errors.shift();
    }
    // An input is satisfied by the first painted frame within one frame
    // interval of the time that input requested.
    const tolerance = Math.max(INTERVAL[frame.level], 0.05);
    metrics.pendingInputs = metrics.pendingInputs.filter(input => {
      if (Math.abs(frame.t - input.t) > tolerance) return now - input.at < 5000;
      metrics.latencies.push(now - input.at);
      if (metrics.latencies.length > 5000) metrics.latencies.shift();
      return false;
    });
  }
  window.cctvScrubMetrics = {
    reset: resetMetrics,
    snapshot() {
      const seconds = Math.max(0.001, (performance.now() - metrics.since) / 1000);
      const errors = metrics.errors, lookups = metrics.hits + metrics.misses;
      const commits = metrics.gestures.map(gesture => gesture.commits);
      return {
        seconds,
        // Canvas work and distinct footage frames are measured separately.
        renderFps: metrics.ticks / seconds,
        paintFps: metrics.paints / seconds,
        fps: Math.max(0, metrics.keys.size - 1) / seconds,
        uniqueFrames: metrics.keys.size,
        displayedChanges: metrics.paints,
        medianLatencyMs: percentile(metrics.latencies, 0.5),
        p95LatencyMs: percentile(metrics.latencies, 0.95),
        meanPositionErrorSeconds: errors.length ? errors.reduce((a, b) => a + b, 0) / errors.length : null,
        maxPositionErrorSeconds: errors.reduce((a, b) => Math.max(a, b), 0),
        cacheHitRate: lookups ? metrics.hits / lookups : null,
        cacheHits: metrics.hits, cacheMisses: metrics.misses,
        manifestRequests: metrics.manifestRequests, sheetRequests: metrics.sheetRequests, abortedRequests: metrics.aborted,
        commitsPerGesture: commits, gestures: commits.length,
        sources: { ...metrics.sources }, levels: { ...metrics.levels },
        decoder: { active: state.active, ready: state.proxyReady, seeking: state.proxySeeking },
      };
    },
  };

  // ------------------------------------------------------------ DOM helpers
  function mainVideo() { return document.querySelector('#workspace-live'); }
  function shell() { return mainVideo()?.closest('.workspace-video-shell'); }
  function isMp4(record) { return /\.mp4(?:[?#]|$)/i.test(String(record?.url || '')); }
  function sourceFor(record) {
    // Recorder output is browser-playable MP4. Only legacy MKV recordings go
    // through the server's conversion endpoint.
    const url = String(record?.url || '');
    if (isMp4(record)) return url;
    return `${url}${url.includes('?') ? '&' : '?'}format=mp4`;
  }

  function disableNativeControls(video) {
    if (!video) return;
    video.controls = false;
    video.removeAttribute('controls');
    video.setAttribute('playsinline', '');
    video.setAttribute('webkit-playsinline', '');
    video.setAttribute('disablepictureinpicture', '');
    video.disableRemotePlayback = true;
    video.controlsList = 'nodownload noplaybackrate noremoteplayback';
  }

  function ensurePreview() {
    const video = mainVideo(), host = shell();
    if (!video || !host) return false;
    disableNativeControls(video);
    if (!state.canvas) {
      state.canvas = document.createElement('canvas');
      state.canvas.className = 'workspace-scrub-frame';
      state.canvas.setAttribute('aria-hidden', 'true');
      host.appendChild(state.canvas);
      state.ctx = state.canvas.getContext('2d', { alpha: false });
    }
    if (!state.message) {
      state.message = document.createElement('div');
      state.message.className = 'workspace-scrub-message';
      state.message.setAttribute('role', 'status');
      state.message.setAttribute('aria-live', 'polite');
      host.appendChild(state.message);
    }
    return true;
  }

  function scrubMessage(message) {
    if (!state.message || state.message.textContent === (message || '')) return;
    state.message.textContent = message || '';
    state.message.classList.toggle('visible', Boolean(message));
  }

  function fitCanvas() {
    const video = mainVideo(), canvas = state.canvas;
    if (!video || !canvas) return;
    const rawWidth = Math.max(1, video.clientWidth * devicePixelRatio);
    const rawHeight = Math.max(1, video.clientHeight * devicePixelRatio);
    // Preview frames are small; a Retina-sized canvas only adds paint cost.
    const scale = Math.min(1, 1280 / rawWidth, 720 / rawHeight);
    const width = Math.max(1, Math.round(rawWidth * scale));
    const height = Math.max(1, Math.round(rawHeight * scale));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
      state.displayed = state.displayed && { ...state.displayed, stale: true };
    }
  }

  function drawSource(source, sx, sy, sw, sh) {
    fitCanvas();
    const cw = state.canvas.width, ch = state.canvas.height;
    const scale = Math.min(cw / sw, ch / sh), dw = sw * scale, dh = sh * scale;
    state.ctx.fillStyle = '#030507';
    state.ctx.fillRect(0, 0, cw, ch);
    state.ctx.drawImage(source, sx, sy, sw, sh, (cw - dw) / 2, (ch - dh) / 2, dw, dh);
    state.hasPicture = true;
    state.canvas.classList.remove('stale');
    if (state.active) state.canvas.classList.add('visible');
  }

  function drawMainFrame() {
    const video = mainVideo();
    if (!state.active || !state.ctx || !video || video.readyState < 2) return false;
    const width = video.videoWidth || video.clientWidth, height = video.videoHeight || video.clientHeight;
    if (!width || !height) return false;
    drawSource(video, 0, 0, width, height);
    return true;
  }

  // ------------------------------------------------------------- time math
  function dayStart(date) { return new Date(`${date}T00:00:00`).getTime() / 1000; }
  function absoluteTimelineSeconds(date, value) { return dayStart(date) + Number(value); }
  function recordStart(record) {
    const start = new Date(record?.start).getTime() / 1000;
    return Number.isFinite(start) ? start : absoluteTimelineSeconds(workspaceTimelineDate, timelineSeconds(record));
  }
  function clock(t) {
    const date = new Date(t * 1000);
    return [date.getHours(), date.getMinutes(), date.getSeconds()].map(value => String(value).padStart(2, '0')).join(':');
  }
  function secondsPerPixel() {
    const height = typeof verticalTimelineHeight === 'number' && verticalTimelineHeight > 0 ? verticalTimelineHeight : 3600;
    const length = typeof timelineDayLength === 'function' ? timelineDayLength(workspaceTimelineDate) : 86400;
    return length / height;
  }
  function hourOf(t) { return Math.floor(t / 3600) * 3600; }

  // Pick the frame density from the zoom level and the cursor speed.
  function desiredLevel() {
    const spp = secondsPerPixel(), speed = Math.abs(state.velocity);
    if (spp <= VIDEO_MAX_SPP && speed <= VIDEO_MAX_SPEED && isMp4(state.cursor.record)) return VIDEO;
    // A cursor at rest always gets per-second frames, whatever the zoom.
    if ((spp <= FINE_MAX_SPP && speed <= FINE_MAX_SPEED) || speed === 0) return FINE;
    return COARSE;
  }
  // Sharper sources win ties: the 160x90 track, 256x144 track, full video.
  const DETAIL = { coarse: 0, fine: 1, video: 2 };
  // How far a frame may be from the cursor before it is reported as stale.
  function lagBound(level) {
    return INTERVAL[level === VIDEO ? FINE : level] * 1.5 + Math.abs(state.velocity) * 0.25;
  }

  // ------------------------------------------------------------- manifests
  function hourKey(camera, hour) { return `${camera}:${hour}`; }

  function indexHour(data) {
    const fine = [];
    (data.segments || []).forEach(segment => {
      const perSheet = segment.columns * segment.rows;
      segment.times.forEach((offset, index) => {
        const sheet = Math.floor(index / perSheet), cell = index % perSheet;
        fine.push({ t: segment.start + offset, level: FINE, cell, columns: segment.columns,
          width: segment.width, height: segment.height,
          url: `${segment.base}${String(sheet).padStart(3, '0')}.jpg?v=${encodeURIComponent(segment.version)}`,
          key: `${segment.path}:${index}` });
      });
    });
    fine.sort((a, b) => a.t - b.t);
    const coarse = (data.coarse?.times || []).map((t, cell) => ({ t, level: COARSE, cell,
      columns: data.coarse.columns, width: data.coarse.width, height: data.coarse.height,
      url: data.coarse.url, key: `coarse:${data.hour}:${t}` }));
    const pending = new Set((data.pending || []).map(item => item.path));
    return { fine, coarse, pending };
  }

  function manifestAge(entry) {
    const hour = entry.hour, now = Date.now() / 1000;
    return hour + 3600 > now - 60 || entry.index?.pending.size ? 10000 : 300000;
  }

  function requestHour(hour, priority) {
    if (!state.camera || hour < 0) return null;
    const key = hourKey(state.camera, hour);
    let entry = state.hours.get(key);
    if (entry && (entry.loading || performance.now() - entry.loadedAt < (entry.error ? 5000 : manifestAge(entry)))) {
      if (entry.queued) entry.priority = Math.min(entry.priority, priority);
      return entry;
    }
    if (!entry) {
      entry = { hour, camera: state.camera, index: null, loadedAt: 0, loading: false };
      state.hours.set(key, entry);
      while (state.hours.size > 96) {
        const oldest = state.hours.keys().next().value;
        if (oldest === key) break;
        state.hours.delete(oldest);
      }
    }
    if (!entry.queued) {
      entry.queued = true;
      entry.priority = priority;
      state.manifestQueue.push(entry);
    } else entry.priority = Math.min(entry.priority, priority);
    pumpManifests();
    return entry;
  }

  function pumpManifests() {
    state.manifestQueue.sort((a, b) => a.priority - b.priority);
    while (state.manifestsInFlight < LIMITS.manifestFetches && state.manifestQueue.length) {
      const entry = state.manifestQueue.shift();
      entry.queued = false;
      if (entry.camera !== state.camera) continue;
      entry.loading = true;
      state.manifestsInFlight += 1;
      metrics.manifestRequests += 1;
      api(`/api/recordings/preview-track?camera_id=${encodeURIComponent(entry.camera)}&hour=${entry.hour}`)
        .then(response => { if (!response.ok) throw new Error(`manifest ${response.status}`); return response.json(); })
        .then(data => { entry.index = indexHour(data); entry.error = false; })
        .catch(error => { entry.error = true; log('preview:manifest-error', error.message); })
        .finally(() => {
          entry.loading = false;
          entry.loadedAt = performance.now();
          state.manifestsInFlight -= 1;
          pumpManifests();
          wake();
        });
    }
  }

  // ---------------------------------------------------------------- sheets
  function decodedCache(level) { return level === COARSE ? state.decodedCoarse : state.decodedFine; }

  function decodedSheet(frame) {
    const cache = decodedCache(frame.level), bitmap = cache.get(frame.url);
    if (bitmap) { cache.delete(frame.url); cache.set(frame.url, bitmap); }
    return bitmap || null;
  }

  function storeDecoded(level, url, bitmap) {
    const cache = decodedCache(level), limit = level === COARSE ? LIMITS.decodedCoarse : LIMITS.decodedFine;
    cache.set(url, bitmap);
    while (cache.size > limit) {
      const oldest = cache.keys().next().value;
      if (state.displayed?.url !== oldest) cache.get(oldest)?.close?.();
      cache.delete(oldest);
    }
  }

  // `wanted` maps sheet URL -> {level, priority}. Fetches outside the wanted
  // set are aborted; the browser HTTP cache keeps their bytes for later.
  function syncSheets(wanted) {
    for (const [url, sheet] of state.sheets) {
      if (!wanted.has(url) && sheet.controller) {
        sheet.controller.abort();
        metrics.aborted += 1;
        state.sheets.delete(url);
      }
    }
    const queue = [...wanted.entries()]
      .filter(([url, item]) => !decodedCache(item.level).has(url) && !state.sheets.has(url))
      .sort((a, b) => a[1].priority - b[1].priority);
    for (const [url, item] of queue) {
      if (state.sheetsInFlight >= LIMITS.sheetFetches) break;
      fetchSheet(url, item.level);
    }
  }

  function fetchSheet(url, level) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const sheet = { controller, level };
    state.sheets.set(url, sheet);
    state.sheetsInFlight += 1;
    metrics.sheetRequests += 1;
    api(url, controller ? { signal: controller.signal } : {})
      .then(response => { if (!response.ok) throw new Error(`sheet ${response.status}`); return response.blob(); })
      .then(blob => {
        sheet.controller = null;
        state.decodesInFlight += 1;
        return Promise.resolve(createImageBitmap(blob)).finally(() => { state.decodesInFlight -= 1; });
      })
      .then(bitmap => { storeDecoded(level, url, bitmap); })
      .catch(error => { if (error?.name !== 'AbortError') log('preview:sheet-error', error?.message); })
      .finally(() => {
        state.sheetsInFlight -= 1;
        if (state.sheets.get(url) === sheet) state.sheets.delete(url);
        wake();
      });
  }

  // ---------------------------------------------------------- frame lookup
  function hourIndex(hour) {
    return state.camera ? state.hours.get(hourKey(state.camera, hour))?.index || null : null;
  }

  function lowerBound(frames, t) {
    let low = 0, high = frames.length;
    while (low < high) { const mid = (low + high) >> 1; if (frames[mid].t < t) low = mid + 1; else high = mid; }
    return low;
  }

  // Nearest frame of `level` within `bound` seconds of `t`. With `decoded`,
  // only frames whose sheet is ready to draw are considered.
  function nearestFrame(level, t, bound, decoded) {
    let best = null;
    for (const hour of [hourOf(t), hourOf(t) - 3600, hourOf(t) + 3600]) {
      if (Math.abs(hour + 1800 - t) > 1800 + bound) continue;
      const frames = hourIndex(hour)?.[level];
      if (!frames?.length) continue;
      const start = lowerBound(frames, t);
      for (let step = 0; step < frames.length; step++) {
        const left = start - 1 - step, right = start + step;
        const candidates = [right < frames.length ? frames[right] : null, left >= 0 ? frames[left] : null];
        if (!candidates[0] && !candidates[1]) break;
        let nearer = false;
        for (const frame of candidates) {
          if (!frame) continue;
          const distance = Math.abs(frame.t - t);
          if (distance > bound || (best && distance >= best.distance)) continue;
          nearer = true;
          if (!decoded || decodedSheet(frame)) best = { frame, distance };
        }
        if (!nearer) break;
      }
    }
    return best?.frame || null;
  }

  // --------------------------------------------------------- render loop
  function wake() {
    if (state.active && !state.loop) state.loop = requestAnimationFrame(tick);
  }

  function distanceToCursor(frame) { return frame ? Math.abs(frame.t - state.cursor.t) : Infinity; }

  function paintFrame(frame) {
    const bitmap = decodedSheet(frame);
    if (!bitmap) return false;
    const x = (frame.cell % frame.columns) * frame.width, y = Math.floor(frame.cell / frame.columns) * frame.height;
    drawSource(bitmap, x, y, frame.width, frame.height);
    state.displayed = frame;
    recordPaint(frame, performance.now());
    return true;
  }

  function updateLabel(level) {
    const cursor = state.cursor, shown = state.displayed;
    if (!cursor.record) return;
    if (!shown) {
      const pending = hourIndex(hourOf(cursor.t))?.pending.has(cursor.record.path);
      scrubMessage(pending ? 'Preparing preview…' : 'Loading preview…');
      return;
    }
    const distance = distanceToCursor(shown);
    if (distance <= Math.max(INTERVAL[shown.level], 0.5)) scrubMessage('');
    else if (distance <= lagBound(level)) scrubMessage(`Preview · ${clock(shown.t)}`);
    else scrubMessage(`Loading preview · showing ${clock(shown.t)}`);
  }

  function prefetch(level) {
    const t = state.cursor.t, speed = Math.abs(state.velocity), direction = state.direction;
    const sheetLevel = level === VIDEO ? FINE : level, interval = INTERVAL[sheetLevel];
    // Look further ahead the faster the cursor moves; keep a short window
    // behind so a reversal finds frames already decoded.
    const ahead = Math.max(interval * (sheetLevel === FINE ? 45 : 8), speed * 2.5);
    const behind = Math.max(interval * (sheetLevel === FINE ? 15 : 4), speed * 0.5);
    const from = direction < 0 ? t - ahead : t - behind, to = direction < 0 ? t + behind : t + ahead;
    const hours = [];
    for (let hour = hourOf(from); hour <= hourOf(to) && hours.length < 14; hour += 3600) hours.push(hour);
    hours.forEach(hour => requestHour(hour, Math.abs(hour + 1800 - t)));
    const wanted = new Map();
    const want = (frame) => {
      const distance = Math.abs(frame.t - t), forward = Math.sign(frame.t - t) === direction;
      const priority = forward || !direction ? distance : distance * 2;
      const current = wanted.get(frame.url);
      if (!current || priority < current.priority) wanted.set(frame.url, { level: frame.level, priority });
    };
    hours.forEach(hour => {
      const frames = hourIndex(hour)?.[sheetLevel] || [];
      for (let i = lowerBound(frames, from); i < frames.length && frames[i].t <= to; i++) want(frames[i]);
    });
    // Keep the sheet of the density on screen wanted too, so a switch of
    // density never aborts the frame that is about to be replaced.
    const shownLevel = state.displayed?.level;
    if ((shownLevel === FINE || shownLevel === COARSE) && shownLevel !== sheetLevel) {
      const near = nearestFrame(shownLevel, t, lagBound(shownLevel), false);
      if (near) want(near);
    }
    // Never want more sheets than the decoded cache can hold, or prefetching
    // would evict the frames it just fetched.
    const kept = new Map(), counts = { fine: 0, coarse: 0 };
    [...wanted.entries()].sort((a, b) => a[1].priority - b[1].priority).forEach(([url, item]) => {
      const limit = item.level === COARSE ? LIMITS.decodedCoarse - 1 : LIMITS.decodedFine - 2;
      if (counts[item.level] < limit) { counts[item.level] += 1; kept.set(url, item); }
    });
    syncSheets(kept);
  }

  function tick() {
    state.loop = 0;
    if (!state.active || state.cursor.t === null) return;
    metrics.ticks += 1;
    const now = performance.now();
    // Scroll events arrive at least every few frames while anything moves;
    // a short silence means the cursor is at rest.
    if (now - state.lastInputAt > REST_AFTER_MS) state.velocity = 0;
    const level = desiredLevel();
    metrics.levels[level] = (metrics.levels[level] || 0) + 1;
    if (state.cursor.record) {
      prefetch(level);
      // Decode the recording itself at detailed zooms, and for a closed
      // segment whose preview frames are not ready yet.
      const decode = level === VIDEO || (isMp4(state.cursor.record) &&
        hourIndex(hourOf(state.cursor.t))?.pending.has(state.cursor.record.path));
      if (decode) {
        requestProxy(state.cursor.record);
        chaseProxy();
      }
      const sheetLevel = level === VIDEO ? FINE : level;
      const bound = lagBound(sheetLevel);
      const nearestAny = nearestFrame(sheetLevel, state.cursor.t, bound, false);
      let candidate = nearestFrame(sheetLevel, state.cursor.t, bound, true);
      if (nearestAny) {
        if (candidate === nearestAny) metrics.hits += 1; else metrics.misses += 1;
      }
      // Use the other density only when this one has nothing near the cursor.
      if (!candidate) candidate = nearestFrame(sheetLevel === FINE ? COARSE : FINE, state.cursor.t, bound, true);
      const shown = state.displayed;
      // Replace the picture only with a frame nearer the cursor, or with an
      // equally near frame from this zoom level's own density.
      const nearer = candidate && shown && distanceToCursor(candidate) < distanceToCursor(shown) - 1e-3;
      const upgrade = candidate && shown && candidate.level === sheetLevel && DETAIL[candidate.level] > DETAIL[shown.level] &&
        distanceToCursor(candidate) <= distanceToCursor(shown) + 1e-3;
      if (candidate && candidate.key !== shown?.key && (!shown || shown.stale || nearer || upgrade)) {
        paintFrame(candidate);
      } else if (shown?.stale && shown.level !== VIDEO && decodedSheet(shown)) paintFrame({ ...shown, stale: false });
      updateLabel(sheetLevel);
      if (state.velocity === 0 && !state.pointerDown && now - state.lastInputAt > PREPARE_AFTER_MS) prepareMain();
      else if (Math.abs(state.velocity) <= PRELOAD_MAX_SPEED && now - state.recordSince >= PRELOAD_DWELL_MS) {
        loadMainSource(state.cursor.record);
      }
    }
    // Keep ticking until the cursor has come to rest and its frames are in,
    // so the resting density (finer than the moving one) gets drawn.
    const busy = state.sheetsInFlight || state.manifestsInFlight || state.proxySeeking ||
      now - state.lastInputAt < 600 || state.velocity !== 0 || state.gapPlaying;
    if (busy) wake();
  }

  // --------------------------------------------------- main player at rest
  // Once the cursor stops, load and seek the main player at that position
  // behind the preview. Its full-resolution frame replaces the small preview
  // frame, and the commit that follows only has to press play.
  function prepareMain() {
    const record = state.cursor.record, video = mainVideo();
    if (!record || !video || !isMp4(record) || !state.active ||
        (state.commitToken && state.commitToken === state.requestToken)) return;
    const offset = state.cursor.offset, prepared = state.prepared;
    if (prepared && prepared.token === state.requestToken) return;
    const token = state.requestToken;
    state.prepared = { token, path: record.path, offset };
    const seek = () => {
      if (token !== state.requestToken || !state.active) return;
      const target = Number.isFinite(video.duration) ? Math.min(offset, Math.max(0, video.duration - 0.05)) : offset;
      if (!video.seeking && video.readyState >= 2 && Math.abs(video.currentTime - target) < 0.02) { showMainAtRest(record, token); return; }
      video.addEventListener('seeked', () => showMainAtRest(record, token), { once: true });
      try { video.currentTime = target; } catch {}
    };
    if (!loadMainSource(record)) return;
    video.pause();
    if (video.readyState >= 1) seek();
    else video.addEventListener('loadedmetadata', seek, { once: true });
  }

  // Put the cursor's segment in the main player, paused behind the preview.
  // Returns false when that is not possible (no MP4 or a commit owns it).
  function loadMainSource(record) {
    const video = mainVideo();
    if (!record || !video || !isMp4(record) || !state.active ||
        (state.commitToken && state.commitToken === state.requestToken)) return false;
    if (workspacePlaybackUrl === record.url && video.dataset.playback === 'recording' && !video.error) return true;
    disableNativeControls(video);
    // The live view relies on autoplay; a prepared recording must stay
    // paused until the commit plays it. Live view turns autoplay back on.
    video.autoplay = false;
    video.pause();
    video.onerror = null;
    if (typeof stopLivePlayback === 'function') stopLivePlayback(video);
    video.dataset.playback = 'recording';
    video.src = record.url;
    video.load();
    workspacePlaybackUrl = record.url;
    workspacePlaybackStartSeconds = timelineSeconds(record);
    return true;
  }

  function showMainAtRest(record, token) {
    const video = mainVideo();
    if (token !== state.requestToken || !state.active || state.cursor.record?.path !== record.path || !video || video.readyState < 2) return;
    const frame = { t: recordStart(record) + video.currentTime, level: VIDEO, key: `main@${record.path}@${video.currentTime.toFixed(3)}` };
    if (state.displayed && distanceToCursor(frame) > distanceToCursor(state.displayed) + 0.05) return;
    if (!drawMainFrame()) return;
    state.displayed = frame;
    recordPaint(frame, performance.now());
    updateLabel(FINE);
  }

  // ------------------------------------------------------ video fine scrub
  function requestProxy(record) {
    if (!record || !isMp4(record) || !ensurePreview()) return;
    if (state.proxyPath === record.path) return;
    if (!state.proxy) {
      const proxy = document.createElement('video');
      if (!proxy?.addEventListener) return;
      proxy.className = 'workspace-scrub-proxy';
      proxy.muted = true;
      proxy.preload = 'auto';
      proxy.setAttribute('playsinline', '');
      disableNativeControls(proxy);
      shell().appendChild(proxy);
      proxy.addEventListener('loadedmetadata', () => { state.proxyReady = true; state.proxySeeking = false; wake(); });
      proxy.addEventListener('seeked', () => { state.proxySeeking = false; onProxyFrame(); wake(); });
      proxy.addEventListener('error', () => { state.proxyReady = false; state.proxySeeking = false; });
      state.proxy = proxy;
    }
    state.proxyPath = record.path;
    state.proxyRecord = record;
    state.proxyReady = false;
    state.proxySeeking = false;
    state.proxy.src = record.url;
    state.proxy.load?.();
  }

  // One seek at a time; the newest cursor position wins. With one keyframe
  // per second each seek decodes at most a second of footage.
  function chaseProxy() {
    const proxy = state.proxy, record = state.cursor.record;
    if (!proxy || !state.proxyReady || state.proxySeeking || !record || state.proxyPath !== record.path) return;
    const target = Math.max(0, Math.min(state.cursor.offset, Math.max(0, (proxy.duration || Infinity) - 0.05)));
    if (Math.abs(proxy.currentTime - target) < 0.02) return;
    state.proxySeeking = true;
    try { proxy.currentTime = target; } catch { state.proxySeeking = false; }
  }

  function onProxyFrame() {
    const proxy = state.proxy, record = state.proxyRecord;
    if (!state.active || !proxy || proxy.readyState < 2 || !record || state.cursor.record?.path !== record.path) return;
    const frame = { t: recordStart(record) + proxy.currentTime, level: VIDEO, key: `${record.path}@${proxy.currentTime.toFixed(3)}` };
    if (state.displayed && distanceToCursor(frame) >= distanceToCursor(state.displayed) - 1e-3) return;
    const width = proxy.videoWidth, height = proxy.videoHeight;
    if (!width || !height) return;
    drawSource(proxy, 0, 0, width, height);
    state.displayed = frame;
    recordPaint(frame, performance.now());
    updateLabel(FINE);
  }

  // ----------------------------------------------------------- preview on/off
  function showPreview() {
    if (!ensurePreview()) return;
    if (!state.active) {
      state.active = true;
      // Start from the picture the viewer is looking at. Any preview frame
      // nearer the cursor replaces it.
      const video = mainVideo();
      // An ended segment keeps the closing frame captured just before the
      // end; redrawing from an ended video can paint black in some browsers.
      if (!video.ended && drawMainFrame()) {
        const recording = video.dataset.playback === 'recording' && typeof workspacePlaybackStartSeconds !== 'undefined';
        const t = recording ? absoluteTimelineSeconds(workspaceTimelineDate, workspacePlaybackStartSeconds + Number(video.currentTime || 0)) : Date.now() / 1000;
        state.displayed = { t, level: VIDEO, key: `main@${t.toFixed(3)}` };
      } else if (state.displayed) state.displayed = { ...state.displayed, stale: true };
    }
    window.workspaceScrubActive = true;
    if (state.hasPicture) state.canvas.classList.add('visible');
    wake();
  }

  function hidePreview() {
    state.active = false;
    window.workspaceScrubActive = false;
    if (state.loop) cancelAnimationFrame(state.loop);
    state.loop = 0;
    clearTimeout(state.commitScheduleTimer);
    clearTimeout(state.commitTimer);
    state.commitScheduleTimer = state.commitTimer = 0;
    state.velocity = 0;
    state.direction = 0;
    state.proxy?.pause?.();
    if (state.canvas) state.canvas.classList.remove('visible');
    scrubMessage('');
  }

  function recordingOffset(record, selected) {
    const offset = Math.max(0, selected - timelineSeconds(record));
    const duration = Number(record.duration);
    return Number.isFinite(duration) && duration > 0 ? Math.min(offset, Math.max(0, duration - 0.04)) : offset;
  }

  function updateStatus(message) {
    const status = document.querySelector('#workspace-player');
    if (status && message) status.innerHTML = `<span class="notice">${esc(message)}</span>`;
  }

  // --------------------------------------------------------------- gestures
  function gesture() {
    const now = performance.now();
    if (!state.gesture || state.gesture.ended) {
      state.gesture = { startedAt: now, commits: 0, moved: false, ended: false };
      metrics.gestures.push(state.gesture);
      if (metrics.gestures.length > 200) metrics.gestures.shift();
    }
    return state.gesture;
  }

  function setTarget(value, _velocity, confirmedGap = false) {
    if (!state.gapTicking) stopGapClock();
    const selected = Math.max(0, Math.min(timelineDayLength(workspaceTimelineDate) - 0.001, Number(value) || 0));
    const record = timelineRecordAt(selected);
    if (selected !== state.target || record?.path !== state.targetRecord?.path) {
      state.requestToken += 1;
      clearTimeout(state.commitTimer);
    }
    if (state.camera !== window.workspaceCamera) {
      state.camera = window.workspaceCamera;
      state.displayed = null;
      state.hasPicture = false;
      state.manifestQueue = [];
      state.canvas?.classList.remove('visible');
    }
    const now = performance.now(), absolute = absoluteTimelineSeconds(workspaceTimelineDate, selected);
    if (!state.gapTicking) {
      const current = gesture();
      if (state.cursor.t === null || absolute !== state.cursor.t) current.moved = true;
    }
    if (state.cursor.t !== null && state.lastInputAt) {
      const elapsed = Math.max(8, now - state.lastInputAt), delta = absolute - state.cursor.t;
      const instant = delta * 1000 / elapsed, direction = Math.sign(delta);
      if (direction && direction !== state.direction) state.velocity = instant;
      else state.velocity += (instant - state.velocity) * Math.min(1, elapsed / 120);
      if (direction) state.direction = direction;
    }
    state.lastInputAt = now;
    if (record?.path !== state.cursor.record?.path) state.recordSince = now;
    cancelBridge();
    state.cursor = { t: absolute, selected, record, offset: record ? recordingOffset(record, selected) : 0, date: workspaceTimelineDate };
    state.lastValue = state.target = selected;
    state.targetAbsolute = absolute;
    state.targetRecord = record;
    state.targetOffset = state.cursor.offset;
    metrics.pendingInputs.push({ at: now, t: absolute });
    if (metrics.pendingInputs.length > 500) metrics.pendingInputs.shift();
    const playing = mainVideo();
    if (playing?.dataset.playback === 'recording' && !playing.paused) playing.pause();
    showPreview();
    if (typeof workspaceTimelineLastValue !== 'undefined') workspaceTimelineLastValue = selected;
    if (typeof verticalTimelineSetPosition === 'function') verticalTimelineSetPosition(selected, false);
    if (typeof updateTimelinePlayhead === 'function') updateTimelinePlayhead(selected);
    if (!record) {
      // Keep the last picture visible with an explicit label instead of
      // flashing black for brief gaps between recording segments.
      const date = workspaceTimelineDate;
      const loading = document.querySelector(`.vertical-timeline-day-layer[data-timeline-day="${date}"]`)?.dataset.loading === 'true';
      if (loading) { scrubMessage('Loading recordings…'); return; }
      const hadFrame = state.hasPicture;
      if (!hadFrame) {
        fitCanvas();
        state.ctx.fillStyle = '#030507';
        state.ctx.fillRect(0, 0, state.canvas.width, state.canvas.height);
        state.hasPicture = true;
      }
      state.canvas.classList.add('visible');
      // The held picture is older or newer footage: dim it so it is never
      // mistaken for the selected time, and say when footage resumes.
      state.canvas.classList.toggle('stale', hadFrame);
      if (confirmedGap) {
        const next = nextRecording(selected);
        scrubMessage(`No recording · ${date} ${formatTimelineTime(selected)}${hadFrame ? ' · last frame shown' : ''}${next ? ` · next recording ${formatTimelineTime(timelineSeconds(next))}` : ''}`);
        updateStatus('No recording is available at the selected time.');
      } else {
        scrubMessage('');
        updateStatus('Scrolling · last available frame shown.');
      }
      return;
    }
    // Back on footage: the label, not dimming, reports any remaining lag.
    state.canvas?.classList.remove('stale');
    wake();
  }

  // ---------------------------------------------------------------- gaps
  function userPaused() { return typeof workspaceUserPaused !== 'undefined' && workspaceUserPaused; }

  function nextRecording(seconds) {
    return (typeof workspaceTimelineRecords === 'undefined' ? [] : workspaceTimelineRecords)
      .filter(item => timelineSeconds(item) > seconds)
      .sort((a, b) => timelineSeconds(a) - timelineSeconds(b))[0];
  }

  function stopGapClock() {
    clearTimeout(state.gapTimer);
    state.gapTimer = 0;
    state.gapPlaying = false;
  }

  function showGapControl() {
    const button = document.querySelector('#workspace-play');
    if (button) button.textContent = state.gapPlaying ? 'Pause' : 'Play';
  }

  function startGapClock() {
    stopGapClock();
    if (userPaused()) { showGapControl(); return; }
    state.gapPlaying = true;
    state.gapLastTick = performance.now();
    showGapControl();
    const step = async () => {
      if (!state.gapPlaying || userPaused()) { stopGapClock(); showGapControl(); return; }
      const now = performance.now();
      const rate = Math.max(0.01, Number(mainVideo()?.playbackRate) || 1);
      const elapsed = Math.max(0, now - state.gapLastTick) / 1000 * rate;
      state.gapLastTick = now;
      let selected = state.target + elapsed;
      if (selected >= timelineDayLength(workspaceTimelineDate)) {
        if (typeof workspaceNextDate !== 'function' || typeof loadCameraTimeline !== 'function') {
          stopGapClock(); showGapControl(); return;
        }
        const date = workspaceTimelineDate, token = state.requestToken;
        const input = document.querySelector('#workspace-date');
        if (!input) { stopGapClock(); showGapControl(); return; }
        input.value = localDateKey(workspaceNextDate(date));
        const nextDate = input.value;
        selected -= timelineDayLength(date);
        await loadCameraTimeline(window.workspaceCamera);
        if (!state.gapPlaying || userPaused() || token !== state.requestToken) return;
        if (workspaceTimelineDate !== nextDate) {
          stopGapClock(); showGapControl();
          updateStatus('Unable to load the next day of recordings.');
          return;
        }
        state.gapLastTick = performance.now();
      }
      if (typeof liveTimelineSeconds === 'function') {
        const live = liveTimelineSeconds();
        if (live !== null && selected >= live) { stopGapClock(); void window.returnWorkspaceToLive(); return; }
      }
      if (timelineRecordAt(selected)) { stopGapClock(); void commit(selected); return; }
      state.gapTicking = true;
      try { setTarget(selected, 0, true); } finally { state.gapTicking = false; }
      showGapControl();
      state.gapTimer = setTimeout(step, 250);
    };
    state.gapTimer = setTimeout(step, 250);
  }

  window.toggleWorkspaceGap = function () {
    if (!state.active || state.target === null || state.targetRecord) return false;
    if (state.gapPlaying) {
      if (typeof workspaceUserPaused !== 'undefined') workspaceUserPaused = true;
      stopGapClock();
    } else {
      if (typeof workspaceUserPaused !== 'undefined') workspaceUserPaused = false;
      startGapClock();
    }
    showGapControl();
    return true;
  };

  // --------------------------------------------------------------- commit
  function canRetryRecording(error, record, converted) {
    if (converted || !isMp4(record)) return false;
    // Only media format/decode failures justify a server-side conversion.
    return error?.code === 3 || error?.code === 4 || error?.name === 'NotSupportedError';
  }

  // --------------------------------------------------- seamless segment switch
  function hiddenNextPlayer() {
    if (state.next) return state.next;
    const host = shell();
    const video = document.createElement('video');
    if (!host || !video?.addEventListener) return null;
    video.className = 'workspace-scrub-proxy';
    video.muted = true;
    video.preload = 'auto';
    video.setAttribute('playsinline', '');
    disableNativeControls(video);
    host.appendChild(video);
    state.next = video;
    return video;
  }

  // Near the end of a segment, load the next one in a hidden player.
  function preloadNextSegment(video) {
    if (typeof workspaceTimelineRecords === 'undefined' || !Number.isFinite(video.duration)) return;
    const current = workspaceTimelineRecords.find(record => record.url === workspacePlaybackUrl);
    if (!current) return;
    const next = nextRecording(timelineSeconds(current));
    if (!next || !isMp4(next) || timelineSeconds(next) > timelineSeconds(current) + video.duration + 0.1) return;
    if (state.nextUrl === next.url) return;
    const player = hiddenNextPlayer();
    if (!player) return;
    state.nextUrl = next.url;
    player.src = next.url;
    player.load();
  }

  function cancelBridge() {
    if (!state.bridging) return;
    state.bridging = false;
    state.bridgeToken += 1;
    state.next?.pause?.();
  }

  // Play the preloaded next segment on the overlay at once, while the main
  // player loads it; hand over when the main player shows the same moment.
  async function bridgeToNext(record, video, token) {
    const player = state.next;
    const bridge = ++state.bridgeToken;
    state.bridging = true;
    const rate = video.playbackRate || video.defaultPlaybackRate || 1;
    player.playbackRate = rate;
    try { player.currentTime = 0; } catch {}
    try { await Promise.resolve(player.play()); }
    catch { state.bridging = false; return false; }
    ensurePreview();
    state.canvas.classList.add('visible');
    const paint = () => {
      if (bridge !== state.bridgeToken || !state.bridging) return;
      if (player.readyState >= 2) drawSource(player, 0, 0, player.videoWidth || 1, player.videoHeight || 1);
      if (typeof updateTimelinePlayhead === 'function') updateTimelinePlayhead(timelineSeconds(record) + player.currentTime);
      requestAnimationFrame(paint);
    };
    requestAnimationFrame(paint);
    disableNativeControls(video);
    video.pause();
    video.onerror = null;
    video.dataset.playback = 'recording';
    video.src = record.url;
    video.load();
    workspacePlaybackUrl = record.url;
    workspacePlaybackStartSeconds = timelineSeconds(record);
    try {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('recording load timed out')), 15000);
        video.addEventListener('loadedmetadata', () => { clearTimeout(timer); resolve(); }, { once: true });
        video.addEventListener('error', () => { clearTimeout(timer); reject(video.error); }, { once: true });
      });
      if (bridge !== state.bridgeToken || token !== state.requestToken) return true;
      const limit = Math.max(0, (video.duration || Infinity) - 0.05);
      // Get ready near the bridge's position (it keeps playing meanwhile),
      // then pause the bridge and line up on its exact frame. The data for
      // that frame is already loaded, so the final seek is quick, and the
      // picture neither jumps ahead nor back at the handover.
      video.currentTime = Math.min(player.currentTime, limit);
      await waitForDisplayedFrame(video);
      if (bridge !== state.bridgeToken || token !== state.requestToken) return true;
      player.pause();
      const target = Math.min(player.currentTime, limit);
      if (Math.abs(video.currentTime - target) > 0.04) {
        video.currentTime = target;
        await waitForDisplayedFrame(video);
      }
      if (bridge !== state.bridgeToken || token !== state.requestToken) return true;
      await startAndWaitForFrame(video);
      if (bridge !== state.bridgeToken || token !== state.requestToken) return true;
      if (userPaused()) video.pause();
      drawMainFrame();
      updateStatus(userPaused() ? 'Recording paused.' : 'Playing recording.');
    } catch (error) {
      if (bridge === state.bridgeToken && token === state.requestToken) handlePlaybackStartError(error);
    } finally {
      if (bridge === state.bridgeToken) {
        state.bridging = false;
        player.pause();
        if (!state.active) state.canvas?.classList.remove('visible');
      }
    }
    return true;
  }

  // `continuous` marks the automatic move to the next segment during
  // playback: it must look like uninterrupted playback, not a new load.
  async function commit(value, converted = false, continuous = false) {
    const selected = Math.max(0, Math.min(timelineDayLength(workspaceTimelineDate) - 0.001, Number(value) || 0));
    const record = timelineRecordAt(selected);
    const video = mainVideo();
    state.requestToken += 1;
    const token = state.requestToken;
    if (state.gesture && !state.gesture.ended) { state.gesture.commits += 1; state.gesture.ended = true; }
    if (!record) {
      // Keep the gap overlay so earlier footage or live cannot be mistaken
      // for footage at the selected time.
      if (video && !video.paused) video.pause();
      setTarget(selected, 0, true);
      if (state.gesture) state.gesture.ended = true;
      if (typeof verticalTimelineUserScrolling !== 'undefined') verticalTimelineUserScrolling = false;
      if (video && typeof setWorkspaceMode === 'function') setWorkspaceMode('recording');
      startGapClock();
      return;
    }
    // Preparation must not touch the player while the current commit is
    // using it. A commit superseded by new movement no longer blocks it.
    state.commitToken = token;
    try { return await commitRecording(record, selected, video, token, converted, continuous); }
    finally { if (state.commitToken === token) state.commitToken = 0; }
  }

  async function commitRecording(record, selected, video, token, converted, continuous) {
    const offset = recordingOffset(record, selected);
    if (continuous && !converted && offset < 0.5 && state.nextUrl === record.url && state.next?.readyState >= 2 &&
        !state.active && await bridgeToNext(record, video, token)) return;
    const url = converted ? `${record.url}${record.url.includes('?') ? '&' : '?'}format=mp4` : sourceFor(record);
    const sameSource = video && workspacePlaybackUrl === record.url && video.dataset.playback === 'recording' && !converted;
    const same = sameSource && video.readyState >= 1 && !video.error;
    // Keep the last preview frame over the player while it seeks; hiding it
    // first is what caused black flashes on commit.
    showPreview();
    if (same) {
      if (Math.abs(video.currentTime - offset) > 0.04) {
        video.pause();
        try { video.currentTime = offset; } catch {}
      }
      if (typeof setWorkspaceMode === 'function') setWorkspaceMode('recording');
      try { await startAndWaitForFrame(video); }
      catch (error) {
        if (token === state.requestToken && canRetryRecording(video.error || error, record, converted)) {
          updateStatus('Preparing compatible playback…');
          return commit(selected, true);
        }
        if (token === state.requestToken) handlePlaybackStartError(error);
        return;
      }
      if (token === state.requestToken) {
        if (userPaused()) video.pause();
        drawMainFrame();
        if (typeof verticalTimelineUserScrolling !== 'undefined') verticalTimelineUserScrolling = false;
        if (typeof updateTimelinePlayhead === 'function') updateTimelinePlayhead(selected);
        updateStatus(userPaused() ? 'Recording paused.' : 'Playing recording.');
        if (token !== state.requestToken) return;
        hidePreview();
      }
      return;
    }
    updateStatus(continuous ? 'Playing recording.' : 'Loading selected recording…');
    disableNativeControls(video);
    video.pause();
    video.onerror = null;
    video.dataset.playback = 'recording';
    if (typeof stopLivePlayback === 'function') stopLivePlayback(video);
    if (!sameSource) {
      video.src = url;
      video.load();
    }
    workspacePlaybackUrl = record.url;
    workspacePlaybackStartSeconds = timelineSeconds(record);
    try {
      if (sameSource && video.error) throw video.error;
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('recording load timed out')), converted ? 180000 : 15000);
        const done = (error) => { clearTimeout(timer); video.removeEventListener('loadedmetadata', ready); video.removeEventListener('error', failed); error ? reject(error) : resolve(); };
        const ready = () => done();
        const failed = () => done(video.error || new Error('recording could not be decoded'));
        video.addEventListener('loadedmetadata', ready, { once: true });
        video.addEventListener('error', failed, { once: true });
        if (video.readyState >= 1) ready();
      });
      if (token !== state.requestToken) return;
      if (Number.isFinite(video.duration)) video.currentTime = Math.min(offset, Math.max(0, video.duration - 0.05));
      video.pause();
      if (typeof setWorkspaceMode === 'function') setWorkspaceMode('recording');
      await startAndWaitForFrame(video);
      if (token !== state.requestToken) return;
      if (userPaused()) video.pause();
      drawMainFrame();
      if (typeof verticalTimelineUserScrolling !== 'undefined') verticalTimelineUserScrolling = false;
      if (typeof updateTimelinePlayhead === 'function') updateTimelinePlayhead(selected);
      updateStatus(userPaused() ? 'Recording paused.' : 'Playing recording.');
      if (token !== state.requestToken) return;
      hidePreview();
      log('commit:ready', { selected, offset });
    } catch (error) {
      if (token === state.requestToken && canRetryRecording(video.error || error, record, converted)) {
        updateStatus('Preparing compatible playback…');
        return commit(selected, true);
      }
      if (token === state.requestToken) handlePlaybackStartError(error);
      log('commit:error', error);
    }
  }

  function startAndWaitForFrame(video) {
    const frame = waitForDisplayedFrame(video);
    let playback;
    try { playback = Promise.resolve(video.play()); }
    catch (error) { playback = Promise.reject(error); }
    return Promise.all([frame, playback]);
  }

  function handlePlaybackStartError(error) {
    if (error?.name === 'NotAllowedError') {
      updateStatus('Playback paused · press Play to continue.');
      hidePreview();
    } else updateStatus(`Playback unavailable: ${error?.message || 'unable to start video'}`);
  }

  function waitForDisplayedFrame(video) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = error => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        video.removeEventListener('seeked', ready);
        video.removeEventListener('loadeddata', ready);
        video.removeEventListener('canplay', ready);
        video.removeEventListener('error', failed);
        error ? reject(error) : resolve();
      };
      const ready = () => { if (!video.seeking && video.readyState >= 2) finish(); };
      const failed = () => finish(video.error || new Error('frame decode failed'));
      const timer = setTimeout(() => finish(new Error('frame decode timed out')), 8000);
      video.addEventListener('seeked', ready);
      video.addEventListener('loadeddata', ready);
      video.addEventListener('canplay', ready);
      video.addEventListener('error', failed);
      if (!video.seeking && video.readyState >= 2) ready();
    });
  }

  // One gesture ends in one commit. A mouse wheel moves the timeline with
  // programmatic scrolls, and browsers report `scrollend` after each notch,
  // so wheel gestures wait for the wheel to go idle instead.
  function scheduleCommit(delay) {
    clearTimeout(state.commitTimer);
    state.commitTimer = setTimeout(() => {
      state.commitTimer = 0;
      if (state.pointerDown) return;
      const sinceWheel = performance.now() - state.lastWheelAt;
      if (sinceWheel < WHEEL_IDLE_MS) { scheduleCommit(WHEEL_IDLE_MS - sinceWheel + 10); return; }
      if (state.active && state.lastValue !== null && state.gesture?.moved && !state.gesture.ended) void commit(state.lastValue);
    }, delay);
  }

  function warmCurrentHours() {
    if (!state.camera) return;
    const now = Date.now() / 1000;
    requestHour(hourOf(now), 0);
    requestHour(hourOf(now) - 3600, 1);
  }

  // ---------------------------------------------------------------- install
  function install() {
    const video = mainVideo();
    if (!video || document.documentElement.dataset.playbackEngineInstalled) return;
    document.documentElement.dataset.playbackEngineInstalled = 'true';
    ensurePreview();
    const openWorkspace = window.openLiveWorkspace;
    if (typeof openWorkspace === 'function') {
      window.openLiveWorkspace = async (...args) => {
        const live = mainVideo();
        if (live) live.autoplay = true;
        state.prepared = null;
        const result = await openWorkspace(...args);
        if (window.workspaceCamera === args[0]) {
          state.camera = window.workspaceCamera;
          warmCurrentHours();
        }
        return result;
      };
    }
    window.previewWorkspaceTimeline = (value) => setTarget(value, 0);
    window.previewWorkspaceTimelineScrub = (value, velocity) => setTarget(value, velocity);
    window.scrubWorkspaceTimeline = (value) => commit(value);
    const returnToLive = window.returnWorkspaceToLive;
    window.returnWorkspaceToLive = async (...args) => {
      stopGapClock();
      state.requestToken += 1;
      clearTimeout(state.commitScheduleTimer);
      clearTimeout(state.commitTimer);
      state.active = false;
      state.pointerDown = false;
      state.lastValue = state.target = state.targetAbsolute = null;
      state.targetRecord = null;
      state.cursor = { t: null, selected: null, record: null, offset: 0, date: null };
      state.velocity = 0;
      state.direction = 0;
      if (state.gesture) state.gesture.ended = true;
      window.workspaceScrubActive = false;
      const live = mainVideo();
      if (live) {
        live.autoplay = true;
        // Live view always runs at normal speed.
        live.defaultPlaybackRate = 1;
        live.playbackRate = 1;
        const speed = document.querySelector('#workspace-speed');
        if (speed) speed.value = '1';
      }
      state.prepared = null;
      cancelBridge();
      const result = await returnToLive(...args);
      const current = mainVideo();
      if (current?.readyState >= 2) hidePreview();
      else current?.addEventListener('loadeddata', hidePreview, { once: true });
      return result;
    };
    window.verticalTimelineSeek = (value) => {
      setTarget(value, 0);
      clearTimeout(state.commitTimer);
    };
    const onTimeline = event => Boolean(event.target?.closest?.('.vertical-timeline-scroll'));
    document.addEventListener('wheel', event => { if (onTimeline(event)) state.lastWheelAt = performance.now(); }, { capture: true, passive: true });
    document.addEventListener('scroll', event => {
      if (!onTimeline(event)) return;
      if (typeof verticalTimelineProgrammatic !== 'undefined' && verticalTimelineProgrammatic) return;
      clearTimeout(state.commitTimer);
      if (state.pointerDown) return;
      // The document capture listener runs before the timeline's own scroll
      // handler; schedule after propagation so this scroll counts.
      clearTimeout(state.commitScheduleTimer);
      state.commitScheduleTimer = setTimeout(() => {
        state.commitScheduleTimer = 0;
        if (!state.pointerDown) scheduleCommit(COMMIT_IDLE_MS);
      }, 0);
    }, true);
    // Native and touch scrolling report the end of momentum, so commit soon
    // after it. Browsers also fire scrollend after every programmatic scroll
    // (each wheel notch, keyboard step or scripted move), so a short grace
    // period lets the next scroll of the same gesture cancel this commit.
    document.addEventListener('scrollend', event => {
      if (!onTimeline(event)) return;
      if (typeof verticalTimelineProgrammatic !== 'undefined' && verticalTimelineProgrammatic) return;
      if (state.pointerDown || !state.active || state.lastValue === null) return;
      if (!state.commitScheduleTimer && !state.commitTimer) return;
      clearTimeout(state.commitScheduleTimer);
      state.commitScheduleTimer = 0;
      scheduleCommit(performance.now() - state.lastWheelAt < WHEEL_IDLE_MS ? WHEEL_IDLE_MS : SCROLLEND_GRACE_MS);
    }, true);
    document.addEventListener('pointerdown', event => {
      if (!onTimeline(event)) return;
      state.pointerDown = true;
      gesture();
      clearTimeout(state.commitScheduleTimer);
      clearTimeout(state.commitTimer);
      state.commitScheduleTimer = state.commitTimer = 0;
    }, true);
    document.addEventListener('pointerup', () => {
      if (!state.pointerDown) return;
      state.pointerDown = false;
      clearTimeout(state.commitTimer);
      state.commitTimer = 0;
      // A click without movement must not restart normal playback.
      if (state.lastValue !== null && state.gesture?.moved && !state.gesture.ended) void commit(state.lastValue);
    }, true);
    document.addEventListener('pointercancel', () => { state.pointerDown = false; }, true);
    video.addEventListener('timeupdate', () => {
      if (video.dataset.playback === 'recording' && !state.active && typeof workspacePlaybackStartSeconds !== 'undefined') {
        updateTimelinePlayhead(workspacePlaybackStartSeconds + video.currentTime);
        // Keep the closing frame of a segment on the hidden overlay. Some
        // browsers cannot draw a video once it has ended, which showed black
        // while the next segment loaded.
        const remaining = (video.duration || 0) - video.currentTime;
        if (remaining > 0 && remaining < NEXT_PRELOAD_SECONDS) preloadNextSegment(video);
        if (remaining > 0 && remaining < 0.6 && video.readyState >= 2 && ensurePreview()) {
          drawSource(video, 0, 0, video.videoWidth || video.clientWidth, video.videoHeight || video.clientHeight);
        }
      }
    });
    // Keep the chosen playback speed across segment loads: loading a new
    // file resets playbackRate to defaultPlaybackRate.
    video.addEventListener('ratechange', () => {
      if (video.dataset.playback === 'recording' && video.playbackRate > 0 && video.defaultPlaybackRate !== video.playbackRate) {
        video.defaultPlaybackRate = video.playbackRate;
      }
    });
    video.addEventListener('ended', () => {
      if (state.active || userPaused() || video.dataset.playback !== 'recording' ||
          typeof workspaceTimelineRecords === 'undefined') return;
      const current = workspaceTimelineRecords.find(record => record.url === workspacePlaybackUrl);
      if (!current || !Number.isFinite(video.duration)) return;
      const next = nextRecording(timelineSeconds(current));
      const end = timelineSeconds(current) + video.duration;
      if (next && timelineSeconds(next) <= end + 0.1) void commit(timelineSeconds(next), false, true);
      else {
        const after = end + 0.1;
        if (after < timelineDayLength(workspaceTimelineDate)) void commit(after, false, true);
        else { setTarget(timelineDayLength(workspaceTimelineDate) - 0.001, 0, true); startGapClock(); }
      }
    });
    window.addEventListener('resize', () => { fitCanvas(); wake(); }, { passive: true });
    new MutationObserver(() => {
      disableNativeControls(mainVideo());
      if (state.active) fitCanvas();
    }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['controls', 'src'] });
    log('installed');
  }

  const style = document.createElement('style');
  style.textContent = `
    .workspace-scrub-frame { position:absolute; inset:0; z-index:5; width:100%; height:100%; object-fit:contain; display:none; pointer-events:none; background:#030507; }
    .workspace-scrub-frame.visible { display:block; }
    .workspace-scrub-frame.stale { filter:grayscale(1) brightness(.45); }
    .workspace-scrub-message { position:absolute; left:50%; bottom:16px; transform:translateX(-50%); z-index:6; display:none; align-items:center; justify-content:center; padding:8px 14px; border-radius:8px; color:#d5dce7; background:#030507dd; font-size:clamp(14px,1.5vw,20px); font-weight:600; text-align:center; pointer-events:none; }
    .workspace-scrub-message.visible { display:flex; }
    .workspace-scrub-proxy { position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; }
    #workspace-live, #workspace-live::-webkit-media-controls, #workspace-live::-webkit-media-controls-enclosure { -webkit-appearance:none; }
    #workspace-live::-webkit-media-controls { display:none !important; }
  `;
  document.head.appendChild(style);
  const observer = new MutationObserver(install);
  observer.observe(document.body, { childList: true, subtree: true });
  install();
})();
