// Regression check: decoded recording frames must become visible during a
// timeline move, before the normal player receives a committed seek.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  let drawn = 0;
  let clears = 0;
  let fetched = 0;
  let committed = 0;
  let clock = 1000;
  const visible = new Set();
  const animationFrames = [];
  const context2d = { fillRect() { clears++; }, drawImage() { drawn++; }, set fillStyle(_) {} };
  const canvas = {
    width: 0, height: 0, classList: {
      add(name) { visible.add(name); }, remove(name) { visible.delete(name); },
    },
    setAttribute() {}, getContext() { return context2d; },
  };
  const host = { appendChild() {} };
  const video = {
    dataset: { playback: 'live' }, clientWidth: 1920, clientHeight: 1080,
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
    document, window, URLSearchParams, location: { search: '' }, devicePixelRatio: 2,
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
  assert.equal(canvas.width, 1280, 'large displays use a bounded preview canvas');
  assert.equal(canvas.height, 720);
  assert.equal(visible.has('visible'), true);
  assert.equal(committed, 0, 'normal playback must not seek during preview');
  clock += 200;
  window.previewWorkspaceTimelineScrub(20, -60);
  animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fetched, 2, 'reverse movement should request another frame');
  assert.equal(drawn, 2, 'reverse frame should appear before playback commits');
  assert.equal(committed, 0);
  const clearsBeforeGap = clears;
  window.previewWorkspaceTimelineScrub(40, 20);
  assert.doesNotMatch(status.innerHTML, /No recording/, 'brief gaps do not interrupt scrolling');
  assert.equal(visible.has('visible'), true, 'a gap keeps the preview visible');
  assert.equal(clears, clearsBeforeGap, 'a gap does not erase the last decoded picture');
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
  const laterRecords = [30, 60, 90].map(second => ({ path: `cam-1/segment-${second}.mp4`, url: `/recordings/cam-1/segment-${second}.mp4`, second }));
  const globals = {
    document, window: { workspaceCamera: 'cam-1', returnWorkspaceToLive: async () => {}, addEventListener() {} },
    URLSearchParams, location: { search: '' }, devicePixelRatio: 1,
    performance: { now: () => 1000 },
    requestAnimationFrame(callback) { pendingFrames.push(callback); return pendingFrames.length; },
    cancelAnimationFrame() {}, MutationObserver: class { observe() {} },
    timelineDayLength: () => 86400, timelineRecordAt: value => value < 30 ? record : laterRecords[Math.min(2, Math.floor(value / 30) - 1)],
    timelineSeconds: item => item.second || 0, workspaceTimelineDate: '2026-11-12',
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
  await new Promise(resolve => setTimeout(resolve, 140));
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
  globals.window.previewWorkspaceTimelineScrub(35, 100);
  globals.window.previewWorkspaceTimelineScrub(65, 100);
  globals.window.previewWorkspaceTimelineScrub(95, 100);
  await new Promise(resolve => setTimeout(resolve, 140));
  assert.equal(proxy.src, laterRecords[2].url, 'the proxy opens the settled segment');
  assert.equal(playbackLoads, 3, 'rapidly crossed segments do not each open a video decoder');
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
    verticalTimelineSetPosition() {}, updateTimelinePlayhead() {}, formatTimelineTime: value => String(value),
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
  const first = draws.filter(args => args.length === 9).at(-1);
  globals.window.previewWorkspaceTimelineScrub(9, 20);
  while (animationFrames.length) animationFrames.shift()();
  const second = draws.filter(args => args.length === 9).at(-1);
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(requestedSheets.some(url => url.includes('06-00-30')), 'the adjacent recording is warmed before reaching it');
  assert.ok(requestedSheets.some(url => url.includes('06-00-30/001.jpg')), 'the next wheel position warms its predicted atlas sheet');
  globals.window.previewWorkspaceTimelineScrub(8, -20);
  while (animationFrames.length) animationFrames.shift()();
  assert.notDeepEqual(first.slice(1, 3), second.slice(1, 3), 'forward movement paints another atlas cell');
  assert.deepEqual(first.slice(1, 3), draws.filter(args => args.length === 9).at(-1).slice(1, 3), 'reverse movement paints the earlier cell');
  const originalSheetRequests = sheetRequests;
  globals.window.previewWorkspaceTimelineScrub(38, 20);
  await new Promise(resolve => setImmediate(resolve));
  while (animationFrames.length) animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(sheetRequests >= originalSheetRequests, 'next recording uses its own atlas sheet');
  const cachedTargetRequests = requestedSheets.filter(url => url.includes('06-00-00/005.jpg')).length;
  globals.window.previewWorkspaceTimelineScrub(8, -20);
  while (animationFrames.length) animationFrames.shift()();
  assert.equal(requestedSheets.filter(url => url.includes('06-00-00/005.jpg')).length, cachedTargetRequests,
    'reversing across a recording boundary reuses the decoded target sheet');
  assert.equal(mainSeeks, 0, 'preview never seeks the main video');
  assert.equal(proxyRequests, 0, 'ready atlas needs no proxy requests');
}

