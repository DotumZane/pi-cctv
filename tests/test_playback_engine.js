// Behaviour checks for the recorded-video scrubbing engine. The engine runs
// in a VM against small DOM, media and network doubles; the preview-track
// API is simulated with one 256x144 frame per second and 15 s coarse frames.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8');
const DATE = '2026-11-12';
const MIDNIGHT = new Date(`${DATE}T00:00:00`).getTime() / 1000;
const pad = value => String(value).padStart(2, '0');
const stem = seconds => `${pad(Math.floor(seconds / 3600))}-${pad(Math.floor(seconds % 3600 / 60))}-${pad(seconds % 60)}`;
const tick = () => new Promise(resolve => setImmediate(resolve));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function makeRecords(first, count, { pending = [], mkv = false } = {}) {
  return Array.from({ length: count }, (_, i) => {
    const start = first + i * 30, name = `${stem(start)}.${mkv ? 'mkv' : 'mp4'}`;
    return { path: `cam-1/${DATE}/${name}`, url: `/recordings/cam-1/${DATE}/${name}`, startSeconds: start, duration: 30,
      start: new Date((MIDNIGHT + start) * 1000).toISOString(), pending: pending.includes(i) };
  });
}

function manifestFor(records, hour) {
  const segments = [], coarse = [], pending = [];
  records.forEach(record => {
    const start = MIDNIGHT + record.startSeconds;
    if (start + record.duration <= hour || start >= hour + 3600) return;
    if (record.pending) { pending.push({ path: record.path, start, state: 'pending' }); return; }
    segments.push({ path: record.path, start, duration: 30, base: `/recordings/.preview-track/${record.path.replace(/\.\w+$/, '')}/`,
      version: 'v1', columns: 6, rows: 5, width: 256, height: 144, sheets: 1,
      times: Array.from({ length: 30 }, (_, i) => i), keyframes: true });
    for (let offset = 0; offset < 30; offset++) {
      const t = start + offset;
      if (t % 15 === 0 && t >= hour && t < hour + 3600) coarse.push(t);
    }
  });
  return { camera_id: 'cam-1', hour, interval: 1, coarse_interval: 15, segments, pending,
    coarse: coarse.length ? { url: `/api/recordings/preview-coarse?camera_id=cam-1&hour=${hour}&v=c1`, version: 'c1',
      width: 160, height: 90, columns: 16, times: coarse } : null };
}

