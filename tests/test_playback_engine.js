// Regression check: decoded recording frames must become visible during a
// timeline move, before the normal player receives a committed seek.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  let drawn = 0;
  let fetched = 0;
  let committed = 0;
  let clock = 1000;
  const visible = new Set();
  const animationFrames = [];
  const context2d = { fillRect() {}, drawImage() { drawn++; }, set fillStyle(_) {} };
  const canvas = {
    width: 0, height: 0, classList: {
      add(name) { visible.add(name); }, remove(name) { visible.delete(name); },
    },
    setAttribute() {}, getContext() { return context2d; },
  };
  const host = { appendChild() {} };
  const video = {
    dataset: { playback: 'live' }, clientWidth: 640, clientHeight: 360,
    readyState: 0, paused: true, controls: false,
    closest() { return host; }, setAttribute() {}, removeAttribute() {},
    addEventListener() {}, pause() { committed++; },
  };
  const status = { innerHTML: '' };
  const document = {
    body: {}, head: { appendChild() {} }, documentElement: { dataset: {} },
    createElement(tag) { return tag === 'canvas' ? canvas : { textContent: '', setAttribute() {}, classList: { toggle() {} } }; },
    querySelector(selector) {
      return selector === '#workspace-live' ? video :
        selector === '#workspace-player' ? status : null;
    },
    addEventListener() {},
  };
  const window = { workspaceCamera: 'cam-1', returnWorkspaceToLive: async () => {}, addEventListener() {} };
  const record = { path: 'cam-1/2026-11-12/06-00-00.mp4', url: '/recordings/cam-1/2026-11-12/06-00-00.mp4' };
  const globals = {
    document, window, URLSearchParams, location: { search: '' }, devicePixelRatio: 1,
    performance: { now: () => clock },
    requestAnimationFrame(callback) { animationFrames.push(callback); return animationFrames.length; },
    cancelAnimationFrame() {}, MutationObserver: class { observe() {} },
    timelineDayLength: () => 86400, timelineRecordAt: value => value === 40 ? null : record,
    timelineSeconds: () => 0, workspaceTimelineDate: '2026-11-12',
    formatTimelineTime: value => `00:00:${String(value).padStart(2, '0')}`,
    workspaceTimelineLastValue: 0, verticalTimelineSetPosition() {},
    updateTimelinePlayhead() {}, esc: value => value,
    async api(url) {
      if (url.includes('/scrub-frames?')) return { ok: true, async json() { return { ready: false }; } };
      if (url.includes('/trickplay?')) return { ok: true, async json() { return { ready: false }; } };
      fetched++;
      return { ok: true, async blob() { return {}; } };
    },
    async createImageBitmap() { return { width: 640, height: 360 }; },
    setTimeout(fn, ms) { return ms === 220 ? 0 : setTimeout(fn, ms); }, clearTimeout, console,
  };
  const source = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8');
  vm.runInNewContext(source, globals);
  window.previewWorkspaceTimelineScrub(30, -60);
  assert.equal(committed, 0);
  assert.equal(visible.has('visible'), false);
  animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fetched, 1);
  assert.equal(drawn, 1);
  assert.equal(visible.has('visible'), true);
  assert.equal(committed, 0, 'normal playback must not seek during preview');
  clock += 200;
  window.previewWorkspaceTimelineScrub(20, -60);
  animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fetched, 2, 'reverse movement should request another frame');
  assert.equal(drawn, 2, 'reverse frame should appear before playback commits');
  assert.equal(committed, 0);
  window.previewWorkspaceTimelineScrub(40, 20);
  assert.match(status.innerHTML, /No recording/);
  assert.equal(visible.has('visible'), true, 'a gap covers the previous video frame');
  await window.scrubWorkspaceTimeline(40);
  assert.equal(visible.has('visible'), true, 'committing a gap keeps the old frame covered');
  assert.match(status.innerHTML, /No recording/);
  window.previewWorkspaceTimelineScrub(20, -20);
  assert.doesNotMatch(status.innerHTML, /No recording/, 'returning to footage clears the gap status');
}