async function seekResumesPlayback() {
  const listeners = new Map();
  const documentListeners = new Map();
  let plays = 0;
  let loads = 0;
  let nextStart = 30;
  let clock = 1000;
  let position = 0;
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
    URLSearchParams, location: { search: '' }, devicePixelRatio: 1, performance: { now: () => clock },
    requestAnimationFrame() { return 1; }, cancelAnimationFrame() {}, MutationObserver: class { observe() {} },
    timelineDayLength: () => 86400, timelineRecordAt: value => value >= nextStart ? nextRecord : value < 30 ? record : undefined,
    timelineSeconds: item => item === nextRecord ? nextStart : 0, workspaceTimelineRecords: [record, nextRecord],
    workspaceTimelineDate: '2026-11-17', workspacePlaybackUrl: '', workspacePlaybackStartSeconds: 0,
    workspaceTimelineLastValue: 0, verticalTimelineUserScrolling: true, updateTimelinePlayhead(value) { position = value; },
    setWorkspaceMode(mode) { video.dataset.playback = mode; }, esc: value => value, formatTimelineTime: value => String(value),
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
  nextStart = 36;
  globals.workspaceUserPaused = true;
  await globals.window.scrubWorkspaceTimeline(35);
  clock += 1000;
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.equal(position, 35, 'paused playback remains at the selected gap');
  assert.equal(globals.window.toggleWorkspaceGap(), true);
  clock += 500;
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.equal(position, 35.5, 'the timeline advances through the gap at playback speed');
  assert.equal(plays, 3, 'the next recording does not start early');
  assert.equal(globals.window.toggleWorkspaceGap(), true);
  clock += 2000;
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.equal(position, 35.5, 'pausing freezes the gap clock');
  globals.window.toggleWorkspaceGap();
  clock += 500;
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.equal(plays, 4, 'playback resumes when the timeline reaches the next recording');
  nextStart = 30;
  globals.workspaceUserPaused = true;
  await globals.window.scrubWorkspaceTimeline(8);
  assert.equal(video.paused, true, 'a paused user stays paused after seeking');
  globals.workspaceUserPaused = false;

  video.dataset.playback = 'live';
  globals.workspacePlaybackUrl = '';
  const loadsBeforeScroll = loads;
  documentListeners.get('scroll')({ target: { closest: () => true } });
  globals.window.previewWorkspaceTimelineScrub(8, -1);
  await new Promise(resolve => setTimeout(resolve, 260));
  assert.equal(loads, loadsBeforeScroll, 'scrolling does not start a competing full recording load');
  await new Promise(resolve => setTimeout(resolve, 200));
  assert.equal(video.src, record.url, 'the settled recording is selected for playback');
  assert.equal(loads, loadsBeforeScroll + 1, 'the recording is loaded after scrolling settles');
  assert.equal(plays, 6, 'the settled recording resumes playback');
  documentListeners.get('pointerdown')({ target: { closest: () => true } });
  globals.window.previewWorkspaceTimelineScrub(10, 1);
  await globals.window.returnWorkspaceToLive();
  documentListeners.get('pointerup')({});
  await new Promise(resolve => setTimeout(resolve, 400));
  assert.equal(plays, 6, 'releasing the scrollbar cannot restart a recording after returning live');
  let attempts = 0;
  video.play = function () {
    attempts++;
    if (attempts === 1) return Promise.reject(Object.assign(new Error('unsupported container'), {name: 'NotSupportedError'}));
    this.paused = false;
    return Promise.resolve();
  };
  globals.workspacePlaybackUrl = '';
  await globals.window.scrubWorkspaceTimeline(8);
  assert.equal(video.src, `${record.url}?format=mp4`, 'a format error retries with a compatible recording');
  assert.equal(attempts, 2, 'the compatible recording is attempted only once');
  video.play = () => Promise.reject(Object.assign(new Error('autoplay blocked'), {name: 'NotAllowedError'}));
  globals.workspacePlaybackUrl = '';
  await globals.window.scrubWorkspaceTimeline(8);
  assert.equal(video.src, record.url, 'autoplay errors do not trigger conversion');
}

