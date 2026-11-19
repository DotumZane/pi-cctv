const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function checkWheelSensitivity() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'dashboard.html'), 'utf8');
  const wheel = source.slice(source.indexOf('function timelineWheelPixels('), source.indexOf('\nfunction verticalTimelineMinimumScroll('));
  const context = {}; vm.createContext(context); vm.runInContext(wheel, context);
  assert.equal(context.timelineWheelPixels({deltaY: 100, deltaMode: 0}), 12, 'a Chrome wheel notch advances about five minutes');
  assert.equal(context.timelineWheelPixels({deltaY: -120, deltaMode: 0}), -12, 'reverse mouse scrolling uses the same sensitivity');
  assert.equal(context.timelineWheelPixels({deltaY: 3, deltaMode: 1}), 12, 'line-mode wheels are normalized');
  assert.equal(context.timelineWheelPixels({deltaY: 4.5, deltaMode: 0}), 4.5, 'trackpad pixel scrolling stays unchanged');
  assert.equal(context.timelineWheelPixels({deltaY: 200, deltaMode: 0}), 24, 'multiple wheel notches accumulate');
}

function checkRecordedDuration() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'timeline-enhancements.js'), 'utf8');
  const engine = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'playback-engine.js'), 'utf8');
  const offset = engine.slice(engine.indexOf('  function recordingOffset('), engine.indexOf('\n  function setTarget('));
  const seek = {timelineSeconds: record => record.startSeconds}; vm.createContext(seek); vm.runInContext(offset, seek);
  assert.ok(seek.recordingOffset({startSeconds: 0, duration: 29.978333}, 29.99) < 29.978333, 'boundary seeks stay within the actual segment');

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
  const adjacent = [{startSeconds: 0, duration: 29.978333}, {startSeconds: 30, duration: 29.9518}, {startSeconds: 60, duration: 30}];
  context.workspaceTimelineRecords = adjacent;
  assert.equal(context.timelineRecordAt(29.99), adjacent[0], 'a 22ms boundary gap resolves to the preceding segment');
  assert.equal(context.timelineRecordAt(59.98), adjacent[1], 'a 48ms boundary gap remains browsable');
  assert.equal(context.timelineRecordAt(30), adjacent[1], 'the next segment starts at its exact timestamp');
  context.workspaceTimelineRecords = [{startSeconds: 0, duration: 29.8}, {startSeconds: 30, duration: 30}];
  assert.equal(context.timelineRecordAt(29.9), undefined, 'larger gaps must remain visible');
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
Promise.resolve().then(checkWheelSensitivity).then(checkRecordedDuration).then(() => checkLiveEdge(key(new Date()))).then(() => checkLiveEdge(key(yesterday)))
  .catch(error => { console.error(error); process.exitCode = 1; });