async function proxyScrubbing() {
  const drawnTimes = [];
  const pendingFrames = [];
  const listeners = new Map();
  let snapshotRequests = 0;
  let playbackLoads = 0;
  let proxyRequests = 0;
  let position = 0;
  const proxy = {
    duration: 30, readyState: 2, videoWidth: 640, videoHeight: 360,
    muted: false, preload: '',
    set currentTime(value) {
      position = value;
      for (const callback of listeners.get('seeked') || []) callback();
    },
    get currentTime() { return position; },
    addEventListener(type, callback) { listeners.set(type, [...(listeners.get(type) || []), callback]); },
    setAttribute() {}, removeAttribute() {}, load() { playbackLoads++; },
  };
  const host = { appendChild() {} };
  const video = {
    dataset: { playback: 'live' }, clientWidth: 640, clientHeight: 360,
    readyState: 0, paused: true, controls: false,
    closest() { return host; }, setAttribute() {}, removeAttribute() {}, addEventListener() {},
    pause() { throw new Error('normal player paused while scrubbing live'); },
  };
  const canvas = {
    width: 0, height: 0, classList: { add() {}, remove() {} }, setAttribute() {},
    getContext() { return { fillRect() {}, drawImage(source) { if (source === proxy) drawnTimes.push(position); }, set fillStyle(_) {} }; },
  };
  const document = {
    body: {}, head: { appendChild() {} }, documentElement: { dataset: {} },
    createElement(tag) { return tag === 'video' ? proxy : tag === 'canvas' ? canvas : { textContent: '', setAttribute() {}, classList: { toggle() {} } }; },
    querySelector(selector) { return selector === '#workspace-live' ? video : null; }, addEventListener() {},
  };
  const record = { path: 'cam-1/2026-11-12/06-00-00.mp4', url: '/recordings/cam-1/2026-11-12/06-00-00.mp4' };
  const globals = {
    document, window: { workspaceCamera: 'cam-1', returnWorkspaceToLive: async () => {}, addEventListener() {} },
    URLSearchParams, location: { search: '' }, devicePixelRatio: 1,
    performance: { now: () => 1000 },
    requestAnimationFrame(callback) { pendingFrames.push(callback); return pendingFrames.length; },
    cancelAnimationFrame() {}, MutationObserver: class { observe() {} },
    timelineDayLength: () => 86400, timelineRecordAt: () => record,
    timelineSeconds: () => 0, workspaceTimelineDate: '2026-11-12',
    workspaceTimelineLastValue: 0, verticalTimelineSetPosition() {}, updateTimelinePlayhead() {},
    async api(url) {
      if (url.includes('/scrub-frames?')) return { ok: true, async json() { return { ready: false }; } };
      if (url.includes('/trickplay?')) { proxyRequests++; return { ok: true, async json() { return { ready: false }; } }; }
      snapshotRequests++;
      return { ok: true, async blob() { return {}; } };
    },
    async createImageBitmap() { return { width: 640, height: 360 }; },
    setTimeout(fn, ms) { return ms === 220 ? 0 : setTimeout(fn, ms); }, clearTimeout, console,
  };
  const source = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8');
  vm.runInNewContext(source, globals);
  globals.window.previewWorkspaceTimelineScrub(8, -20);
  await new Promise(resolve => setImmediate(resolve));
  // The original recording provides frames while the atlas is still building.
  globals.window.previewWorkspaceTimelineScrub(8, -20);
  await new Promise(resolve => setImmediate(resolve));
  for (const callback of listeners.get('loadedmetadata') || []) callback();
  while (pendingFrames.length) pendingFrames.shift()();
  globals.window.previewWorkspaceTimelineScrub(12, 20);
  while (pendingFrames.length) pendingFrames.shift()();
  globals.window.previewWorkspaceTimelineScrub(4, -20);
  while (pendingFrames.length) pendingFrames.shift()();
  assert.deepEqual(drawnTimes.filter((value, index, array) => index === 0 || value !== array[index - 1]), [8, 12, 4]);
  assert.equal(playbackLoads, 1, 'proxy stays loaded across direction changes');
  assert.equal(proxy.src, record.url, 'the original MP4 is used without waiting for conversion');
  assert.equal(proxyRequests, 0, 'scrubbing does not start a competing proxy encode');
  assert.ok(snapshotRequests <= 1, 'fallback requests remain bounded during rapid movement');
}

