const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function checkLiveEdge(initialDate) {
  const source = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'dashboard.html'), 'utf8');
  const minimum = source.slice(source.indexOf('function verticalTimelineMinimumScroll('), source.indexOf('\nfunction verticalTimelineSecondsFromScroll('));
  const paging = source.slice(source.indexOf('let verticalTimelineDayPaging='), source.indexOf('\nconst verticalTimelineDayStyle='));
  const now = new Date();
  const dateKey = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const today = dateKey(now);
  const callbacks = [];
  let scrollHandler, liveCalls = 0, loads = 0, previews = 0;
  const scroll = { scrollTop: 0, scrollHeight: 7200, clientHeight: 500, dataset: {},
    addEventListener(type, callback) { if (type === 'scroll') scrollHandler = callback; } };
  const content = { style: {} }, input = { value: initialDate }, video = { dataset: { playback: 'recording' } };
  const document = { querySelector(selector) { return ({ '.vertical-timeline-scroll': scroll, '.vertical-timeline-content': content,
    '#workspace-date': input, '#workspace-live': video })[selector] || null; } };
  const globals = { document, window: { workspaceCamera: 'cam', workspaceScrubActive: true },
    workspaceTimelineDate: initialDate, workspaceTimelineRecords: [], workspaceTimelineLastValue: 0,
    verticalTimelineHeight: 3600, verticalTimelineUserScrolling: false, verticalTimelineProgrammatic: false,
    verticalTimelineScrollTimer: 0, verticalTimelineScrollEndTimer: 0,
    localDateKey: dateKey, workspaceDayStart: date => new Date(`${date}T00:00:00`),
    timelineDayLength: () => 86400, liveTimelineSeconds: () => (Date.now() - new Date(`${today}T00:00:00`).getTime()) / 1000,
    buildVerticalTimeline() {}, updateTimelinePlayhead() {},
    previewWorkspaceTimelineScrub() { previews++; },
    returnWorkspaceToLive() { liveCalls++; return Promise.resolve(); },
    loadCameraTimeline() { loads++; return Promise.resolve(); },
    requestAnimationFrame(callback) { callbacks.push(callback); }, clearTimeout, setTimeout, console };
  vm.createContext(globals);
  vm.runInContext(minimum + '\n' + paging, globals);
  vm.runInContext('buildVerticalTimeline()', globals);
  scroll.scrollTop = initialDate === today ? vm.runInContext('verticalTimelineMinimumScroll(document.querySelector(".vertical-timeline-scroll"))', globals) + 150 : 150;
  scrollHandler({ stopImmediatePropagation() {} });
  assert.equal(previews, 1, 'scrolling away from live previews a recording');
  scroll.scrollTop = 0;
  scrollHandler({ stopImmediatePropagation() {} });
  await Promise.resolve();
  assert.equal(liveCalls, 1, 'scrolling to the top returns to live');
  assert.equal(globals.workspaceTimelineDate, today);
  assert.equal(input.value, today);
  assert.equal(loads, initialDate === today ? 0 : 1, 'an older-date view reloads today');
  if (initialDate === today) assert.ok(scroll.scrollTop > 0, 'future times are clamped');
}

const yesterday = new Date(); yesterday.setDate(yesterday.getDate() - 1);
const key = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
Promise.resolve().then(() => checkLiveEdge(key(new Date()))).then(() => checkLiveEdge(key(yesterday)))
  .catch(error => { console.error(error); process.exitCode = 1; });