function harness({ records, zoom = 14400, realClock = false, holdSheets = false } = {}) {
  let fakeClock = 1000;
  const now = () => (realClock ? performance.now() : fakeClock);
  const animationFrames = [], draws = [], requests = [], held = [], listeners = new Map();
  const manifests = new Map();
  const visible = new Set();
  const message = { textContent: '', setAttribute() {}, classList: { toggle() {} } };
  const context2d = {
    fillRect() {}, set fillStyle(_) {},
    drawImage(source, sx, sy, sw, sh) { draws.push({ source, sx, sy, sw, sh, at: now(), cursor: env.cursor }); },
  };
  const canvas = { width: 0, height: 0, setAttribute() {}, getContext() { return context2d; },
    classList: { add(name) { visible.add(name); }, remove(name) { visible.delete(name); } } };
  function media(name) {
    const own = new Map();
    const element = {
      name, dataset: {}, readyState: 0, paused: true, seeking: false, duration: 30, videoWidth: 1920, videoHeight: 1080,
      clientWidth: 1280, clientHeight: 720, controls: true, src: '', loads: 0, plays: 0, seeks: 0, _time: 0,
      get currentTime() { return this._time; },
      set currentTime(value) {
        this._time = value; this.seeks++;
        setImmediate(() => { this.readyState = Math.max(this.readyState, 2); element.dispatch('seeked'); });
      },
      addEventListener(type, fn) { own.set(type, [...(own.get(type) || []), fn]); },
      removeEventListener(type, fn) { own.set(type, (own.get(type) || []).filter(item => item !== fn)); },
      dispatch(type) { (own.get(type) || []).slice().forEach(fn => fn({ type })); },
      setAttribute() {}, removeAttribute(name) { if (name === 'controls') this.controls = false; },
      closest() { return host; }, pause() { this.paused = true; },
      load() { this.loads++; this.readyState = 0; setImmediate(() => { this.readyState = 1; element.dispatch('loadedmetadata'); }); },
      play() { this.plays++; this.paused = false; setImmediate(() => { this.readyState = 2; element.dispatch('canplay'); }); return Promise.resolve(); },
    };
    return element;
  }
  const host = { appendChild() {} };
  const video = media('main');
  video.dataset.playback = 'live';
  const proxies = [];
  const status = { innerHTML: '' };
  const document = {
    body: {}, head: { appendChild() {} }, documentElement: { dataset: {} },
    createElement(tag) {
      if (tag === 'canvas') return canvas;
      if (tag === 'video') { const proxy = media('proxy'); proxies.push(proxy); return proxy; }
      return tag === 'style' ? { textContent: '' } : message;
    },
    querySelector(selector) {
      return selector === '#workspace-live' ? video : selector === '#workspace-player' ? status : null;
    },
    addEventListener(type, fn) { listeners.set(type, [...(listeners.get(type) || []), fn]); },
  };
  const api = (url, options = {}) => {
    requests.push(url);
    if (url.includes('/api/recordings/preview-track?')) {
      const hour = Number(new URL(url, 'http://x').searchParams.get('hour'));
      manifests.set(hour, (manifests.get(hour) || 0) + 1);
      return Promise.resolve({ ok: true, json: async () => manifestFor(env.records, hour) });
    }
    const response = { ok: true, blob: async () => ({ url }) };
    if (!holdSheets) return Promise.resolve(response);
    return new Promise((resolve, reject) => {
      held.push({ url, resolve: () => resolve(response) });
      options.signal?.addEventListener('abort', () => { const error = new Error('aborted'); error.name = 'AbortError'; reject(error); });
    });
  };
  const env = {
    records, cursor: null, draws, requests, held, proxies, video, status, message, visible, manifests, committed: [],
    advance(ms) { fakeClock += ms; },
  };
  const window = { workspaceCamera: 'cam-1', returnWorkspaceToLive: async () => {}, addEventListener() {} };
  const globals = {
    document, window, URLSearchParams, URL, AbortController, location: { search: '' }, devicePixelRatio: 1,
    performance: { now }, console, setTimeout, clearTimeout, setImmediate,
    requestAnimationFrame(fn) { animationFrames.push(fn); return animationFrames.length; },
    cancelAnimationFrame() {}, MutationObserver: class { observe() {} },
    timelineDayLength: () => 86400, timelineSeconds: record => record.startSeconds,
    timelineRecordAt: value => env.records.find(r => value >= r.startSeconds && value < r.startSeconds + r.duration),
    workspaceTimelineDate: DATE, workspaceTimelineRecords: records, workspaceTimelineLastValue: 0,
    workspacePlaybackUrl: '', workspacePlaybackStartSeconds: 0, verticalTimelineHeight: zoom,
    formatTimelineTime: value => `${pad(Math.floor(value / 3600))}:${pad(Math.floor(value % 3600 / 60))}:${pad(Math.floor(value % 60))}`,
    verticalTimelineSetPosition() {}, updateTimelinePlayhead() {}, setWorkspaceMode(mode) { video.dataset.playback = mode; },
    stopLivePlayback() {}, esc: value => value, api,
    createImageBitmap: async blob => ({ id: blob.url, close() {} }),
  };
  vm.runInNewContext(SOURCE, globals);
  env.globals = globals;
  env.window = window;
  env.metrics = () => window.cctvScrubMetrics.snapshot();
  env.setZoom = height => { globals.verticalTimelineHeight = height; };
  env.frames = async (count = 4) => {
    for (let i = 0; i < count; i++) {
      animationFrames.splice(0).forEach(fn => fn(now()));
      await tick();
      await tick();
    }
  };
  env.move = async (seconds, { stepMs = 16, frames = 2 } = {}) => {
    env.cursor = seconds;
    window.previewWorkspaceTimelineScrub(seconds, 0);
    if (!realClock) fakeClock += stepMs;
    await env.frames(frames);
  };
  env.fire = (type, target = timelineTarget) => (listeners.get(type) || []).forEach(fn => fn({ type, target }));
  // Map a draw back to the footage time it shows.
  env.frameTime = draw => {
    const url = draw.source?.id;
    if (!url) return draw.source === video ? 'main' : draw.source?.name === 'proxy' ? 'video' : null;
    const cell = Math.round(draw.sy / draw.sh) * (url.includes('preview-coarse') ? 16 : 6) + Math.round(draw.sx / draw.sw);
    if (url.includes('preview-coarse')) {
      const hour = Number(new URL(url, 'http://x').searchParams.get('hour'));
      return manifestFor(env.records, hour).coarse.times[cell] - MIDNIGHT;
    }
    const match = url.match(/(\d\d)-(\d\d)-(\d\d)\/000\.jpg/);
    return Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]) + cell;
  };
  env.level = draw => (draw.source?.id?.includes('preview-coarse') ? 'coarse' : draw.source?.id ? 'fine' : 'other');
  env.lastSheetDraw = () => [...draws].reverse().find(draw => draw.source?.id);
  return env;
}
const timelineTarget = { closest: () => true };

