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
    setTimeout, clearTimeout, console,
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
  window.previewWorkspaceTimelineScrub(20, -20);
  assert.doesNotMatch(status.innerHTML, /No recording/, 'returning to footage clears the gap status');
}

async function proxyScrubbing() {
  const drawnTimes = [];
  const pendingFrames = [];
  const listeners = new Map();
  let snapshotRequests = 0;
  let playbackLoads = 0;
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
      if (url.includes('/scrub-frames?')) return { ok: true, async json() { return { ready: false, failed: true }; } };
      if (url.includes('/trickplay?')) return { ok: true, async json() { return { ready: true, url: '/recordings/.trickplay-cache/cam-1/proxy.mp4' }; } };
      snapshotRequests++;
      return { ok: true, async blob() { return {}; } };
    },
    async createImageBitmap() { return { width: 640, height: 360 }; },
    setTimeout, clearTimeout, console,
  };
  const source = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8');
  vm.runInNewContext(source, globals);
  globals.window.previewWorkspaceTimelineScrub(8, -20);
  await new Promise(resolve => setImmediate(resolve));
  // The atlas failure is observed on the next pointer update before falling
  // back to the old proxy for this segment.
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
  assert.equal(snapshotRequests, 0, 'ready proxy needs no still-frame requests');
}

async function atlasScrubbing() {
  const draws = [];
  const animationFrames = [];
  let mainSeeks = 0;
  let proxyRequests = 0;
  let sheetRequests = 0;
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
    timelineSeconds: item => item === nextRecord ? 30 : 0,
    workspaceTimelineDate: '2026-11-12', workspaceTimelineLastValue: 0,
    verticalTimelineSetPosition() {}, updateTimelinePlayhead() {},
    async api(url) {
      if (url.includes('/scrub-frames?')) return { ok: true, async json() { return {
        ready: true, urlBase: `/recordings/.scrub-frames/cam-1/${url.includes('06-00-30') ? '06-00-30' : '06-00-00'}/`,
        manifest: { fps: 15, width: 480, height: 270, columns: 6, rows: 4, frames: 900, sheets: 38 },
      }; } };
      if (url.includes('/trickplay?')) { proxyRequests++; return { ok: true, async json() { return { ready: false }; } }; }
      sheetRequests++;
      return { ok: true, async blob() { return {}; } };
    },
    async createImageBitmap() { return { width: 2880, height: 1080, close() {} }; },
    setTimeout, clearTimeout, console,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8'), globals);
  globals.window.previewWorkspaceTimelineScrub(8, 20);
  await new Promise(resolve => setImmediate(resolve));
  for (let i = 0; i < 4; i++) { while (animationFrames.length) animationFrames.shift()(); await new Promise(resolve => setImmediate(resolve)); }
  const first = draws.at(-1);
  globals.window.previewWorkspaceTimelineScrub(9, 20);
  while (animationFrames.length) animationFrames.shift()();
  const second = draws.at(-1);
  globals.window.previewWorkspaceTimelineScrub(8, -20);
  while (animationFrames.length) animationFrames.shift()();
  assert.notDeepEqual(first.slice(1, 3), second.slice(1, 3), 'forward movement paints another atlas cell');
  assert.deepEqual(first.slice(1, 3), draws.at(-1).slice(1, 3), 'reverse movement paints the earlier cell');
  const originalSheetRequests = sheetRequests;
  globals.window.previewWorkspaceTimelineScrub(38, 20);
  await new Promise(resolve => setImmediate(resolve));
  while (animationFrames.length) animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(sheetRequests > originalSheetRequests, 'next recording loads its own atlas sheet');
  const nextSheetRequests = sheetRequests;
  globals.window.previewWorkspaceTimelineScrub(8, -20);
  while (animationFrames.length) animationFrames.shift()();
  assert.equal(sheetRequests, nextSheetRequests, 'reversing across a recording boundary reuses the decoded sheet');
  assert.equal(mainSeeks, 0, 'preview never seeks the main video');
  assert.equal(proxyRequests, 0, 'ready atlas needs no proxy requests');
}

Promise.resolve().then(main).then(proxyScrubbing).then(atlasScrubbing)
  .then(() => process.exit(0))
  .catch(error => { console.error(error); process.exit(1); });
