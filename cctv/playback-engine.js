/*
 * Persistent playback engine for the camera workspace.
 *
 * The dashboard stores recordings as individual MP4/MKV segments, not as one
 * HLS/DASH VOD presentation.  A manifest player would therefore not solve
 * seeking by itself.  Instead we keep the live/normal player stable and use a
 * a dense frame atlas plus a canvas for bidirectional scrubbing. The proxy
 * and server-extracted frames remain fallbacks while an atlas is prepared.
 */
(function () {
  'use strict';

  const debug = new URLSearchParams(location.search).has('debugPlayback');
  const state = {
    canvas: null,
    ctx: null,
    scrubMessage: null,
    frame: 0,
    frameDelay: 0,
    target: null,
    targetRecord: null,
    targetOffset: 0,
    targetAbsolute: null,
    inFlight: 0,
    pending: null,
    lastRequestedAt: 0,
    lastFrameTime: null,
    lastPreviewAt: 0,
    lastBitmap: null,
    motionDirection: 0,
    motionEpoch: 0,
    camera: null,
    frameCache: new Map(),
    frameLoads: new Map(),
    requestToken: 0,
    lastInputAt: 0,
    lastValue: null,
    lastTime: 0,
    committing: false,
    gapPlaying: false,
    gapTimer: 0,
    gapLastTick: 0,
    gapTicking: false,
    commitTimer: 0,
    commitScheduleTimer: 0,
    preloadTimer: 0,
    preloadPath: null,
    pointerDown: false,
    active: false,
    hasFrame: false,
    hasPicture: false,
    proxy: null,
    proxyPath: null,
    proxyReady: false,
    proxyLoading: false,
    proxyChecking: null,
    proxyRetry: 0,
    proxyWaiting: false,
    proxyFailed: false,
    proxyDesired: null,
    proxySeeking: false,
    proxyFrame: 0,
    proxyOpenTimer: 0,
    proxyOpenPath: null,
    atlasPath: null,
    atlasManifest: null,
    atlasBase: null,
    atlasChecking: null,
    atlasWaiting: false,
    atlasRetry: 0,
    atlasFailed: false,
    atlasCache: new Map(),
    atlasLoads: new Map(),
    atlasManifests: new Map(),
    atlasChecks: new Map(),
    atlasPrefetching: new Set(),
    atlasPrefetchQueue: new Map(),
    atlasRecordList: null,
    atlasRecordIndices: new Map(),
    atlasPrefetchRetry: new Map(),
    atlasWarmTimer: 0,
    atlasWarmPath: null,
    atlasWarmRetry: new Map(),
    atlasFrame: 0,
  };

  function log(event, details) {
    if (debug) console.debug('[playback]', event, details || '');
  }

  function mainVideo() { return document.querySelector('#workspace-live'); }
  function shell() { return mainVideo()?.closest('.workspace-video-shell'); }
  function sourceFor(record) {
    const url = String(record?.url || '');
    // Recorder output is normally already browser-playable MP4. Do not send
    // those files through the server's FFmpeg conversion path: that makes
    // Safari wait for a full transcode before it can seek. MKV recordings
    // still use the conversion endpoint for browser compatibility.
    if (/\.mp4(?:[?#]|$)/i.test(url)) return url;
    return `${url}${url.includes('?') ? '&' : '?'}format=mp4`;
  }

  function instrument(video, name) {
    if (!video || video.dataset.playbackDiagnostics) return;
    video.dataset.playbackDiagnostics = name;
    ['loadstart', 'loadedmetadata', 'durationchange', 'canplay', 'playing', 'pause',
      'seeking', 'seeked', 'waiting', 'stalled', 'error', 'emptied'].forEach(type => {
      video.addEventListener(type, () => log(`${name}:${type}`, {
        src: video.currentSrc || video.src,
        currentTime: Number(video.currentTime || 0).toFixed(3),
        readyState: video.readyState,
        networkState: video.networkState,
        error: video.error?.code || null,
      }));
    });
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
    instrument(video, 'main');
    if (!state.canvas) {
      state.canvas = document.createElement('canvas');
      state.canvas.className = 'workspace-scrub-frame';
      state.canvas.setAttribute('aria-hidden', 'true');
      host.appendChild(state.canvas);
      state.ctx = state.canvas.getContext('2d', { alpha: false });
    }
    if (!state.scrubMessage) {
      state.scrubMessage = document.createElement('div');
      state.scrubMessage.className = 'workspace-scrub-message';
      state.scrubMessage.setAttribute('role', 'status');
      state.scrubMessage.setAttribute('aria-live', 'polite');
      host.appendChild(state.scrubMessage);
    }
    return true;
  }

  function scrubMessage(message) {
    if (!state.scrubMessage) return;
    state.scrubMessage.textContent = message || '';
    state.scrubMessage.classList.toggle('visible', Boolean(message));
  }

  function fitCanvas() {
    const video = mainVideo(), canvas = state.canvas;
    if (!video || !canvas) return;
    const rawWidth = Math.max(1, video.clientWidth * devicePixelRatio);
    const rawHeight = Math.max(1, video.clientHeight * devicePixelRatio);
    // Preview sources are small; a full Retina-sized canvas needlessly paints
    // millions of extra pixels on every scroll. CSS still fills the player.
    const scale = Math.min(1, 1280 / rawWidth, 720 / rawHeight);
    const width = Math.max(1, Math.round(rawWidth * scale));
    const height = Math.max(1, Math.round(rawHeight * scale));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
      if (state.active && state.lastBitmap) paintBitmap(state.lastBitmap);
      if (state.active && state.atlasManifest) scheduleAtlasFrame();
    }
  }

  function paintBitmap(bitmap) {
    const canvas = state.canvas;
    const scale = Math.min(canvas.width / bitmap.width, canvas.height / bitmap.height);
    const width = bitmap.width * scale, height = bitmap.height * scale;
    state.ctx.fillStyle = '#030507';
    state.ctx.fillRect(0, 0, canvas.width, canvas.height);
    state.ctx.drawImage(bitmap, (canvas.width - width) / 2, (canvas.height - height) / 2, width, height);
  }

  function drawImage(source) {
    const video = mainVideo();
    if (!state.ctx || !source || !video || source.readyState < 2) return false;
    fitCanvas();
    const sw = source.videoWidth || source.clientWidth;
    const sh = source.videoHeight || source.clientHeight;
    if (!sw || !sh) return false;
    const cw = state.canvas.width, ch = state.canvas.height;
    const scale = Math.min(cw / sw, ch / sh);
    const dw = sw * scale, dh = sh * scale;
    state.ctx.fillStyle = '#030507';
    state.ctx.fillRect(0, 0, cw, ch);
    state.ctx.drawImage(source, (cw - dw) / 2, (ch - dh) / 2, dw, dh);
    state.hasFrame = true;
    state.hasPicture = true;
    if (source !== video) state.lastPreviewAt = performance.now();
    if (state.active) state.canvas.classList.add('visible');
    scrubMessage('');
    return true;
  }

  function drawMainFrame() {
    const video = mainVideo();
    if (state.active && video?.readyState >= 2) drawImage(video);
  }

  function drawExtractedFrame(bitmap, request) {
    if (!state.active || !state.ctx || !bitmap) return;
    fitCanvas();
    paintBitmap(bitmap);
    state.hasFrame = true;
    state.hasPicture = true;
    state.lastBitmap = bitmap;
    state.lastFrameTime = request.absolute;
    state.lastPreviewAt = performance.now();
    state.canvas.classList.add('visible');
    scrubMessage(request.absolute === state.targetAbsolute ? '' : `Preview · ${formatTimelineTime(request.selected)}`);
    log('scrub:frame', { requested: request.selected, displayed: state.lastFrameTime,
      latencyMs: Math.round(performance.now() - request.startedAt) });
  }

  function frameKey(request) {
    return `${request.record.path}:${Math.round(request.offset * 4)}`;
  }

  function absoluteTimelineSeconds(date, value) {
    return new Date(`${date}T00:00:00`).getTime() / 1000 + Number(value);
  }

  function atlasSheetName(index) { return `${String(index).padStart(3, '0')}.jpg`; }

  function atlasKey(path, index) { return `${path}:${index}`; }

  function prefetchNearbyAtlas(record, direction) {
    if (!direction || typeof workspaceTimelineRecords === 'undefined') return;
    if (state.atlasRecordList !== workspaceTimelineRecords || state.atlasRecordIndices.size !== workspaceTimelineRecords.length) {
      state.atlasRecordList = workspaceTimelineRecords;
      state.atlasRecordIndices = new Map(workspaceTimelineRecords.map((item, index) => [item.path, index]));
    }
    const index = state.atlasRecordIndices.get(record.path) ?? -1;
    if (index < 0) return;
    // Ten 30-second segments cover approximately five minutes. Retarget the
    // pending queue whenever the user changes position or direction.
    state.atlasPrefetchQueue.clear();
    for (let step = 1; step <= 10; step++) {
      const next = workspaceTimelineRecords[index + Math.sign(direction) * step];
      if (next) prefetchAtlas(next, direction, Number(state.target) + Math.sign(direction) * 24 * step);
    }
  }

  function prefetchAtlas(next, direction, predictedSeconds) {
    if (!next || state.atlasPrefetching.has(next.path) ||
        (state.atlasPrefetchRetry.get(next.path) || 0) > performance.now()) return;
    if (state.atlasPrefetching.size >= 4) {
      state.atlasPrefetchQueue.set(next.path, {next, direction, predictedSeconds});
      return;
    }
    state.atlasPrefetching.add(next.path);
    const loadManifest = state.atlasManifests.has(next.path) ? Promise.resolve(state.atlasManifests.get(next.path)) :
      api(`/api/recordings/scrub-frames?prepare=0&path=${encodeURIComponent(next.path)}`)
        .then(response => response.ok ? response.json() : null);
    loadManifest.then(result => {
      if (!result?.ready || !result.manifest?.frames || !result.urlBase) {
        // A segment may finish building moments after this check. Retry soon
        // instead of suppressing a newly available preview for half a minute.
        state.atlasPrefetchRetry.set(next.path, performance.now() + 5000);
        if (state.atlasPrefetchRetry.size > 128) state.atlasPrefetchRetry.delete(state.atlasPrefetchRetry.keys().next().value);
        return;
      }
      state.atlasManifests.set(next.path, result);
      if (state.atlasManifests.size > 16) state.atlasManifests.delete(state.atlasManifests.keys().next().value);
      if (state.atlasPath === next.path) {
        state.atlasManifest = result.manifest;
        state.atlasBase = result.urlBase;
        scheduleAtlasFrame();
      }
      // A mouse-wheel notch moves about 24 seconds. Warm the sheet the next
      // notch is likely to land on, including the middle of a segment.
      const predicted = predictedSeconds - timelineSeconds(next);
      const perSheet = result.manifest.columns * result.manifest.rows;
      const frame = Math.max(0, Math.min(result.manifest.frames - 1, Math.floor(predicted * result.manifest.fps)));
      const sheet = Number.isFinite(frame / perSheet) && perSheet > 0
        ? Math.floor(frame / perSheet) : (direction < 0 ? result.manifest.sheets - 1 : 0);
      const key = atlasKey(next.path, sheet);
      if (state.atlasCache.has(key) || state.atlasLoads.has(key)) return;
      const job = api(result.urlBase + atlasSheetName(sheet), { credentials: 'same-origin' })
        .then(response => { if (!response.ok) throw new Error('preview unavailable'); return response.blob(); })
        .then(blob => createImageBitmap(blob)).then(bitmap => {
          state.atlasCache.set(key, bitmap);
          if (state.atlasCache.size > 12) {
            const oldest = state.atlasCache.keys().next().value;
            state.atlasCache.get(oldest)?.close?.();
            state.atlasCache.delete(oldest);
          }
          if (state.atlasPath === next.path) scheduleAtlasFrame();
        }).catch(() => {}).finally(() => state.atlasLoads.delete(key));
      state.atlasLoads.set(key, job);
      return job;
    }).catch(() => {}).finally(() => {
      state.atlasPrefetching.delete(next.path);
      while (state.atlasPrefetching.size < 4 && state.atlasPrefetchQueue.size) {
        const [path, queued] = state.atlasPrefetchQueue.entries().next().value;
        state.atlasPrefetchQueue.delete(path);
        prefetchAtlas(queued.next, queued.direction, queued.predictedSeconds);
      }
    });
  }

  function warmRecentAtlases() {
    if (typeof workspaceTimelineRecords === 'undefined' || !workspaceTimelineRecords.length) return;
    // Start with recent segments while the live view is
    // already visible, so the first backward wheel notches need less waiting.
    state.atlasPrefetchQueue.clear();
    const recent = workspaceTimelineRecords.slice(-10).reverse();
    const latest = recent[0];
    const duration = Number(latest.duration) || Number(typeof workspaceTimelineSegmentSeconds === 'undefined' ? 30 : workspaceTimelineSegmentSeconds) || 30;
    const live = typeof liveTimelineSeconds === 'function' ? liveTimelineSeconds() : null;
    const start = live ?? (timelineSeconds(latest) + duration);
    recent.forEach((record, index) => {
      prefetchAtlas(record, -1, start - 24 * (index + 1));
    });
  }

  function prepareVisitedAtlas(record) {
    if (state.atlasManifest || state.atlasManifests.has(record.path) || state.atlasFailed ||
        state.atlasWarmPath === record.path || (state.atlasWarmRetry.get(record.path) || 0) > performance.now()) return;
    clearTimeout(state.atlasWarmTimer);
    state.atlasWarmPath = record.path;
    state.atlasWarmTimer = setTimeout(() => {
      state.atlasWarmPath = null;
      if (!state.active || state.targetRecord?.path !== record.path) return;
      state.atlasWarmRetry.set(record.path, performance.now() + 10000);
      if (state.atlasWarmRetry.size > 128) state.atlasWarmRetry.delete(state.atlasWarmRetry.keys().next().value);
      // One server-wide worker prepares a visited segment in the background.
      // Never block preview or recording while it is being prepared.
      api(`/api/recordings/scrub-frames?prepare=1&path=${encodeURIComponent(record.path)}`)
        .then(response => response.ok ? response.json() : null)
        .then(result => {
          // A background build may finish soon; do not retain an earlier
          // negative prefetch result when this segment is revisited.
          state.atlasPrefetchRetry.delete(record.path);
          if (!result?.ready || !result.manifest?.frames || !result.urlBase) return;
          state.atlasManifests.set(record.path, result);
          if (state.atlasPath === record.path) {
            state.atlasManifest = result.manifest;
            state.atlasBase = result.urlBase;
            scheduleAtlasFrame();
          }
        }).catch(() => {});
    }, 150);
  }

  function loadAtlasSheet(index) {
    const manifest = state.atlasManifest, path = state.atlasPath, base = state.atlasBase;
    const key = atlasKey(path, index);
    if (!manifest || index < 0 || index >= manifest.sheets || state.atlasCache.has(key) || state.atlasLoads.has(key)) return;
    const job = api(base + atlasSheetName(index), { credentials: 'same-origin' })
      .then(response => { if (!response.ok) throw new Error(`atlas ${response.status}`); return response.blob(); })
      .then(blob => createImageBitmap(blob))
      .then(bitmap => {
        // Retain decoded sheets across short recording boundaries. A wheel
        // gesture commonly crosses several 30-second files and then reverses.
        state.atlasCache.set(key, bitmap);
        if (state.atlasCache.size > 12) {
          const oldest = state.atlasCache.keys().next().value;
          state.atlasCache.get(oldest)?.close?.();
          state.atlasCache.delete(oldest);
        }
        if (state.atlasPath === path && state.atlasBase === base) scheduleAtlasFrame();
      }).catch(error => log('scrub:atlas-sheet-error', error.message))
      .finally(() => state.atlasLoads.delete(key));
    state.atlasLoads.set(key, job);
  }

  function scheduleAtlasFrame() {
    if (state.atlasFrame || !state.active || !state.atlasManifest) return;
    state.atlasFrame = requestAnimationFrame(() => {
      state.atlasFrame = 0;
      const manifest = state.atlasManifest;
      if (!state.active || !manifest || state.targetRecord?.path !== state.atlasPath) return;
      const perSheet = manifest.columns * manifest.rows;
      const index = Math.min(manifest.frames - 1, Math.max(0, Math.floor(state.targetOffset * manifest.fps)));
      const sheet = Math.floor(index / perSheet);
      const key = atlasKey(state.atlasPath, sheet);
      const bitmap = state.atlasCache.get(key);
      if (!bitmap) { loadAtlasSheet(sheet); return; }
      state.atlasCache.delete(key);
      state.atlasCache.set(key, bitmap);
      const cell = index % perSheet;
      fitCanvas();
      const cw = state.canvas.width, ch = state.canvas.height;
      const scale = Math.min(cw / manifest.width, ch / manifest.height);
      const dw = manifest.width * scale, dh = manifest.height * scale;
      state.ctx.fillStyle = '#030507';
      state.ctx.fillRect(0, 0, cw, ch);
      state.ctx.drawImage(bitmap, (cell % manifest.columns) * manifest.width,
        Math.floor(cell / manifest.columns) * manifest.height, manifest.width, manifest.height,
        (cw - dw) / 2, (ch - dh) / 2, dw, dh);
      state.hasFrame = true;
      state.hasPicture = true;
      state.canvas.classList.add('visible');
      scrubMessage('');
      state.lastFrameTime = state.targetAbsolute;
      state.lastPreviewAt = performance.now();
      loadAtlasSheet(sheet + (state.motionDirection >= 0 ? 1 : -1));
    });
  }

  function ensureAtlas(record) {
    if (!record || !ensurePreview()) return false;
    if (state.atlasPath !== record.path) {
      state.atlasPath = record.path;
      state.atlasManifest = null;
      state.atlasBase = null;
      state.atlasChecking = state.atlasChecks.get(record.path) || null;
      state.atlasWaiting = false;
      state.atlasFailed = false;
      clearTimeout(state.atlasRetry);
      if (state.atlasFrame) cancelAnimationFrame(state.atlasFrame);
      state.atlasFrame = 0;
      const cached = state.atlasManifests.get(record.path);
      if (cached) {
        state.atlasManifest = cached.manifest;
        state.atlasBase = cached.urlBase;
        state.atlasManifests.delete(record.path);
        state.atlasManifests.set(record.path, cached);
      }
    }
    if (state.atlasManifest) return true;
    if (state.atlasFailed || state.atlasWaiting || state.atlasChecking ||
        (state.atlasPrefetchRetry.get(record.path) || 0) > performance.now()) return false;
    const path = record.path;
    const check = api(`/api/recordings/scrub-frames?prepare=0&path=${encodeURIComponent(path)}`)
      .then(response => response.ok ? response.json() : null)
      .then(result => {
        if (result?.ready && result.manifest?.frames && result.urlBase) {
          state.atlasManifests.set(path, result);
          if (state.atlasManifests.size > 16) state.atlasManifests.delete(state.atlasManifests.keys().next().value);
          if (state.atlasPath === path) {
            state.atlasManifest = result.manifest;
            state.atlasBase = result.urlBase;
            scheduleAtlasFrame();
          }
        } else if (result?.failed && state.atlasPath === path) state.atlasFailed = true;
        else {
          if (state.atlasPath !== path) return;
          state.atlasPrefetchRetry.set(path, performance.now() + 5000);
          if (state.atlasPrefetchRetry.size > 128) state.atlasPrefetchRetry.delete(state.atlasPrefetchRetry.keys().next().value);
          state.atlasWaiting = true;
          state.atlasRetry = setTimeout(() => {
            state.atlasWaiting = false;
            if (state.atlasPath === path && state.active) ensureAtlas(record);
          }, 5000);
        }
      }).catch(error => log('scrub:atlas-error', error.message))
      .finally(() => {
        state.atlasChecks.delete(path);
        if (state.atlasPath === path) state.atlasChecking = null;
      });
    state.atlasChecking = check;
    state.atlasChecks.set(path, check);
    return false;
  }

  function seekProxy() {
    if (state.proxyFrame || !state.proxyReady || state.proxySeeking || !state.proxy || !state.active || state.atlasManifest) return;
    state.proxyFrame = requestAnimationFrame(() => {
      state.proxyFrame = 0;
      const desired = state.proxyDesired;
      if (desired === null || !state.proxyReady || state.proxySeeking || !state.active) return;
      const proxy = state.proxy;
      const target = Math.max(0, Math.min(desired, Math.max(0, proxy.duration - 0.125)));
      if (Math.abs(proxy.currentTime - target) < 0.07 && proxy.readyState >= 2) {
        drawImage(proxy);
        return;
      }
      state.proxySeeking = true;
      try { proxy.currentTime = target; }
      catch { state.proxySeeking = false; }
    });
  }

  function ensureProxy(record, direct = false) {
    if (!record || !ensurePreview()) return false;
    if (!state.proxy && !document.createElement('video').addEventListener) return false;
    if (state.proxyPath !== record.path) {
      state.proxyPath = record.path;
      state.proxyReady = false;
      state.proxyLoading = false;
      state.proxyDesired = null;
      state.proxySeeking = false;
      state.proxyChecking = null;
      state.proxyWaiting = false;
      state.proxyFailed = false;
      clearTimeout(state.proxyRetry);
      if (state.proxyFrame) cancelAnimationFrame(state.proxyFrame);
      state.proxyFrame = 0;
      if (state.proxy) {
        state.proxy.removeAttribute('src');
        state.proxy.load();
      }
    }
    if (state.proxyReady) return true;
    if (state.proxyFailed || state.proxyWaiting) return false;
    if (state.proxyLoading) return false;
    if (state.proxyChecking) return false;
    // A browser-playable recording can provide frames immediately while its
    // dense atlas is being generated. This avoids waiting for a full encode.
    const path = record.path;
    const source = direct ? Promise.resolve({ ready: true, url: record.url }) :
      api(`/api/recordings/trickplay?path=${encodeURIComponent(path)}`)
        .then(response => response.ok ? response.json() : null);
    state.proxyChecking = source
      .then(result => {
        if (state.proxyPath !== path) return;
        if (result?.ready && result.url) {
          if (!state.proxy) {
            const proxy = document.createElement('video');
            if (!proxy.addEventListener) return;
            proxy.className = 'workspace-scrub-proxy';
            proxy.muted = true;
            proxy.preload = 'auto';
            proxy.setAttribute('playsinline', '');
            shell().appendChild(proxy);
            proxy.addEventListener('loadedmetadata', () => {
              state.proxyReady = true;
              state.proxyLoading = false;
              state.proxyDesired = state.targetOffset;
              seekProxy();
            });
            proxy.addEventListener('seeked', () => {
              state.proxySeeking = false;
              if (state.active && state.proxyReady && !state.atlasManifest && state.targetRecord?.path === state.proxyPath) {
                // Show every decoded intermediate frame during movement. A
                // direction change starts seeking toward the newest target.
                drawImage(proxy);
                seekProxy();
              }
            });
            proxy.addEventListener('error', () => { state.proxyReady = false; state.proxyLoading = false; state.proxySeeking = false; state.proxyFailed = true; if (state.active && state.pending) scheduleFrame(); });
            state.proxy = proxy;
          }
          state.proxyLoading = true;
          state.proxy.src = result.url;
          state.proxy.load();
        } else if (result?.failed) {
          state.proxyFailed = true;
        } else {
          state.proxyWaiting = true;
          state.proxyRetry = setTimeout(() => {
            state.proxyWaiting = false;
            if (state.proxyPath === path && state.active) ensureProxy(record);
          }, 2000);
        }
      }).catch(error => log('scrub:proxy-error', error.message))
      .finally(() => { if (state.proxyPath === path) state.proxyChecking = null; });
    return false;
  }

  function requestProxy(record, direct) {
    if (state.proxyPath === record.path) return ensureProxy(record, direct);
    if (state.proxyOpenPath === record.path) return false;
    clearTimeout(state.proxyOpenTimer);
    state.proxyOpenPath = record.path;
    state.proxyOpenTimer = setTimeout(() => {
      state.proxyOpenPath = null;
      if (!state.active || state.targetRecord?.path !== record.path || state.atlasManifest) return;
      if (ensureProxy(record, direct)) {
        state.proxyDesired = state.targetOffset;
        seekProxy();
      }
    }, 120);
    return false;
  }

  async function fetchFrame(request) {
    state.inFlight += 1;
    request.startedAt = performance.now();
    try {
      const key = frameKey(request);
      let bitmap = state.frameCache.get(key);
      if (!bitmap) {
        let pending = state.frameLoads.get(key);
        if (!pending) {
          const url = `/api/recordings/snapshot?preview=1&path=${encodeURIComponent(request.record.path)}&at=${request.offset.toFixed(2)}`;
          pending = api(url, { credentials: 'same-origin' })
            .then(response => { if (!response.ok) throw new Error(`frame request ${response.status}`); return response.blob(); })
            .then(blob => createImageBitmap(blob)).then(image => {
              state.frameCache.set(key, image);
              if (state.frameCache.size > 80) {
                const oldest = state.frameCache.keys().next().value;
                const evicted = state.frameCache.get(oldest);
                if (evicted !== state.lastBitmap) evicted?.close?.();
                state.frameCache.delete(oldest);
              }
              return image;
            }).finally(() => state.frameLoads.delete(key));
          state.frameLoads.set(key, pending);
        }
        bitmap = await pending;
      } else {
        state.frameCache.delete(key);
        state.frameCache.set(key, bitmap);
      }
      // A frame can trail the cursor during a fast move, but cannot replace a
      // newer frame or a frame from the opposite side after direction changes.
      const alreadyPassed = state.lastFrameTime !== null && request.direction &&
        Math.sign(request.absolute - state.lastFrameTime) === -request.direction;
      if (state.active && request.epoch === state.motionEpoch && !alreadyPassed && state.targetRecord &&
          request.camera === state.camera) drawExtractedFrame(bitmap, request);
    } catch (error) { log('scrub:frame-error', error.message); }
    finally {
      state.inFlight -= 1;
      if (state.pending && state.active) scheduleFrame();
    }
  }

  function scheduleFrame() {
    if (state.pending && state.frameDelay && state.frameCache.has(frameKey(state.pending))) {
      clearTimeout(state.frameDelay);
      state.frameDelay = 0;
    }
    if (state.frame || state.frameDelay || !state.pending ||
        (state.inFlight >= 2 && !state.frameCache.has(frameKey(state.pending)))) return;
    state.frame = requestAnimationFrame(() => {
      state.frame = 0;
      if (!state.active || !state.pending ||
          (state.inFlight >= 2 && !state.frameCache.has(frameKey(state.pending)))) return;
      const request = state.pending;
      const interval = state.frameCache.has(frameKey(request)) ? 0 : Math.max(0, 180 - (performance.now() - state.lastRequestedAt));
      if (interval) {
        state.frameDelay = setTimeout(() => { state.frameDelay = 0; scheduleFrame(); }, interval);
        return;
      }
      state.pending = null;
      state.lastRequestedAt = performance.now();
      void fetchFrame(request);
    });
  }

  function showPreview() {
    if (!ensurePreview()) return;
    state.active = true;
    window.workspaceScrubActive = true;
    if (!state.hasFrame) drawMainFrame();
    if (state.hasFrame) state.canvas.classList.add('visible');
  }

  function hidePreview() {
    state.active = false;
    window.workspaceScrubActive = false;
    clearTimeout(state.commitScheduleTimer);
    clearTimeout(state.commitTimer);
    clearTimeout(state.preloadTimer);
    clearTimeout(state.atlasWarmTimer);
    state.atlasWarmPath = null;
    state.preloadPath = null;
    state.pending = null;
    state.lastFrameTime = null;
    state.motionDirection = 0;
    state.proxyDesired = null;
    clearTimeout(state.proxyRetry);
    clearTimeout(state.proxyOpenTimer);
    state.proxyOpenPath = null;
    state.proxyWaiting = false;
    if (state.proxyFrame) cancelAnimationFrame(state.proxyFrame);
    state.proxyFrame = 0;
    if (state.atlasFrame) cancelAnimationFrame(state.atlasFrame);
    state.atlasFrame = 0;
    clearTimeout(state.atlasRetry);
    state.atlasWaiting = false;
    if (state.canvas) state.canvas.classList.remove('visible');
    scrubMessage('');
    if (state.frame) cancelAnimationFrame(state.frame);
    state.frame = 0;
    clearTimeout(state.frameDelay);
    state.frameDelay = 0;
  }

  function preparePlayback(record) {
    if (!record || !/\.mp4(?:[?#]|$)/i.test(record.url || '')) return;
    const video = mainVideo();
    if (!video) return;
    if (video.dataset.playback === 'recording' && workspacePlaybackUrl === record.url) {
      clearTimeout(state.preloadTimer);
      state.preloadPath = null;
      return;
    }
    if (state.preloadPath === record.path) return;
    clearTimeout(state.preloadTimer);
    state.preloadPath = record.path;
    state.preloadTimer = setTimeout(() => {
      state.preloadTimer = 0;
      if (!state.active || state.targetRecord?.path !== record.path) return;
      // Keep the preview canvas visible while the normal player opens the
      // recording. Commit can reuse this load instead of starting it again.
      video.dataset.playback = 'recording';
      if (typeof stopLivePlayback === 'function') stopLivePlayback(video);
      video.pause();
      video.onerror = null;
      workspacePlaybackUrl = record.url;
      workspacePlaybackStartSeconds = timelineSeconds(record);
      video.src = sourceFor(record);
      video.load();
      if (typeof setWorkspaceMode === 'function') setWorkspaceMode('recording');
    }, 220);
  }

  function recordingOffset(record, selected) {
    const offset = Math.max(0, selected - timelineSeconds(record));
    const duration = Number(record.duration);
    return Number.isFinite(duration) && duration > 0 ? Math.min(offset, Math.max(0, duration - 0.04)) : offset;
  }

  function setTarget(value, velocity, confirmedGap = false) {
    if (!state.gapTicking) stopGapClock();
    const selected = Math.max(0, Math.min(timelineDayLength(workspaceTimelineDate) - 0.001, Number(value) || 0));
    const record = timelineRecordAt(selected);
    const wasGap = state.target !== null && !state.targetRecord;
    // A seek started before this movement may still be decoding. Its result
    // must never hide or overwrite the frame selected by the new movement.
    if (selected !== state.target || record?.path !== state.targetRecord?.path) {
      state.requestToken += 1;
      clearTimeout(state.commitTimer);
    }
    if (state.camera !== window.workspaceCamera) {
      state.camera = window.workspaceCamera;
      state.motionEpoch += 1;
      state.lastFrameTime = null;
      state.lastBitmap = null;
      state.hasFrame = false;
      state.hasPicture = false;
      state.canvas?.classList.remove('visible');
    }
    const absolute = absoluteTimelineSeconds(workspaceTimelineDate, selected);
    const elapsed = state.lastInputAt ? Math.max(16, performance.now() - state.lastInputAt) : 0;
    const speed = elapsed && state.targetAbsolute !== null ? Math.abs(absolute - state.targetAbsolute) * 1000 / elapsed : 0;
    const direction = state.targetAbsolute === null ? 0 : Math.sign(absolute - state.targetAbsolute);
    if (direction && state.motionDirection && direction !== state.motionDirection) state.motionEpoch += 1;
    if (direction) state.motionDirection = direction;
    state.lastInputAt = performance.now();
    state.lastValue = selected;
    state.target = selected;
    state.targetAbsolute = absolute;
    state.targetRecord = record;
    state.targetOffset = record ? recordingOffset(record, selected) : 0;
    const playing = mainVideo();
    if (playing?.dataset.playback === 'recording' && !playing.paused) playing.pause();
    showPreview();
    if (typeof workspaceTimelineLastValue !== 'undefined') workspaceTimelineLastValue = selected;
    if (typeof verticalTimelineSetPosition === 'function') verticalTimelineSetPosition(selected, false);
    if (typeof updateTimelinePlayhead === 'function') updateTimelinePlayhead(selected);
    if (!record) {
      clearTimeout(state.preloadTimer);
      state.preloadPath = null;
      log('scrub:gap', { selected });
      state.pending = null;
      if (state.frame) cancelAnimationFrame(state.frame);
      state.frame = 0;
      const date = workspaceTimelineDate;
      const loading = document.querySelector(`.vertical-timeline-day-layer[data-timeline-day="${date}"]`)?.dataset.loading === 'true';
      if (loading) {
        // Keep the last picture in place while older days are being indexed.
        scrubMessage('Loading recordings…');
        return;
      }
      // Keep the last picture visible, with an explicit gap label, instead of
      // flashing black for brief gaps between recording segments.
      const hadFrame = state.hasPicture;
      if (!hadFrame) {
        fitCanvas();
        state.ctx.fillStyle = '#030507';
        state.ctx.fillRect(0, 0, state.canvas.width, state.canvas.height);
      }
      state.lastFrameTime = null;
      state.hasFrame = true;
      state.canvas.classList.add('visible');
      if (confirmedGap) {
        scrubMessage(`No recording · ${date} ${formatTimelineTime(selected)}${hadFrame ? ' · last frame shown' : ''}`);
        updateStatus('No recording is available at the selected time.');
      } else {
        scrubMessage('');
        updateStatus('Scrolling · last available frame shown.');
      }
      return;
    }
    scrubMessage('');
    preparePlayback(record);
    if (wasGap) updateStatus('Loading recording preview…');
    const atlasReady = ensureAtlas(record);
    if (!atlasReady) prepareVisitedAtlas(record);
    prefetchNearbyAtlas(record, state.motionDirection);
    if (atlasReady) {
      scheduleAtlasFrame();
    }
    // Seek the original MP4 while the atlas builds. Older MKV recordings use
    // snapshots until a compatible proxy is available.
    const direct = /\.mp4(?:[?#]|$)/i.test(record.url || '');
    const proxyReady = !atlasReady && (state.atlasFailed ? requestProxy(record, false) : (direct && requestProxy(record, true)));
    state.proxyDesired = state.targetOffset;
    if (proxyReady) {
      seekProxy();
    }
    // Fast scrolling crosses minutes of footage per second. Reusing a
    // nearby frame avoids redundant decodes; committed playback stays exact.
    const step = speed > 120 ? 2 : speed > 30 ? 1 : 0.25;
    const offset = Math.floor(state.targetOffset / step) * step;
    const difference = state.targetOffset - offset;
    const request = { selected: selected - difference, record, offset,
      absolute: absolute - difference, camera: state.camera, direction: state.motionDirection, epoch: state.motionEpoch };
    if (state.frameCache.has(frameKey(request)) || !state.lastPreviewAt || performance.now() - state.lastPreviewAt >= 200) {
      state.pending = request;
      scheduleFrame();
    }
  }

  function updateStatus(message) {
    const status = document.querySelector('#workspace-player');
    if (status && message) status.innerHTML = `<span class="notice">${esc(message)}</span>`;
  }

  function userPaused() {
    return typeof workspaceUserPaused !== 'undefined' && workspaceUserPaused;
  }

  // Find the next actual segment without skipping the time between segments.
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
    const tick = async () => {
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
          stopGapClock();
          showGapControl();
          updateStatus('Unable to load the next day of recordings.');
          return;
        }
        state.gapLastTick = performance.now();
      }
      if (typeof liveTimelineSeconds === 'function') {
        const live = liveTimelineSeconds();
        if (live !== null && selected >= live) {
          stopGapClock();
          void window.returnWorkspaceToLive();
          return;
        }
      }
      const record = timelineRecordAt(selected);
      if (record) {
        stopGapClock();
        void commit(selected);
        return;
      }
      state.gapTicking = true;
      try { setTarget(selected, 0, true); } finally { state.gapTicking = false; }
      showGapControl();
      state.gapTimer = setTimeout(tick, 250);
    };
    state.gapTimer = setTimeout(tick, 250);
  }

  window.toggleWorkspaceGap = function () {
    if (!state.active || state.target === null || state.targetRecord) return false;
    if (state.gapPlaying) {
      if (typeof workspaceUserPaused !== 'undefined') workspaceUserPaused = true;
      stopGapClock();
      showGapControl();
    } else {
      if (typeof workspaceUserPaused !== 'undefined') workspaceUserPaused = false;
      startGapClock();
    }
    return true;
  };

  function canRetryRecording(error, record, converted) {
    if (converted || !/\.mp4(?:[?#]|$)/i.test(record?.url || '')) return false;
    // Only retry media format/decode failures. Network and autoplay errors
    // should not launch a costly server-side conversion.
    return error?.code === 3 || error?.code === 4 || error?.name === 'NotSupportedError';
  }

  async function commit(value, converted = false) {
    const selected = Math.max(0, Math.min(timelineDayLength(workspaceTimelineDate) - 0.001, Number(value) || 0));
    const record = timelineRecordAt(selected);
    const video = mainVideo();
    state.requestToken += 1;
    const token = state.requestToken;
    if (!record) {
      // Keep the gap overlay visible after release so an earlier recording or
      // the live stream cannot be mistaken for footage at the selected time.
      if (video && !video.paused) video.pause();
      setTarget(selected, 0, true);
      if (typeof verticalTimelineUserScrolling !== 'undefined') verticalTimelineUserScrolling = false;
      if (video && typeof setWorkspaceMode === 'function') setWorkspaceMode('recording');
      startGapClock();
      return;
    }
    const offset = recordingOffset(record, selected);
    const url = converted ? `${record.url}${record.url.includes('?') ? '&' : '?'}format=mp4` : sourceFor(record);
    clearTimeout(state.preloadTimer);
    state.preloadPath = null;
    const sameSource = video && workspacePlaybackUrl === record.url && video.dataset.playback === 'recording' && !converted;
    const same = sameSource && video.readyState >= 1 && !video.error;
    // Keep the last decoded preview frame over the player while the main
    // element seeks. Replacing the source first and hiding the preview here
    // caused the visible black flash during every scrub commit.
    showPreview();
    if (same) {
      video.pause();
      try { video.currentTime = offset; } catch {}
      if (typeof setWorkspaceMode === 'function') setWorkspaceMode('recording');
      // Some browsers defer decoding a paused seek until playback starts.
      // Start it behind the preview instead of waiting on a paused element.
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
        if (token !== state.requestToken) return;
        hidePreview();
      }
      return;
    }
    state.committing = true;
    updateStatus('Loading selected recording…');
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
    } finally { state.committing = false; }
  }

  function startAndWaitForFrame(video) {
    const frame = waitForDisplayedFrame(video);
    let playback;
    try { playback = Promise.resolve(video.play()); }
    catch (error) { playback = Promise.reject(error); }
    // Reject promptly if the browser blocks playback; otherwise keep the
    // preview visible until the first frame has actually decoded.
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
      const ready = () => {
        if (video.seeking || video.readyState < 2) return;
        // A paused Safari video may not produce another frame callback after
        // seeked; current data is sufficient to resume and reveal the video.
        finish();
      };
      const failed = () => finish(video.error || new Error('frame decode failed'));
      const timer = setTimeout(() => finish(new Error('frame decode timed out')), 8000);
      video.addEventListener('seeked', ready);
      video.addEventListener('loadeddata', ready);
      video.addEventListener('canplay', ready);
      video.addEventListener('error', failed);
      if (!video.seeking && video.readyState >= 2) ready();
    });
  }

  function install() {
    const video = mainVideo();
    if (!video || document.documentElement.dataset.playbackEngineInstalled) return;
    document.documentElement.dataset.playbackEngineInstalled = 'true';
    ensurePreview();
    const openWorkspace = window.openLiveWorkspace;
    if (typeof openWorkspace === 'function') {
      window.openLiveWorkspace = async (...args) => {
        const result = await openWorkspace(...args);
        if (window.workspaceCamera === args[0]) warmRecentAtlases();
        return result;
      };
    }
    const originalScrub = window.scrubWorkspaceTimeline;
    window.previewWorkspaceTimeline = (value) => setTarget(value, 0);
    window.previewWorkspaceTimelineScrub = (value, velocity) => setTarget(value, velocity);
    window.scrubWorkspaceTimeline = (value) => commit(value);
    const returnToLive = window.returnWorkspaceToLive;
    window.returnWorkspaceToLive = async (...args) => {
      stopGapClock();
      state.atlasPrefetchQueue.clear();
      state.requestToken += 1;
      clearTimeout(state.commitScheduleTimer);
      clearTimeout(state.commitTimer);
      clearTimeout(state.preloadTimer);
      clearTimeout(state.atlasWarmTimer);
      clearTimeout(state.proxyOpenTimer);
      state.proxyOpenPath = null;
      state.atlasWarmPath = null;
      state.preloadPath = null;
      state.active = false;
      state.pointerDown = false;
      state.lastValue = null;
      state.target = null;
      state.targetAbsolute = null;
      state.targetRecord = null;
      state.motionDirection = 0;
      state.motionEpoch += 1;
      window.workspaceScrubActive = false;
      const result = await returnToLive(...args);
      const video = mainVideo();
      if (video?.readyState >= 2) hidePreview();
      else video?.addEventListener('loadeddata', hidePreview, { once: true });
      return result;
    };
    window.verticalTimelineSeek = (value) => {
      const now = performance.now();
      const previous = state.lastValue;
      const elapsed = state.lastTime ? Math.max(16, now - state.lastTime) : 0;
      const velocity = previous === null || !elapsed ? 0 : (Number(value) - previous) / (elapsed / 1000);
      setTarget(value, velocity);
      state.lastTime = now;
      clearTimeout(state.commitTimer);
      // A drag commits on release. Wheel scrolling commits after its momentum
      // settles; neither path replaces the main video during pointer movement.
    };
    document.addEventListener('scroll', event => {
      if (!event.target?.closest?.('.vertical-timeline-scroll')) return;
      if (typeof verticalTimelineProgrammatic !== 'undefined' && verticalTimelineProgrammatic) return;
      clearTimeout(state.commitTimer);
      if (state.pointerDown) return;
      // The document capture listener runs before the timeline's own scroll
      // handler. Schedule after propagation, otherwise setTarget cancels the
      // timer that was just created for this very scroll event.
      clearTimeout(state.commitScheduleTimer);
      state.commitScheduleTimer = setTimeout(() => {
        if (state.pointerDown) return;
        clearTimeout(state.commitTimer);
        state.commitTimer = setTimeout(() => {
          if (state.active && state.lastValue !== null) void commit(state.lastValue);
        }, 350);
      }, 0);
    }, true);
    document.addEventListener('pointerdown', event => {
      if (event.target?.closest?.('.vertical-timeline-scroll')) {
        state.pointerDown = true;
        clearTimeout(state.commitScheduleTimer);
        clearTimeout(state.commitTimer);
      }
    }, true);
    document.addEventListener('pointerup', event => {
      if (!state.pointerDown) return;
      state.pointerDown = false;
      clearTimeout(state.commitTimer);
      if (state.lastValue !== null) void commit(state.lastValue);
    }, true);
    document.addEventListener('pointercancel', () => { state.pointerDown = false; }, true);
    video.addEventListener('timeupdate', () => {
      if (video.dataset.playback === 'recording' && !state.active && typeof workspacePlaybackStartSeconds !== 'undefined') {
        updateTimelinePlayhead(workspacePlaybackStartSeconds + video.currentTime);
      }
    });
    video.addEventListener('ended', () => {
      if (state.active || userPaused() || video.dataset.playback !== 'recording' ||
          typeof workspaceTimelineRecords === 'undefined') return;
      const current = workspaceTimelineRecords.find(record => record.url === workspacePlaybackUrl);
      if (!current || !Number.isFinite(video.duration)) return;
      const next = nextRecording(timelineSeconds(current));
      const end = timelineSeconds(current) + video.duration;
      if (next && timelineSeconds(next) <= end + 0.1) void commit(timelineSeconds(next));
      else {
        const after = end + 0.1;
        if (after < timelineDayLength(workspaceTimelineDate)) void commit(after);
        else { setTarget(timelineDayLength(workspaceTimelineDate) - 0.001, 0, true); startGapClock(); }
      }
    });
    window.addEventListener('resize', fitCanvas, { passive: true });
    new MutationObserver(() => {
      disableNativeControls(mainVideo());
      if (state.active) fitCanvas();
    }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['controls', 'src'] });
    log('installed', { originalScrub: Boolean(originalScrub) });
  }

  const style = document.createElement('style');
  style.textContent = `
    .workspace-scrub-frame { position:absolute; inset:0; z-index:5; width:100%; height:100%; object-fit:contain; display:none; pointer-events:none; background:#030507; }
    .workspace-scrub-frame.visible { display:block; }
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
