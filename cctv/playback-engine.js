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
    frame: 0,
    target: null,
    targetRecord: null,
    targetOffset: 0,
    inFlight: false,
    pending: null,
    lastRequestedAt: 0,
    lastFrameTime: null,
    lastBitmap: null,
    motionDirection: 0,
    motionEpoch: 0,
    camera: null,
    frameCache: new Map(),
    requestToken: 0,
    lastInputAt: 0,
    lastValue: null,
    lastTime: 0,
    committing: false,
    commitTimer: 0,
    active: false,
    hasFrame: false,
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
    atlasPath: null,
    atlasManifest: null,
    atlasBase: null,
    atlasChecking: null,
    atlasWaiting: false,
    atlasRetry: 0,
    atlasFailed: false,
    atlasCache: new Map(),
    atlasLoads: new Map(),
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
    return true;
  }

  function fitCanvas() {
    const video = mainVideo(), canvas = state.canvas;
    if (!video || !canvas) return;
    const width = Math.max(1, Math.round(video.clientWidth * devicePixelRatio));
    const height = Math.max(1, Math.round(video.clientHeight * devicePixelRatio));
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
    if (state.active) state.canvas.classList.add('visible');
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
    state.lastBitmap = bitmap;
    state.lastFrameTime = request.selected;
    state.canvas.classList.add('visible');
    log('scrub:frame', { requested: request.selected, displayed: state.lastFrameTime,
      latencyMs: Math.round(performance.now() - request.startedAt) });
  }

  function frameKey(request) {
    return `${request.record.path}:${Math.round(request.offset * 4)}`;
  }

  function atlasSheetName(index) { return `${String(index).padStart(3, '0')}.jpg`; }

  function loadAtlasSheet(index) {
    const manifest = state.atlasManifest, path = state.atlasPath, base = state.atlasBase;
    if (!manifest || index < 0 || index >= manifest.sheets || state.atlasCache.has(index) || state.atlasLoads.has(index)) return;
    const job = api(base + atlasSheetName(index), { credentials: 'same-origin' })
      .then(response => { if (!response.ok) throw new Error(`atlas ${response.status}`); return response.blob(); })
      .then(blob => createImageBitmap(blob))
      .then(bitmap => {
        if (state.atlasPath !== path || state.atlasBase !== base) { bitmap.close?.(); return; }
        state.atlasCache.set(index, bitmap);
        if (state.atlasCache.size > 6) {
          const oldest = state.atlasCache.keys().next().value;
          state.atlasCache.get(oldest)?.close?.();
          state.atlasCache.delete(oldest);
        }
        scheduleAtlasFrame();
      }).catch(error => log('scrub:atlas-sheet-error', error.message))
      .finally(() => { if (state.atlasPath === path) state.atlasLoads.delete(index); });
    state.atlasLoads.set(index, job);
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
      const bitmap = state.atlasCache.get(sheet);
      if (!bitmap) { loadAtlasSheet(sheet); return; }
      state.atlasCache.delete(sheet);
      state.atlasCache.set(sheet, bitmap);
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
      state.canvas.classList.add('visible');
      state.lastFrameTime = state.target;
      loadAtlasSheet(sheet + (state.motionDirection >= 0 ? 1 : -1));
    });
  }

  function ensureAtlas(record) {
    if (!record || !ensurePreview()) return false;
    if (state.atlasPath !== record.path) {
      state.atlasPath = record.path;
      state.atlasManifest = null;
      state.atlasBase = null;
      state.atlasChecking = null;
      state.atlasWaiting = false;
      state.atlasFailed = false;
      clearTimeout(state.atlasRetry);
      if (state.atlasFrame) cancelAnimationFrame(state.atlasFrame);
      state.atlasFrame = 0;
      for (const bitmap of state.atlasCache.values()) bitmap.close?.();
      state.atlasCache.clear();
      state.atlasLoads.clear();
    }
    if (state.atlasManifest) return true;
    if (state.atlasFailed || state.atlasWaiting || state.atlasChecking) return false;
    const path = record.path;
    state.atlasChecking = api(`/api/recordings/scrub-frames?path=${encodeURIComponent(path)}`)
      .then(response => response.ok ? response.json() : null)
      .then(result => {
        if (state.atlasPath !== path) return;
        if (result?.ready && result.manifest?.frames && result.urlBase) {
          state.atlasManifest = result.manifest;
          state.atlasBase = result.urlBase;
          state.pending = null;
          scheduleAtlasFrame();
        } else if (result?.failed) state.atlasFailed = true;
        else {
          state.atlasWaiting = true;
          state.atlasRetry = setTimeout(() => {
            state.atlasWaiting = false;
            if (state.atlasPath === path && state.active) ensureAtlas(record);
          }, 1200);
        }
      }).catch(error => log('scrub:atlas-error', error.message))
      .finally(() => { if (state.atlasPath === path) state.atlasChecking = null; });
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

  function ensureProxy(record) {
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
    // Keep the still-frame fallback until the proxy for this segment is ready.
    const path = record.path;
    state.proxyChecking = api(`/api/recordings/trickplay?path=${encodeURIComponent(path)}`)
      .then(response => response.ok ? response.json() : null)
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
              state.pending = null;
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
            proxy.addEventListener('error', () => { state.proxyReady = false; state.proxyLoading = false; state.proxySeeking = false; });
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

  async function fetchFrame(request) {
    state.inFlight = true;
    request.startedAt = performance.now();
    try {
      const key = frameKey(request);
      let bitmap = state.frameCache.get(key);
      if (!bitmap) {
        const url = `/api/recordings/snapshot?preview=1&path=${encodeURIComponent(request.record.path)}&at=${(Math.round(request.offset * 4) / 4).toFixed(2)}`;
        const response = await api(url, { credentials: 'same-origin' });
        if (!response.ok) throw new Error(`frame request ${response.status}`);
        bitmap = await createImageBitmap(await response.blob());
        state.frameCache.set(key, bitmap);
        if (state.frameCache.size > 80) {
          const oldest = state.frameCache.keys().next().value;
          const evicted = state.frameCache.get(oldest);
          if (evicted !== state.lastBitmap) evicted?.close?.();
          state.frameCache.delete(oldest);
        }
      } else {
        state.frameCache.delete(key);
        state.frameCache.set(key, bitmap);
      }
      // A frame can trail the cursor during a fast move, but cannot replace a
      // newer frame or a frame from the opposite side after direction changes.
      const alreadyPassed = state.lastFrameTime !== null && request.direction &&
        Math.sign(request.selected - state.lastFrameTime) === -request.direction;
      if (state.active && !state.proxyReady && !state.atlasManifest && request.epoch === state.motionEpoch && !alreadyPassed && state.targetRecord &&
          state.targetRecord.path === request.record.path) drawExtractedFrame(bitmap, request);
    } catch (error) { log('scrub:frame-error', error.message); }
    finally {
      state.inFlight = false;
      if (state.pending && state.active) scheduleFrame();
    }
  }

  function scheduleFrame() {
    if (state.frame || state.inFlight || !state.pending) return;
    state.frame = requestAnimationFrame(() => {
      state.frame = 0;
      if (!state.active || state.inFlight || !state.pending) return;
      const request = state.pending;
      const interval = Math.max(0, 90 - (performance.now() - state.lastRequestedAt));
      if (interval) {
        state.frame = requestAnimationFrame(() => { state.frame = 0; scheduleFrame(); });
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
    if (!state.hasFrame) drawMainFrame();
    if (state.hasFrame) state.canvas.classList.add('visible');
  }

  function hidePreview() {
    state.active = false;
    state.pending = null;
    state.lastFrameTime = null;
    state.motionDirection = 0;
    state.proxyDesired = null;
    clearTimeout(state.proxyRetry);
    state.proxyWaiting = false;
    if (state.proxyFrame) cancelAnimationFrame(state.proxyFrame);
    state.proxyFrame = 0;
    if (state.atlasFrame) cancelAnimationFrame(state.atlasFrame);
    state.atlasFrame = 0;
    clearTimeout(state.atlasRetry);
    state.atlasWaiting = false;
    if (state.canvas) state.canvas.classList.remove('visible');
    if (state.frame) cancelAnimationFrame(state.frame);
    state.frame = 0;
  }

  function setTarget(value, velocity) {
    const selected = Math.max(0, Math.min(timelineDayLength(workspaceTimelineDate) - 1, Number(value) || 0));
    const record = timelineRecordAt(selected);
    if (state.camera !== window.workspaceCamera) {
      state.camera = window.workspaceCamera;
      state.motionEpoch += 1;
      state.lastFrameTime = null;
      state.lastBitmap = null;
      state.hasFrame = false;
      state.canvas?.classList.remove('visible');
    }
    const direction = state.target === null ? 0 : Math.sign(selected - state.target);
    if (direction && state.motionDirection && direction !== state.motionDirection) state.motionEpoch += 1;
    if (direction) state.motionDirection = direction;
    state.lastInputAt = performance.now();
    state.lastValue = selected;
    state.target = selected;
    state.targetRecord = record;
    state.targetOffset = record ? Math.max(0, selected - timelineSeconds(record)) : 0;
    const playing = mainVideo();
    if (playing?.dataset.playback === 'recording' && !playing.paused) playing.pause();
    showPreview();
    if (typeof workspaceTimelineLastValue !== 'undefined') workspaceTimelineLastValue = selected;
    if (typeof verticalTimelineSetPosition === 'function') verticalTimelineSetPosition(selected, false);
    if (typeof updateTimelinePlayhead === 'function') updateTimelinePlayhead(selected);
    if (!record) {
      log('scrub:gap', { selected });
      state.pending = null;
      if (state.frame) cancelAnimationFrame(state.frame);
      state.frame = 0;
      return;
    }
    if (ensureAtlas(record)) {
      state.pending = null;
      scheduleAtlasFrame();
      return;
    }
    // Atlas generation has priority. An extra proxy encode for the same
    // segment would contend with it and postpone the seek-free preview.
    const proxyReady = state.atlasFailed ? ensureProxy(record) : false;
    state.proxyDesired = state.targetOffset;
    if (proxyReady) {
      state.pending = null;
      seekProxy();
      return;
    }
    state.pending = { selected, record, offset: state.targetOffset,
      direction: state.motionDirection, epoch: state.motionEpoch };
    scheduleFrame();
  }

  function updateStatus(message) {
    const status = document.querySelector('#workspace-player');
    if (status && message) status.innerHTML = `<span class="notice">${esc(message)}</span>`;
  }

  async function commit(value) {
    const selected = Math.max(0, Math.min(timelineDayLength(workspaceTimelineDate) - 1, Number(value) || 0));
    const record = timelineRecordAt(selected);
    const video = mainVideo();
    state.requestToken += 1;
    const token = state.requestToken;
    if (!record) {
      hidePreview();
      updateStatus('No recording is available at the selected time.');
      return;
    }
    const offset = Math.max(0, selected - timelineSeconds(record));
    const url = sourceFor(record);
    const same = video && workspacePlaybackUrl === record.url && video.dataset.playback === 'recording' && video.readyState >= 1;
    // Keep the last decoded preview frame over the player while the main
    // element seeks. Replacing the source first and hiding the preview here
    // caused the visible black flash during every scrub commit.
    showPreview();
    if (same) {
      video.pause();
      try { video.currentTime = offset; } catch {}
      try { await waitForDisplayedFrame(video); }
      catch (error) { if (token === state.requestToken) updateStatus(`Playback unavailable: ${error.message}`); return; }
      if (token === state.requestToken) {
        drawMainFrame();
        if (typeof updateTimelinePlayhead === 'function') updateTimelinePlayhead(selected);
        if (typeof setWorkspaceMode === 'function') setWorkspaceMode('recording');
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
    video.src = url;
    video.load();
    workspacePlaybackUrl = record.url;
    workspacePlaybackStartSeconds = timelineSeconds(record);
    try {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('recording load timed out')), 15000);
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
      await waitForDisplayedFrame(video);
      if (token !== state.requestToken) return;
      drawMainFrame();
      if (typeof updateTimelinePlayhead === 'function') updateTimelinePlayhead(selected);
      updateStatus('Playback paused · press Play to continue.');
      if (typeof setWorkspaceMode === 'function') setWorkspaceMode('recording');
      hidePreview();
      log('commit:ready', { selected, offset });
    } catch (error) {
      if (token === state.requestToken) updateStatus(`Playback unavailable: ${error.message}`);
      log('commit:error', error);
    } finally { state.committing = false; }
  }

  function waitForDisplayedFrame(video) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = error => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        video.removeEventListener('seeked', ready);
        video.removeEventListener('error', failed);
        error ? reject(error) : resolve();
      };
      const ready = () => {
        if (video.readyState < 2) return;
        if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(() => finish());
        else finish();
      };
      const failed = () => finish(video.error || new Error('frame decode failed'));
      const timer = setTimeout(() => finish(new Error('frame decode timed out')), 8000);
      video.addEventListener('seeked', ready);
      video.addEventListener('error', failed);
      if (!video.seeking && video.readyState >= 2) ready();
    });
  }

  function install() {
    const video = mainVideo();
    if (!video || document.documentElement.dataset.playbackEngineInstalled) return;
    document.documentElement.dataset.playbackEngineInstalled = 'true';
    ensurePreview();
    const originalScrub = window.scrubWorkspaceTimeline;
    window.previewWorkspaceTimeline = (value) => setTarget(value, 0);
    window.previewWorkspaceTimelineScrub = (value, velocity) => setTarget(value, velocity);
    window.scrubWorkspaceTimeline = (value) => commit(value);
    const returnToLive = window.returnWorkspaceToLive;
    window.returnWorkspaceToLive = async (...args) => {
      state.requestToken += 1;
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
      state.commitTimer = setTimeout(() => {
        if (state.active && state.lastValue !== null) void commit(state.lastValue);
      }, 550);
    }, true);
    document.addEventListener('pointerdown', event => {
      if (event.target?.closest?.('.vertical-timeline-scroll')) clearTimeout(state.commitTimer);
    }, true);
    document.addEventListener('pointerup', event => {
      if (!event.target?.closest?.('.vertical-timeline-scroll')) return;
      clearTimeout(state.commitTimer);
      if (state.lastValue !== null) void commit(state.lastValue);
    }, true);
    video.addEventListener('timeupdate', () => {
      if (video.dataset.playback === 'recording' && !state.active && typeof workspacePlaybackStartSeconds !== 'undefined') {
        updateTimelinePlayhead(workspacePlaybackStartSeconds + video.currentTime);
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
    .workspace-scrub-proxy { position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; }
    #workspace-live, #workspace-live::-webkit-media-controls, #workspace-live::-webkit-media-controls-enclosure { -webkit-appearance:none; }
    #workspace-live::-webkit-media-controls { display:none !important; }
  `;
  document.head.appendChild(style);
  const observer = new MutationObserver(install);
  observer.observe(document.body, { childList: true, subtree: true });
  install();
})();
