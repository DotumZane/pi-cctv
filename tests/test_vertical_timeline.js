const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function checkRecordedDuration() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'timeline-enhancements.js'), 'utf8');
  const duration = source.slice(source.indexOf('function workspaceRecordDuration('), source.indexOf('\nfunction mergeWorkspaceCoverage('));
  const lookup = source.slice(source.indexOf('let workspaceTimelineLookup='), source.indexOf('\nupdateTimelinePlayhead='));
  const record = { startSeconds: 0, duration: 45 };
  const context = { timelineSeconds: item => item.startSeconds, workspaceTimelineRecords: [record], workspaceTimelineDate: '2026-11-17', workspaceTimelineSegmentSeconds: 30 };
  vm.createContext(context);
  vm.runInContext(duration + '\n' + lookup, context);
  assert.equal(context.timelineRecordAt(40), record, 'a longer recording remains selectable past the configured segment length');
  assert.equal(context.timelineRecordAt(46), undefined, 'time beyond the actual recording remains a gap');
  const overlap = [{ startSeconds: 0, duration: 50 }, { startSeconds: 10, duration: 5 }, { startSeconds: 60, duration: 5 }];
  context.workspaceTimelineRecords = overlap;
  assert.equal(context.timelineRecordAt(12), overlap[0], 'overlaps retain the earliest matching recording');
  assert.equal(context.timelineRecordAt(55), undefined, 'gaps remain empty');
  assert.equal(context.timelineRecordAt(61), overlap[2]);
  const mixed = Array.from({length: 400}, (_, i) => ({startSeconds: i * 20, duration: i % 5 === 0 ? 70 : i % 3 === 0 ? 8 : 20}));
  context.workspaceTimelineRecords = mixed;
  for (let value = 0; value < 8000; value += 7) {
    const expected = mixed.find(item => value >= item.startSeconds && value < item.startSeconds + item.duration);
    assert.equal(context.timelineRecordAt(value), expected, `overlap and gap lookup at ${value}`);
  }
  let lookups = 0;
  context.timelineSeconds = item => { lookups++; return item.startSeconds; };
  context.workspaceTimelineRecords = Array.from({length: 2880}, (_, i) => ({startSeconds: i * 30, duration: 30}));
  context.timelineRecordAt(0);
  const initial = lookups;
  for (let i = 0; i < 500; i++) context.timelineRecordAt((i * 137) % 86400);
  assert.equal(lookups, initial, 'subsequent seeks reuse the recording index');
}

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
Promise.resolve().then(checkRecordedDuration).then(() => checkLiveEdge(key(new Date()))).then(() => checkLiveEdge(key(yesterday)))
  .catch(error => { console.error(error); process.exitCode = 1; });