async function previewAcrossSegments() {
  const frames = [], requests = [];
  let clock = 1000;
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
    URLSearchParams, location: { search: '' }, devicePixelRatio: 1, performance: { now: () => clock },
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
  await new Promise(resolve => setTimeout(resolve, 140));
  proxyListeners.get('loadedmetadata')(); // Metadata is ready, but decoding never completes.
  while (raf.length) raf.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 1);
  clock += 200;
  globals.window.previewWorkspaceTimelineScrub(20, 10);
  while (raf.length) raf.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 2);
  clock += 200;
  globals.window.previewWorkspaceTimelineScrub(40, 10);
  requests[1]({ ok: true, blob: async () => ({ id: 'recent frame' }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(frames, ['recent frame'], 'a recent frame can still be displayed while crossing a segment');
  requests[0]({ ok: true, blob: async () => ({ id: 'older frame' }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(frames, ['recent frame'], 'an older completion cannot overwrite a newer frame');
}

async function delayedAtlasFramesContinueAcrossSegments() {
  const raf = [], loads = new Map(), paints = [];
  let clock = 1000;
  const records = [0, 30, 60].map(start => ({path: `${start}.mp4`, url: `/${start}.mp4`, start}));
  const video = {dataset: {playback: 'live'}, clientWidth: 100, clientHeight: 100, readyState: 0, paused: true,
    closest: () => ({appendChild() {}}), setAttribute() {}, removeAttribute() {}, addEventListener() {}, pause() {}};
  const canvas = {width: 0, height: 0, classList: {add() {}, remove() {}}, setAttribute() {},
    getContext: () => ({fillRect() {}, drawImage(bitmap) {paints.push(bitmap.id);}, set fillStyle(_) {}})};
  const document = {body: {}, head: {appendChild() {}}, documentElement: {dataset: {}},
    createElement: tag => tag === 'canvas' ? canvas : {setAttribute() {}, classList: {toggle() {}}},
    querySelector: selector => selector === '#workspace-live' ? video : null, addEventListener() {}};
  const globals = {document, window: {workspaceCamera: 'cam', returnWorkspaceToLive: async () => {}, addEventListener() {}},
    URLSearchParams, location: {search: ''}, devicePixelRatio: 1, performance: {now: () => clock},
    requestAnimationFrame(fn) {raf.push(fn); return raf.length;}, cancelAnimationFrame() {}, MutationObserver: class {observe() {}},
    timelineDayLength: () => 86400, timelineRecordAt: value => records[Math.min(2, Math.floor(value / 30))],
    timelineSeconds: record => record.start, workspaceTimelineRecords: records, workspaceTimelineDate: '2026-11-17', workspaceTimelineLastValue: 0,
    verticalTimelineSetPosition() {}, updateTimelinePlayhead() {}, formatTimelineTime: String,
    api(url) {
      if (url.includes('scrub-frames?')) {
        const record = records.find(item => url.endsWith(`path=${encodeURIComponent(item.path)}`));
        return Promise.resolve({ok: true, json: async () => ({ready: true, urlBase: `/${record.start}/`,
          manifest: {fps: 1, frames: 30, columns: 6, rows: 4, sheets: 2, width: 10, height: 10}})});
      }
      if (url.includes('/snapshot?')) return Promise.resolve({ok: false});
      return new Promise(resolve => loads.set(url, resolve));
    },
    createImageBitmap: async blob => ({id: blob.id, width: 60, height: 40, close() {}}),
    setTimeout(fn, ms) {return ms === 220 ? 0 : setTimeout(fn, ms);}, clearTimeout, console};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8'), globals);
  async function target(value) {
    globals.window.previewWorkspaceTimelineScrub(value, 1);
    for (let i = 0; i < 3; i++) {await new Promise(resolve => setImmediate(resolve)); while (raf.length) raf.shift()();}
    clock += 100;
  }
  await target(10); await target(11); // Prefetch the next segment before visiting it.
  assert.ok(loads.has('/30/000.jpg'), 'the next segment is already loading');
  await target(40); await target(70);
  loads.get('/30/000.jpg')({ok: true, blob: async () => ({id: 'second'})});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(paints, ['second'], 'a trailing sheet advances the preview while crossing segments');
  loads.get('/0/000.jpg')({ok: true, blob: async () => ({id: 'first'})});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(paints, ['second'], 'a slower older sheet cannot rewind the displayed preview');
  await target(40);
  loads.get('/60/000.jpg')({ok: true, blob: async () => ({id: 'third'})});
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(!paints.includes('third'), 'an old direction cannot paint after reversal');
}

async function decoderRequiresPlayback() {
  const listeners = new Map();
  let plays = 0;
  const video = { dataset: { playback: 'recording' }, clientWidth: 640, clientHeight: 360,
    readyState: 1, duration: 30, paused: true, seeking: false, _time: 0,
    get currentTime() { return this._time; }, set currentTime(value) { this._time = value; this.seeking = true; },
    closest() { return { appendChild() {} }; }, setAttribute() {}, removeAttribute() {},
    addEventListener(type, callback) { listeners.set(type, callback); }, removeEventListener() {},
    pause() { this.paused = true; }, play() {
      plays++; this.paused = false; this.seeking = false; this.readyState = 2;
      listeners.get('seeked')?.(); return Promise.resolve();
    } };
  const canvas = { width: 0, height: 0, classList: { add() {}, remove() {} }, setAttribute() {},
    getContext() { return { fillRect() {}, drawImage() {}, set fillStyle(_) {} }; } };
  const status = { innerHTML: '' };
  const document = { body: {}, head: { appendChild() {} }, documentElement: { dataset: {} },
    createElement(tag) { return tag === 'canvas' ? canvas : { setAttribute() {}, classList: { toggle() {} } }; },
    querySelector(selector) { return selector === '#workspace-live' ? video : selector === '#workspace-player' ? status : null; }, addEventListener() {} };
  const record = { path: 'test.mp4', url: '/test.mp4' };
  const globals = { document, window: { workspaceCamera: 'cam', returnWorkspaceToLive: async () => {}, addEventListener() {} },
    URLSearchParams, location: { search: '' }, devicePixelRatio: 1, performance: { now: () => 1000 },
    requestAnimationFrame() { return 1; }, cancelAnimationFrame() {}, MutationObserver: class { observe() {} },
    timelineDayLength: () => 86400, timelineRecordAt: () => record, timelineSeconds: () => 0,
    workspaceTimelineDate: '2026-11-17', workspacePlaybackUrl: record.url, workspacePlaybackStartSeconds: 0,
    workspaceTimelineLastValue: 0, updateTimelinePlayhead() {}, setWorkspaceMode() {}, esc: value => value, setTimeout, clearTimeout, console };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8'), globals);
  await Promise.race([globals.window.scrubWorkspaceTimeline(8), new Promise((_, reject) => setTimeout(() => reject(new Error('seek stalled')), 300))]);
  assert.equal(plays, 1, 'playback starts before waiting for a decoded frame');
  assert.equal(video.currentTime, 8);
  assert.equal(globals.window.workspaceScrubActive, false);
  video.readyState = 1;
  video.play = () => Promise.reject(Object.assign(new Error('autoplay blocked'), { name: 'NotAllowedError' }));
  await globals.window.scrubWorkspaceTimeline(9);
  assert.match(status.innerHTML, /press Play/, 'blocked autoplay offers a way to continue immediately');
  assert.equal(globals.window.workspaceScrubActive, false, 'the player controls remain accessible');
}

async function deduplicatePreviewRequests() {
  const animationFrames = [], requests = [];
  let clock = 1000, warmRequests = 0, drawn = 0;
  const host = { appendChild() {} };
  const video = { dataset: { playback: 'live' }, clientWidth: 100, clientHeight: 100,
    readyState: 0, paused: true, closest: () => host, setAttribute() {}, removeAttribute() {}, addEventListener() {}, pause() {} };
  const canvas = { width: 0, height: 0, classList: { add() {}, remove() {} }, setAttribute() {},
    getContext() { return { fillRect() {}, drawImage() { drawn++; }, set fillStyle(_) {} }; } };
  const document = { body: {}, head: { appendChild() {} }, documentElement: { dataset: {} },
    createElement(tag) { return tag === 'canvas' ? canvas : { setAttribute() {}, classList: { toggle() {} } }; },
    querySelector(selector) { return selector === '#workspace-live' ? video : null; }, addEventListener() {} };
  const record = { path: 'test.mp4', url: '/test.mp4' };
  const globals = { document, window: { workspaceCamera: 'cam', returnWorkspaceToLive: async () => {}, addEventListener() {} },
    URLSearchParams, location: { search: '' }, devicePixelRatio: 1, performance: { now: () => clock },
    requestAnimationFrame(fn) { animationFrames.push(fn); return animationFrames.length; }, cancelAnimationFrame() {}, MutationObserver: class { observe() {} },
    timelineDayLength: () => 86400, timelineRecordAt: () => record, timelineSeconds: () => 0,
    workspaceTimelineDate: '2026-11-17', workspaceTimelineLastValue: 0,
    verticalTimelineSetPosition() {}, updateTimelinePlayhead() {}, formatTimelineTime: value => String(value),
    api(url) {
      if (url.includes('scrub-frames')) {
        if (url.includes('prepare=1')) warmRequests++;
        return Promise.resolve({ ok: true, json: async () => ({ ready: false }) });
      }
      return new Promise(resolve => requests.push({ url, resolve }));
    },
    createImageBitmap: async () => ({ width: 100, height: 100 }),
    setTimeout(fn, ms) { return ms === 220 ? 0 : setTimeout(fn, ms); }, clearTimeout, console };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8'), globals);
  globals.window.previewWorkspaceTimelineScrub(10.1, 1);
  while (animationFrames.length) animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  clock += 200;
  globals.window.previewWorkspaceTimelineScrub(10.2, 1);
  while (animationFrames.length) animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 1, 'identical in-flight preview frames share one request');
  assert.match(requests[0].url, /at=10\.00/);
  requests[0].resolve({ ok: true, blob: async () => ({}) });
  await new Promise(resolve => setImmediate(resolve));
  clock += 200;
  globals.window.previewWorkspaceTimelineScrub(40.9, 100);
  while (animationFrames.length) animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(requests[1].url, /at=40\.00/, 'fast motion reuses a coarser preview frame');
  requests[1].resolve({ ok: true, blob: async () => ({}) });
  await new Promise(resolve => setImmediate(resolve));
  clock += 200;
  globals.window.previewWorkspaceTimelineScrub(60.1, 100);
  while (animationFrames.length) animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  clock += 200;
  globals.window.previewWorkspaceTimelineScrub(80.1, 100);
  while (animationFrames.length) animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 4, 'two uncached frame requests are in flight');
  const drawnBeforeReverse = drawn;
  clock += 16;
  globals.window.previewWorkspaceTimelineScrub(10.1, -100);
  while (animationFrames.length) animationFrames.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 4, 'reversing over a cached frame does not fetch it again');
  assert.ok(drawn > drawnBeforeReverse, 'cached frames appear even while both decode slots are busy');
  await new Promise(resolve => setTimeout(resolve, 180));
  assert.equal(warmRequests, 1, 'a visited segment is prepared once without blocking the preview');
}

async function warmBeforeScrolling() {
  const requested = [];
  let activeChecks = 0, maxChecks = 0;
  const video = {dataset: {playback: 'live'}, clientWidth: 100, clientHeight: 100, readyState: 0, paused: true,
    closest: () => ({appendChild() {}}), setAttribute() {}, removeAttribute() {}, addEventListener() {}, pause() {}};
  const canvas = {width: 0, height: 0, classList: {add() {}, remove() {}}, setAttribute() {},
    getContext: () => ({fillRect() {}, drawImage() {}, set fillStyle(_) {}})};
  const document = {body: {}, head: {appendChild() {}}, documentElement: {dataset: {}},
    createElement(tag) { return tag === 'canvas' ? canvas : {setAttribute() {}, classList: {toggle() {}}}; },
    querySelector(selector) { return selector === '#workspace-live' ? video : null; }, addEventListener() {}};
  const records = Array.from({length: 10}, (_, i) => ({path: `cam/${i}.mp4`, url: `/recordings/cam/${i}.mp4`, startSeconds: i * 30, duration: 30}));
  const globals = {document, window: {workspaceCamera: 'cam', openLiveWorkspace: async () => {}, returnWorkspaceToLive: async () => {}, addEventListener() {}},
    URLSearchParams, location: {search: ''}, devicePixelRatio: 1, performance: {now: () => 1000},
    requestAnimationFrame() {return 1;}, cancelAnimationFrame() {}, MutationObserver: class {observe() {}},
    timelineDayLength: () => 86400, timelineSeconds: item => item.startSeconds,
    workspaceTimelineRecords: records, workspaceTimelineDate: '2026-11-17',
    api: async url => {
      requested.push(url);
      if (!url.includes('scrub-frames?')) return {ok: true, blob: async () => ({})};
      activeChecks++;
      maxChecks = Math.max(maxChecks, activeChecks);
      await new Promise(resolve => setImmediate(resolve));
      activeChecks--;
      return {ok: true, json: async () => ({ready: true, urlBase: '/atlas/', manifest: {fps: 1, frames: 30, columns: 6, rows: 4, sheets: 2}})};
    },
    createImageBitmap: async () => ({close() {}}), setTimeout, clearTimeout, console};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8'), globals);
  await globals.window.openLiveWorkspace('cam');
  for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(requested.filter(url => url.includes('scrub-frames?')).length, 10, 'five minutes of 30-second segments are warmed before the first scroll');
  assert.equal(maxChecks, 2, 'at most two nearby recordings compete for preview bandwidth');
  assert.ok(requested.some(url => url.endsWith('.jpg')), 'preview sheets are decoded in advance');
}

async function cachedAtlasMovesBetweenTargets() {
  const raf = [], cells = [];
  let clock = 1000;
  const video = {dataset: {playback: 'live'}, clientWidth: 100, clientHeight: 100, readyState: 0, paused: true,
    closest: () => ({appendChild() {}}), setAttribute() {}, removeAttribute() {}, addEventListener() {}, pause() {}};
  const canvas = {width: 0, height: 0, classList: {add() {}, remove() {}}, setAttribute() {},
    getContext: () => ({fillRect() {}, drawImage(...args) {if (args.length === 9) cells.push(args[2] / 10 * 6 + args[1] / 10);}, set fillStyle(_) {}})};
  const document = {body: {}, head: {appendChild() {}}, documentElement: {dataset: {}},
    createElement(tag) {return tag === 'canvas' ? canvas : {setAttribute() {}, classList: {toggle() {}}};},
    querySelector(selector) {return selector === '#workspace-live' ? video : null;}, addEventListener() {}};
  const record = {path: 'test.mp4', url: '/test.mp4'};
  const globals = {document, window: {workspaceCamera: 'cam', returnWorkspaceToLive: async () => {}, addEventListener() {}},
    URLSearchParams, location: {search: ''}, devicePixelRatio: 1, performance: {now: () => clock},
    requestAnimationFrame(fn) {raf.push(fn); return raf.length;}, cancelAnimationFrame() {}, MutationObserver: class {observe() {}},
    timelineDayLength: () => 86400, timelineRecordAt: () => record, timelineSeconds: () => 0,
    workspaceTimelineRecords: [record], workspaceTimelineDate: '2026-11-17', workspaceTimelineLastValue: 0,
    verticalTimelineSetPosition() {}, updateTimelinePlayhead() {}, formatTimelineTime: String,
    api: async url => url.includes('scrub-frames?')
      ? {ok: true, json: async () => ({ready: true, urlBase: '/atlas/', manifest: {fps: 1, frames: 24, columns: 6, rows: 4, width: 10, height: 10, sheets: 1}})}
      : {ok: true, blob: async () => ({})},
    createImageBitmap: async () => ({width: 60, height: 40, close() {}}),
    setTimeout(fn, ms) {return ms === 220 ? 0 : setTimeout(fn, ms);}, clearTimeout, console};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8'), globals);
  globals.window.previewWorkspaceTimelineScrub(2, 0);
  for (let i = 0; i < 5; i++) {await new Promise(resolve => setImmediate(resolve)); while (raf.length) raf.shift()();}
  assert.equal(cells.at(-1), 2);
  cells.length = 0;
  clock += 50;
  globals.window.previewWorkspaceTimelineScrub(20, 1);
  while (raf.length) raf.shift()();
  assert.ok(cells.length > 2, 'cached intermediate frames appear between large movements');
  assert.equal(cells.at(-1), 20);
  assert.ok(cells.every((value, i) => i === 0 || value >= cells[i - 1]), 'intermediate frames stay in order');
  cells.length = 0;
  clock += 50;
  globals.window.previewWorkspaceTimelineScrub(2, -1);
  while (raf.length) raf.shift()();
  assert.ok(cells.length > 2);
  assert.equal(cells.at(-1), 2);
  assert.ok(cells.every((value, i) => i === 0 || value <= cells[i - 1]), 'reverse frames stay in order');
}

async function decodedScrubbingKeepsFramesOrdered() {
  const listeners = new Map(), raf = [], drawn = [];
  let clock = 1000, position = 0, frameCallback, atlasAvailable = false, atlasPaints = 0;
  const proxy = {duration: 30, readyState: 2, paused: true, videoWidth: 100, videoHeight: 100,
    addEventListener(type, fn) {listeners.set(type, fn);}, setAttribute() {}, removeAttribute() {}, load() {},
    requestVideoFrameCallback(fn) {frameCallback = fn; return 1;},
    get currentTime() {return position;}, set currentTime(value) {position = value; listeners.get('seeked')?.();},
    play() {this.paused = false; return Promise.resolve();}, pause() {this.paused = true;}};
  const video = {dataset: {playback: 'live'}, clientWidth: 100, clientHeight: 100, readyState: 0, paused: true,
    closest: () => ({appendChild() {}}), setAttribute() {}, removeAttribute() {}, addEventListener() {}, pause() {}};
  const canvas = {width: 0, height: 0, classList: {add() {}, remove() {}}, setAttribute() {},
    getContext: () => ({fillRect() {}, drawImage(source) {if (source === proxy) drawn.push(position); else atlasPaints++;}, set fillStyle(_) {}})};
  const document = {body: {}, head: {appendChild() {}}, documentElement: {dataset: {}},
    createElement(tag) {return tag === 'canvas' ? canvas : tag === 'video' ? proxy : {setAttribute() {}, classList: {toggle() {}}};},
    querySelector(selector) {return selector === '#workspace-live' ? video : null;}, addEventListener() {}};
  let record = {path: 'test.mp4', url: '/test.mp4'};
  const globals = {document, window: {workspaceCamera: 'cam', returnWorkspaceToLive: async () => {}, addEventListener() {}},
    HTMLVideoElement: function Video() {}, URLSearchParams, location: {search: ''}, devicePixelRatio: 1,
    performance: {now: () => clock}, requestAnimationFrame(fn) {raf.push(fn); return raf.length;}, cancelAnimationFrame() {},
    MutationObserver: class {observe() {}}, timelineDayLength: () => 86400, timelineRecordAt: () => record,
    timelineSeconds: () => 0, workspaceTimelineDate: '2026-11-17', workspaceTimelineLastValue: 0,
    verticalTimelineSetPosition() {}, updateTimelinePlayhead() {}, formatTimelineTime: value => String(value),
    api: async url => atlasAvailable && url.includes('scrub-frames?')
      ? {ok: true, json: async () => ({ready: true, urlBase: '/atlas/', manifest: {fps: 8, frames: 240, columns: 6, rows: 4, sheets: 10}})}
      : atlasAvailable && url.startsWith('/atlas/') ? {ok: true, blob: async () => ({})} : {ok: false},
    createImageBitmap: async () => ({width: 100, height: 100, close() {}}),
    setTimeout(fn, ms) {return ms === 220 ? 0 : setTimeout(fn, ms);}, clearTimeout, console};
  globals.HTMLVideoElement.prototype.requestVideoFrameCallback = () => {};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8'), globals);
  globals.window.previewWorkspaceTimelineScrub(8, 0);
  clock += 33;
  globals.window.previewWorkspaceTimelineScrub(8.033, 1);
  await new Promise(resolve => setTimeout(resolve, 140));
  await new Promise(resolve => setImmediate(resolve));
  listeners.get('loadedmetadata')();
  while (raf.length) raf.shift()();
  position = 8.033; frameCallback(0, {mediaTime: 8.033});
  assert.equal(drawn.length, 1, 'a decoded video frame is shown');
  position = 8.01; frameCallback(0, {mediaTime: 8.01});
  assert.equal(drawn.length, 1, 'an older forward frame cannot replace it');
  clock += 33;
  globals.window.previewWorkspaceTimelineScrub(8.0, -1);
  while (raf.length) raf.shift()();
  position = 8.0; frameCallback(0, {mediaTime: 8.0});
  assert.equal(drawn.length, 2, 'a decoded frame is shown after reversing direction');
  assert.equal(globals.window.cctvScrubMetrics.snapshot().sources.video, 2);
  // The next browser decoder loads metadata but never invokes its frame
  // callback. Cached atlas frames must continue to update during the scrub.
  record = {path: 'next.mp4', url: '/next.mp4'};
  atlasAvailable = true;
  clock += 33;
  globals.window.previewWorkspaceTimelineScrub(8.033, 1);
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setTimeout(resolve, 140));
  listeners.get('loadedmetadata')();
  for (let i = 0; i < 4; i++) {
    while (raf.length) raf.shift()();
    await new Promise(resolve => setImmediate(resolve));
  }
  const before = atlasPaints;
  clock += 250;
  globals.window.previewWorkspaceTimelineScrub(8.16, 1);
  for (let i = 0; i < 4; i++) {
    while (raf.length) raf.shift()();
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.ok(atlasPaints > before, 'a ready decoder without video frames does not freeze atlas previews');
}

Promise.resolve().then(main).then(proxyScrubbing).then(atlasScrubbing).then(seekResumesPlayback).then(decoderRequiresPlayback).then(previewAcrossSegments).then(delayedAtlasFramesContinueAcrossSegments).then(deduplicatePreviewRequests).then(warmBeforeScrolling).then(cachedAtlasMovesBetweenTargets).then(decodedScrubbingKeepsFramesOrdered)
  .then(() => process.exit(0))
  .catch(error => { console.error(error); process.exit(1); });