async function forwardAndReverse() {
  for (const direction of [1, -1]) {
    const env = harness({ records: makeRecords(21600, 40) });
    const start = direction > 0 ? 21600 : 21600 + 40 * 30 - 1;
    await env.move(start, { frames: 6 });
    env.draws.length = 0;
    // 1h zoom: three pixels (18 s) every 16 ms is about 1100 s/s.
    let settled = 0;
    for (let i = 1; i <= 60; i++) {
      if (i === 10) settled = env.draws.length;
      await env.move(start + direction * i * 18);
    }
    const times = env.draws.map(env.frameTime).filter(Number.isFinite);
    assert.ok(times.length >= 20, `continuous ${direction > 0 ? 'forward' : 'reverse'} scrolling paints many frames (${times.length})`);
    for (let i = 1; i < times.length; i++) {
      assert.ok((times[i] - times[i - 1]) * direction >= 0, `frames never step against the scroll direction (${times[i - 1]} -> ${times[i]})`);
    }
    // The speed estimate needs a few events to reach full speed.
    assert.ok(env.draws.slice(settled).every(draw => env.level(draw) === 'coarse'), 'fast movement uses one density: the coarse track');
    // At rest the per-second track takes over and lands on the cursor.
    env.advance(400);
    await env.frames(10);
    const last = env.lastSheetDraw();
    assert.equal(env.level(last), 'fine', 'a resting cursor shows the per-second frame');
    assert.equal(env.frameTime(last), env.cursor, 'the resting frame is the requested second');
    const metrics = env.metrics();
    assert.ok(metrics.uniqueFrames >= 20);
    assert.ok(metrics.renderFps >= metrics.fps, 'canvas ticks and distinct frames are counted separately');
    assert.ok(metrics.medianLatencyMs !== null && metrics.medianLatencyMs < 100, 'cursor-to-preview latency is measured');
    assert.ok(metrics.cacheHitRate !== null);
  }
}

async function rapidDirectionChanges() {
  const env = harness({ records: makeRecords(21600, 20) });
  await env.move(21900, { frames: 6 });
  env.advance(300);
  await env.frames(6);
  let cursor = 21900;
  const pattern = [...Array(8).fill(2), ...Array(8).fill(-2), ...Array(8).fill(2), ...Array(8).fill(-2)];
  let previous = env.frameTime(env.lastSheetDraw());
  for (const step of pattern) {
    cursor += step;
    const before = env.draws.length;
    await env.move(cursor, { stepMs: 60 });
    for (const draw of env.draws.slice(before)) {
      const time = env.frameTime(draw);
      if (!Number.isFinite(time)) continue;
      assert.ok(Math.abs(time - draw.cursor) <= Math.abs(previous - draw.cursor) + 1e-6,
        `a new frame is never further from the cursor than the one it replaces (${previous} -> ${time} at ${draw.cursor})`);
      previous = time;
    }
  }
  env.advance(400);
  await env.frames(8);
  assert.equal(env.frameTime(env.lastSheetDraw()), cursor, 'after reversals the preview settles on the cursor');
}

