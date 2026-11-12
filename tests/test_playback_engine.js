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
    createElement(tag) { return tag === 'canvas' ? canvas : { textContent: '' }; },
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
    timelineDayLength: () => 86400, timelineRecordAt: () => record,
    timelineSeconds: () => 0, workspaceTimelineDate: '2026-11-12',
    workspaceTimelineLastValue: 0, verticalTimelineSetPosition() {},
    updateTimelinePlayhead() {}, esc: value => value,
    async api() { fetched++; return { ok: true, async blob() { return {}; } }; },
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
}

main().catch(error => { console.error(error); process.exitCode = 1; });
