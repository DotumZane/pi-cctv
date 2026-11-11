/*
 * Persistent playback engine for the camera workspace.
 *
 * The dashboard stores recordings as individual MP4/MKV segments, not as one
 * HLS/DASH VOD presentation.  A manifest player would therefore not solve
 * seeking by itself.  Instead we keep the live/normal player stable and use a
 * second decoder plus a canvas for continuous, latest-target-wins scrubbing.
 */
(function () {
  'use strict';

  const debug = new URLSearchParams(location.search).has('debugPlayback');
  const state = {
    preview: null,
    canvas: null,
    ctx: null,
    frame: 0,
    target: null,
    targetRecord: null,
    targetOffset: 0,
    sourceUrl: '',
    loadToken: 0,
    requestToken: 0,
    lastInputAt: 0,
    lastValue: null,
    lastTime: 0,
    committing: false,
    commitTimer: 0,
    active: false,
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
    if (!state.preview) {
      state.preview = document.createElement('video');
      state.preview.className = 'workspace-scrub-decoder';
      state.preview.muted = true;
      state.preview.preload = 'auto';
      state.preview.playsInline = true;
      disableNativeControls(state.preview);
      host.appendChild(state.preview);
      instrument(state.preview, 'scrub');
      state.preview.addEventListener('loadedmetadata', () => seekPreview());
      state.preview.addEventListener('canplay', () => seekPreview());
      state.preview.addEventListener('seeked', drawPreviewFrame);
      state.preview.addEventListener('error', () => log('scrub:error', state.preview.error));
    }
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
    }
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
    return true;
  }

  function drawMainFrame() {
    const video = mainVideo();
    if (state.active && video?.readyState >= 2) drawImage(video);
  }

  function drawPreviewFrame() {
    const preview = state.preview;
    if (!state.active || !preview || preview.readyState < 2 || !state.targetRecord) return;
    if (state.requestToken !== state.loadToken && state.sourceUrl !== sourceFor(state.targetRecord)) return;
    if (drawImage(preview)) {
      log('scrub:frame', { target: state.target, offset: preview.currentTime });
      if (typeof updateTimelinePlayhead === 'function') updateTimelinePlayhead(state.target);
    }
  }

  function seekPreview() {
    const preview = state.preview;
    if (!preview || !state.active || state.target === null) return;
    const target = Math.max(0, Number(state.targetOffset) || 0);
    if (preview.readyState < 1) return;
    try {
      if (Math.abs((preview.currentTime || 0) - target) > 0.015) preview.currentTime = target;
      else drawPreviewFrame();
    } catch (error) { log('scrub:seek-failed', error); }
  }

  function tick() {
    state.frame = 0;
    if (!state.active || state.target === null) return;
    const now = performance.now();
    state.lastTime = now;
    seekPreview();
    if (now - state.lastInputAt < 120) state.frame = requestAnimationFrame(tick);
  }

  function showPreview() {
    if (!ensurePreview()) return;
    state.active = true;
    state.canvas.classList.add('visible');
    drawMainFrame();
  }

  function hidePreview() {
    state.active = false;
    if (state.canvas) state.canvas.classList.remove('visible');
    if (state.frame) cancelAnimationFrame(state.frame);
    state.frame = 0;
  }

  function setTarget(value, velocity) {
    const selected = Math.max(0, Math.min(timelineDayLength(workspaceTimelineDate) - 1, Number(value) || 0));
    const record = timelineRecordAt(selected);
    state.lastInputAt = performance.now();
    state.lastValue = selected;
    state.target = selected;
    state.targetRecord = record;
    state.targetOffset = record ? Math.max(0, selected - timelineSeconds(record)) : 0;
    showPreview();
    if (typeof workspaceTimelineLastValue !== 'undefined') workspaceTimelineLastValue = selected;
    if (typeof verticalTimelineSetPosition === 'function') verticalTimelineSetPosition(selected, false);
    if (typeof updateTimelinePlayhead === 'function') updateTimelinePlayhead(selected);
    if (!record) {
      log('scrub:gap', { selected });
      if (state.frame) cancelAnimationFrame(state.frame);
      state.frame = 0;
      return;
    }
    const url = sourceFor(record);
    if (url !== state.sourceUrl) {
      state.sourceUrl = url;
      state.loadToken += 1;
      state.preview.pause();
      state.preview.src = url;
      state.preview.load();
      log('scrub:source', { url, selected, velocity });
    } else {
      seekPreview();
    }
    if (!state.frame) state.frame = requestAnimationFrame(tick);
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
      await new Promise(resolve => {
        let settled = false;
        const finish = () => { if (settled) return; settled = true; video.removeEventListener('seeked', finish); resolve(); };
        video.addEventListener('seeked', finish, { once: true });
        setTimeout(finish, 1200);
      });
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
      await new Promise(resolve => {
        let settled = false;
        const finish = () => { if (settled) return; settled = true; video.removeEventListener('seeked', finish); resolve(); };
        video.addEventListener('seeked', finish, { once: true });
        setTimeout(finish, 1200);
      });
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

  function install() {
    const video = mainVideo();
    if (!video || document.documentElement.dataset.playbackEngineInstalled) return;
    document.documentElement.dataset.playbackEngineInstalled = 'true';
    ensurePreview();
    const originalScrub = window.scrubWorkspaceTimeline;
    window.previewWorkspaceTimeline = (value) => setTarget(value, 0);
    window.previewWorkspaceTimelineScrub = (value, velocity) => setTarget(value, velocity);
    window.scrubWorkspaceTimeline = (value) => commit(value);
    window.verticalTimelineSeek = (value) => {
      const now = performance.now();
      const previous = state.lastValue;
      const elapsed = state.lastTime ? Math.max(16, now - state.lastTime) : 0;
      const velocity = previous === null || !elapsed ? 0 : (Number(value) - previous) / (elapsed / 1000);
      setTarget(value, velocity);
      clearTimeout(state.commitTimer);
      state.commitTimer = setTimeout(() => commit(state.lastValue), 130);
    };
    video.addEventListener('timeupdate', () => {
      if (video.dataset.playback === 'recording' && !state.active && typeof workspacePlaybackStartSeconds !== 'undefined') {
        updateTimelinePlayhead(workspacePlaybackStartSeconds + video.currentTime);
      }
    });
    window.addEventListener('resize', fitCanvas, { passive: true });
    new MutationObserver(() => {
      disableNativeControls(mainVideo());
      disableNativeControls(state.preview);
      if (state.active) fitCanvas();
    }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['controls', 'src'] });
    log('installed', { originalScrub: Boolean(originalScrub) });
  }

  const style = document.createElement('style');
  style.textContent = `
    .workspace-scrub-decoder { position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; }
    .workspace-scrub-frame { position:absolute; inset:0; z-index:5; width:100%; height:100%; object-fit:contain; display:none; pointer-events:none; background:#030507; }
    .workspace-scrub-frame.visible { display:block; }
    #workspace-live, #workspace-live::-webkit-media-controls, #workspace-live::-webkit-media-controls-enclosure { -webkit-appearance:none; }
    #workspace-live::-webkit-media-controls { display:none !important; }
  `;
  document.head.appendChild(style);
  const observer = new MutationObserver(install);
  observer.observe(document.body, { childList: true, subtree: true });
  install();
})();