async function wheelGestureCommitsOnce() {
  const env = harness({ records: makeRecords(21600, 20), realClock: true });
  env.fire('pointerdown', { closest: () => false });
  for (let i = 0; i < 10; i++) {
    env.fire('wheel');
    env.fire('scroll');
    await env.move(21700 + i * 6, { frames: 1 });
    env.fire('scrollend');
    await sleep(60);
  }
  assert.equal(env.video.loads, 0, 'scrollend after each wheel notch does not restart playback');
  await sleep(500);
  await env.frames(2);
  assert.equal(JSON.stringify(env.metrics().commitsPerGesture), '[1]', 'one wheel gesture ends in one playback commit');
  assert.equal(env.video.loads, 1);
  assert.match(env.video.src, /07-56-30|07-55-00|21754|\.mp4/, 'the committed recording is loaded');

  // Scripted or keyboard scrolls fire scrollend after every step; steps in
  // quick succession are still one gesture.
  for (let i = 0; i < 6; i++) {
    env.fire('scroll');
    await env.move(21800 + i, { frames: 1 });
    await sleep(5);
    env.fire('scrollend');
    await sleep(40);
  }
  assert.equal(JSON.stringify(env.metrics().commitsPerGesture), '[1,0]', 'scrollend between quick scroll steps does not commit');
  await sleep(200);
  assert.equal(JSON.stringify(env.metrics().commitsPerGesture), '[1,1]', 'a native scroll gesture commits once, soon after scrollend');

  // Clicking the timeline without moving it never restarts playback.
  await sleep(50);
  const loads = env.video.loads, seeks = env.video.seeks;
  env.fire('pointerdown');
  env.fire('pointerup');
  await sleep(20);
  assert.equal(env.video.loads, loads);
  assert.equal(env.video.seeks, seeks);
}

async function zoomSelectsDensity() {
  const env = harness({ records: makeRecords(21600, 20), zoom: 3600 });
  await env.move(21700, { frames: 6 });
  env.advance(400);
  await env.frames(6);
  assert.equal(env.level(env.lastSheetDraw()), 'coarse', 'the 4h zoom (24 s per pixel) uses 15 s frames');
  env.setZoom(14400);
  await env.move(21701, { stepMs: 400, frames: 6 });
  env.advance(400);
  await env.frames(6);
  assert.equal(env.level(env.lastSheetDraw()), 'fine', 'the 1h zoom uses per-second frames');
  assert.equal(env.frameTime(env.lastSheetDraw()), 21701, 'the selected timestamp is kept across zoom changes');
  env.setZoom(172800);
  await env.move(21701.5, { stepMs: 400, frames: 6 });
  assert.equal(env.proxies.length, 1, 'detailed zooms decode the recording itself');
  assert.match(env.proxies[0].src, /06-01-30\.mp4$/);
  await env.frames(6);
  const levels = env.metrics().levels;
  assert.ok(levels.video > 0 && levels.fine > 0 && levels.coarse > 0, JSON.stringify(levels));
  assert.equal(env.proxies[0].currentTime, 11.5, 'the decoder seeks to the sub-second cursor position');
}

async function sourceConsistency() {
  const env = harness({ records: makeRecords(21600, 20), zoom: 14400 });
  await env.move(21700, { stepMs: 400, frames: 6 });
  env.advance(400);
  await env.frames(6);
  assert.equal(env.level(env.lastSheetDraw()), 'fine');
  // Zoomed out, a decoded per-second sheet is near the cursor, but the
  // coarse track is the source for this zoom level.
  env.setZoom(600);
  env.draws.length = 0;
  await env.move(21705, { stepMs: 400, frames: 6 });
  env.advance(400);
  await env.frames(6);
  assert.ok(env.draws.length > 0);
  // Until the hour's coarse sheet arrives, an already decoded per-second
  // frame may stand in; once the zoom's own density is on screen, it stays.
  const first = env.draws.findIndex(draw => env.level(draw) === 'coarse');
  assert.ok(first >= 0, 'the 24h zoom shows the coarse track');
  assert.ok(env.draws.slice(first).every(draw => env.level(draw) === 'coarse'), 'the 24h zoom does not mix in per-second frames');
  for (const second of [21712, 21731, 21755]) await env.move(second, { stepMs: 400, frames: 4 });
  assert.ok(env.draws.slice(first).every(draw => env.level(draw) === 'coarse'), 'later moves stay on the coarse track');
}

async function segmentBoundaries() {
  const env = harness({ records: makeRecords(21600, 4) });
  await env.move(21625, { stepMs: 400, frames: 6 });
  const shown = [];
  for (let second = 21626; second <= 21636; second++) {
    await env.move(second, { stepMs: 250, frames: 4 });
    shown.push(env.frameTime(env.lastSheetDraw()));
  }
  assert.deepEqual(shown, [21626, 21627, 21628, 21629, 21630, 21631, 21632, 21633, 21634, 21635, 21636],
    'slow movement shows every second across the 06:00:30 segment boundary');
}