async function atlasScrubbing() {
  const draws = [];
  const animationFrames = [];
  let mainSeeks = 0;
  let proxyRequests = 0;
  let sheetRequests = 0;
  const requestedSheets = [];
  const canvas = { width: 0, height: 0, classList: { add() {}, remove() {} }, setAttribute() {},
    getContext() { return { fillRect() {}, drawImage(...args) { draws.push(args); }, set fillStyle(_) {} }; } };
  const host = { appendChild() {} };
  const video = { dataset: { playback: 'live' }, clientWidth: 640, clientHeight: 360,
    readyState: 0, paused: true, controls: false, closest() { return host; },
    setAttribute() {}, removeAttribute() {}, addEventListener() {},
    set currentTime(_) { mainSeeks++; }, pause() {} };
  const document = { body: {}, head: { appendChild() {} }, documentElement: { dataset: {} },
    createElement(tag) { return tag === 'canvas' ? canvas : { textContent: '', setAttribute() {}, classList: { toggle() {} } }; },
    querySelector(selector) { return selector === '#workspace-live' ? video : null; }, addEventListener() {} };
  const record = { path: 'cam-1/2026-11-12/06-00-00.mp4', url: '/recordings/cam-1/2026-11-12/06-00-00.mp4' };
  const nextRecord = { path: 'cam-1/2026-11-12/06-00-30.mp4', url: '/recordings/cam-1/2026-11-12/06-00-30.mp4' };
  const globals = { document, window: { workspaceCamera: 'cam-1', returnWorkspaceToLive: async () => {}, addEventListener() {} },
    URLSearchParams, location: { search: '' }, devicePixelRatio: 1, performance: { now: () => 1000 },
    requestAnimationFrame(callback) { animationFrames.push(callback); return animationFrames.length; },
    cancelAnimationFrame() {}, MutationObserver: class { observe() {} },
    timelineDayLength: () => 86400, timelineRecordAt: value => value >= 30 ? nextRecord : record,
    timelineSeconds: item => item === nextRecord ? 30 : 0, workspaceTimelineRecords: [record, nextRecord],
    workspaceTimelineDate: '2026-11-12', workspaceTimelineLastValue: 0,
    verticalTimelineSetPosition() {}, updateTimelinePlayhead() {},
    async api(url) {
      if (url.includes('/scrub-frames?')) return { ok: true, async json() { return {
        ready: true, urlBase: `/recordings/.scrub-frames/cam-1/${url.includes('06-00-30') ? '06-00-30' : '06-00-00'}/`,
        manifest: { fps: 15, width: 480, height: 270, columns: 6, rows: 4, frames: 900, sheets: 38 },
      }; } };
      if (url.includes('/trickplay?')) { proxyRequests++; return { ok: true, async json() { return { ready: false }; } }; }
      sheetRequests++;
      requestedSheets.push(url);
      return { ok: true, async blob() { return {}; } };
    },
    async createImageBitmap() { return { width: 2880, height: 1080, close() {} }; },
    setTimeout(fn, ms) { return ms === 220 ? 0 : setTimeout(fn, ms); }, clearTimeout, console,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8'), globals);
  globals.window.previewWorkspaceTimelineScrub(8, 20);
  await new Promise(resolve => setImmediate(resolve));
  for (let i = 0; i < 4; i++) { while (animationFrames.length) animationFrames.shift()(); await new Promise(resolve => setImmediate(resolve)); }
  const first = draws.at(-1);
  globals.window.previewWorkspaceTimelineScrub(9, 20);
  while (animationFrames.length) animationFrames.shift()();
  const second = draws.at(-1);
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(requestedSheets.some(url => url.includes('06-00-30')), 'the adjacent recording is warmed before reaching it');
  globals.window.previewWorkspaceTimelineScrub(8, -20);
  while (animationFrames.length) animationFrames.shift()();
  assert.notDeepEqual(first.slice(1, 3), second.slice(1, 3), 'forward movement paints another atlas cell');
  assert.deepEqual(first.slice(1, 3), draws.at(-1).slice(1, 3), 'reverse movement paints the earlier cell');
  const originalSheetRequests = sheetRequests;
  globals.window.previewWorkspaceTimelineScrub(38, 20);
  await new Promise(resolve => setImmediate(resolve));
  while (animationFrames.length) animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(sheetRequests >= originalSheetRequests, 'next recording uses its own atlas sheet');
  const nextSheetRequests = sheetRequests;
  globals.window.previewWorkspaceTimelineScrub(8, -20);
  while (animationFrames.length) animationFrames.shift()();
  assert.equal(sheetRequests, nextSheetRequests, 'reversing across a recording boundary reuses the decoded sheet');
  assert.equal(mainSeeks, 0, 'preview never seeks the main video');
  assert.equal(proxyRequests, 0, 'ready atlas needs no proxy requests');
}

async function seekResumesPlayback() {
  const listeners = new Map();
  const documentListeners = new Map();
  let plays = 0;
  let loads = 0;
  const video = { dataset: { playback: 'live' }, clientWidth: 640, clientHeight: 360,
    readyState: 2, duration: 30, paused: false, seeking: false, currentTime: 0,
    closest() { return { appendChild() {} }; }, setAttribute() {}, removeAttribute() {},
    addEventListener(type, fn) { listeners.set(type, fn); }, removeEventListener() {},
    pause() { this.paused = true; }, load() { loads++; }, play() { this.paused = false; plays++; return Promise.resolve(); },
    // Safari may never call this callback for a paused, already-decoded frame.
    requestVideoFrameCallback() {} };
  const canvas = { width: 0, height: 0, classList: { add() {}, remove() {} }, setAttribute() {},
    getContext() { return { fillRect() {}, drawImage() {}, set fillStyle(_) {} }; } };
  const status = { innerHTML: '' };
  const document = { body: {}, head: { appendChild() {} }, documentElement: { dataset: {} },
    createElement(tag) { return tag === 'canvas' ? canvas : { setAttribute() {}, classList: { toggle() {} } }; },
    querySelector(selector) { return selector === '#workspace-live' ? video : selector === '#workspace-player' ? status : null; },
    addEventListener(type, callback) { documentListeners.set(type, callback); } };
  const record = { path: 'test.mp4', url: '/test.mp4' };
  const nextRecord = { path: 'next.mp4', url: '/next.mp4' };
  const globals = { document, window: { workspaceCamera: 'cam', returnWorkspaceToLive: async () => {}, addEventListener() {} },
    URLSearchParams, location: { search: '' }, devicePixelRatio: 1, performance: { now: () => 1000 },
    requestAnimationFrame() { return 1; }, cancelAnimationFrame() {}, MutationObserver: class { observe() {} },
    timelineDayLength: () => 86400, timelineRecordAt: value => value >= 30 ? nextRecord : record,
    timelineSeconds: item => item === nextRecord ? 30 : 0, workspaceTimelineRecords: [record, nextRecord],
    workspaceTimelineDate: '2026-11-17', workspacePlaybackUrl: '', workspacePlaybackStartSeconds: 0,
    workspaceTimelineLastValue: 0, verticalTimelineUserScrolling: true, updateTimelinePlayhead() {},
    setWorkspaceMode(mode) { video.dataset.playback = mode; }, esc: value => value,
    async api() { return { ok: true, async json() { return { ready: false }; } }; },
    setTimeout, clearTimeout, console };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8'), globals);
  await globals.window.scrubWorkspaceTimeline(8);
  assert.equal(video.currentTime, 8);
  assert.equal(plays, 1, 'playback resumes after seeking');
  assert.equal(globals.window.workspaceScrubActive, false);
  assert.equal(globals.verticalTimelineUserScrolling, false);
  // A native scroll reaches document capture before the timeline updates its
  // target. The final target must still be committed when scrolling stops.
  documentListeners.get('scroll')({ target: { closest: () => true } });
  await Promise.resolve(); // Browsers can run microtasks between event listeners.
  globals.window.previewWorkspaceTimelineScrub(12, 1);
  await new Promise(resolve => setTimeout(resolve, 450));
  assert.equal(video.currentTime, 12, 'scroll event commits the latest position');
  assert.equal(plays, 2, 'scrolling transitions from preview to playback');
  listeners.get('ended')();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(video.src, nextRecord.url, 'playback advances to the adjacent segment');
  assert.equal(plays, 3, 'playback continues after a segment ends');
  documentListeners.get('scroll')({ target: { closest: () => true } });
  globals.window.previewWorkspaceTimelineScrub(8, -1);
  await new Promise(resolve => setTimeout(resolve, 260));
  assert.equal(video.src, record.url, 'the selected recording starts loading while scrolling');
  const preloaded = loads;
  await new Promise(resolve => setTimeout(resolve, 200));
  assert.equal(loads, preloaded, 'committing reuses the recording load');
  assert.equal(plays, 4, 'the preloaded recording resumes playback');
  documentListeners.get('pointerdown')({ target: { closest: () => true } });
  globals.window.previewWorkspaceTimelineScrub(10, 1);
  await globals.window.returnWorkspaceToLive();
  documentListeners.get('pointerup')({});
  await new Promise(resolve => setTimeout(resolve, 400));
  assert.equal(plays, 4, 'releasing the scrollbar cannot restart a recording after returning live');
}

async function previewAcrossSegments() {
  const frames = [], requests = [];
  const raf = [];
  const proxyListeners = new Map();
  const proxy = { duration: 30, readyState: 1, currentTime: 0,
    addEventListener(type, fn) { proxyListeners.set(type, fn); }, setAttribute() {}, removeAttribute() {}, load() {} };
  const host = { appendChild() {} };
  const video = { dataset: { playback: 'live' }, clientWidth: 100, clientHeight: 100,
    readyState: 0, paused: true, closest: () => host, setAttribute() {}, removeAttribute() {}, addEventListener() {}, pause() {} };
  const canvas = { width: 0, height: 0, classList: { add() {}, remove() {} }, setAttribute() {},
    getContext() { return { fillRect() {}, drawImage(bitmap) { frames.push(bitmap.id); }, set fillStyle(_) {} }; } };
  const document = { body: {}, head: { appendChild() {} }, documentElement: { dataset: {} },
    createElement(tag) { return tag === 'canvas' ? canvas : tag === 'video' ? proxy : { setAttribute() {}, classList: { toggle() {} } }; },
    querySelector(selector) { return selector === '#workspace-live' ? video : null; }, addEventListener() {} };
  const first = { path: 'first.mp4', url: '/first.mp4' }, second = { path: 'second.mp4', url: '/second.mp4' };
  const globals = { document, window: { workspaceCamera: 'cam', returnWorkspaceToLive: async () => {}, addEventListener() {} },
    URLSearchParams, location: { search: '' }, devicePixelRatio: 1, performance: { now: () => 1000 },
    requestAnimationFrame(fn) { raf.push(fn); return raf.length; }, cancelAnimationFrame() {}, MutationObserver: class { observe() {} },
    timelineDayLength: () => 86400, timelineRecordAt: value => value < 30 ? first : second,
    timelineSeconds: record => record === first ? 0 : 30, workspaceTimelineDate: '2026-11-17',
    workspaceTimelineLastValue: 0, verticalTimelineSetPosition() {}, updateTimelinePlayhead() {}, formatTimelineTime: value => String(value),
    api(url) {
      if (url.includes('scrub-frames')) return Promise.resolve({ ok: true, json: async () => ({ ready: false }) });
      return new Promise(resolve => requests.push(resolve));
    },
    async createImageBitmap(blob) { return { id: blob.id, width: 100, height: 100 }; },
    setTimeout(fn, ms) { return ms === 220 ? 0 : setTimeout(fn, ms); }, clearTimeout, console };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8'), globals);
  globals.window.previewWorkspaceTimelineScrub(10, 10);
  await new Promise(resolve => setImmediate(resolve));
  proxyListeners.get('loadedmetadata')(); // Metadata is ready, but decoding never completes.
  while (raf.length) raf.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 1);
  globals.window.previewWorkspaceTimelineScrub(40, 10);
  requests[0]({ ok: true, blob: async () => ({ id: 'first segment' }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(frames, ['first segment'], 'a decoded frame remains visible when scrolling crosses a segment');
}

Promise.resolve().then(main).then(proxyScrubbing).then(atlasScrubbing).then(seekResumesPlayback).then(previewAcrossSegments)
  .then(() => process.exit(0))
  .catch(error => { console.error(error); process.exit(1); });