async function cacheMisses() {
  const env = harness({ records: makeRecords(21600, 160), holdSheets: true });
  await env.move(21700, { stepMs: 400, frames: 4 });
  env.held.splice(0).forEach(item => item.resolve());
  await env.frames(6);
  assert.equal(env.frameTime(env.lastSheetDraw()), 21700);
  const draws = env.draws.length;
  // Jump into the next hour while its sheets are still downloading.
  env.advance(5000);
  await env.move(25800, { stepMs: 400, frames: 4 });
  assert.equal(env.draws.length, draws, 'no frame is drawn before its sheet arrives');
  assert.equal(env.visible.has('visible'), true, 'the last frame stays visible during a cache miss');
  assert.match(env.message.textContent, /Loading preview · showing 06:01:40/, 'a stale frame is labelled with its own time');
  // Move again before the downloads finish: obsolete requests are cancelled.
  await env.move(21610, { stepMs: 400, frames: 4 });
  assert.ok(env.metrics().abortedRequests > 0, 'obsolete sheet requests are aborted');
  env.advance(400);
  for (let i = 0; i < 20; i++) { env.held.splice(0).forEach(item => item.resolve()); await env.frames(1); }
  assert.equal(env.frameTime(env.lastSheetDraw()), 21610, 'the frame for the latest cursor appears once loaded');
  assert.ok(env.metrics().cacheMisses > 0);
}

async function scrubToPlayback() {
  const env = harness({ records: makeRecords(21600, 4) });
  await env.move(21650, { stepMs: 400, frames: 6 });
  assert.equal(env.video.loads, 0, 'scrubbing never loads the main player');
  assert.equal(env.visible.has('visible'), true);
  await env.window.scrubWorkspaceTimeline(21650);
  assert.equal(env.video.src, '/recordings/cam-1/2026-11-12/06-00-30.mp4');
  assert.equal(env.video.currentTime, 20);
  assert.equal(env.video.paused, false);
  assert.equal(env.visible.has('visible'), false, 'the preview hides once the player shows its first frame');
  assert.equal(env.video.controls, false, 'browser-native controls stay disabled');
  // Committing the time that is already playing does not restart it.
  env.globals.workspacePlaybackUrl = env.video.src;
  const seeks = env.video.seeks;
  await env.window.scrubWorkspaceTimeline(21650);
  assert.equal(env.video.loads, 1);
  assert.equal(env.video.seeks, seeks, 'the same position is not re-seeked');
}

async function missingRecordingsAndPreviews() {
  const records = makeRecords(21600, 4, { pending: [3] });
  const env = harness({ records });
  await env.move(21650, { stepMs: 400, frames: 6 });
  const draws = env.draws.length;
  // A gap between recordings keeps the last frame without an alarm.
  await env.move(21900, { stepMs: 400, frames: 4 });
  assert.equal(env.draws.length, draws);
  assert.doesNotMatch(env.message.textContent, /No recording/);
  await env.window.scrubWorkspaceTimeline(21900);
  assert.match(env.message.textContent, /No recording .* last frame shown/, 'a committed gap is labelled');
  assert.equal(env.visible.has('visible'), true);
  // A closed segment without a preview yet: label it and decode the MP4.
  await env.move(21695, { stepMs: 400, frames: 8 });
  assert.equal(env.proxies.length, 1, 'a segment without preview frames falls back to the recording');
  assert.match(env.proxies[0].src, /06-01-30\.mp4$/);
  await env.frames(4);
  assert.equal(env.draws.at(-1).source.name, 'proxy', 'the decoded recording frame is shown');
  // Unavailable manifests (server error) leave the picture in place.
  const failing = harness({ records: makeRecords(21600, 4) });
  failing.globals.api = () => Promise.resolve({ ok: false, status: 503 });
  await failing.move(21650, { stepMs: 400, frames: 6 });
  assert.equal(failing.draws.filter(draw => draw.source?.id).length, 0);
  assert.match(failing.message.textContent, /Loading preview/);
}

Promise.resolve()
  .then(forwardAndReverse).then(rapidDirectionChanges).then(wheelGestureCommitsOnce).then(zoomSelectsDensity)
  .then(sourceConsistency).then(segmentBoundaries).then(cacheMisses).then(scrubToPlayback).then(missingRecordingsAndPreviews)
  .then(() => { console.log('Playback engine checks passed'); process.exit(0); })
  .catch(error => { console.error(error); process.exit(1); });
