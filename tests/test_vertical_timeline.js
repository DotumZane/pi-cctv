const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function checkWheelSensitivity() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'dashboard.html'), 'utf8');
  const wheel = source.slice(source.indexOf('function timelineWheelPixels('), source.indexOf('\nfunction verticalTimelineMinimumScroll('));
  const context = {}; vm.createContext(context); vm.runInContext(wheel, context);
  assert.equal(context.timelineWheelPixels({deltaY: 100, deltaMode: 0}), 1, 'a Chrome wheel notch advances 24 seconds on a normal day');
  assert.equal(context.timelineWheelPixels({deltaY: -120, deltaMode: 0}), -1, 'reverse mouse scrolling uses the same sensitivity');
  assert.equal(context.timelineWheelPixels({deltaY: 3, deltaMode: 1}), 1, 'line-mode wheels are normalized');
  assert.equal(context.timelineWheelPixels({deltaY: 1, deltaMode: 1}), 1 / 3, 'small line deltas retain fractional notches');
  assert.equal(context.timelineWheelPixels({deltaY: 4.5, deltaMode: 0}), 4.5, 'trackpad pixel scrolling stays unchanged');
  assert.equal(context.timelineWheelPixels({deltaY: 200, deltaMode: 0}), 2, 'multiple wheel notches accumulate');
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
  if (initialDate === today) {
    assert.ok(scroll.scrollTop > 0, 'future times are clamped');
    scroll.scrollTop += 1;
    // Let the programmatic return-to-live scroll settle before a wheel notch.
    while (callbacks.length) callbacks.shift()();
    scrollHandler({ stopImmediatePropagation() {} });
    assert.equal(previews, 2, 'one wheel notch away from live previews the past');
  }
}

async function checkHistoryRetry() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'dashboard.html'), 'utf8');
  const paging = source.slice(source.indexOf('let verticalTimelineDayPaging='), source.indexOf('\nconst verticalTimelineDayStyle='));
  const layers = new Map(), scroll = {scrollTop: 100, dataset: {continuousDaysReady: 'true'}};
  const content = {style: {}, querySelector(selector) { return layers.get(selector.match(/"([^"]+)"/)?.[1]) || null; }, appendChild() {}};
  const document = {querySelector(selector) {
    if (selector === '.vertical-timeline-scroll') return scroll;
    if (selector === '.vertical-timeline-content') return content;
    const date = selector.match(/data-timeline-day="([^"]+)"/)?.[1];
    return date ? layers.get(date) : null;
  }, createElement() {return {style: {}, dataset: {}, appendChild() {}, querySelectorAll() {return [];}};}};
  let calls = 0;
  const globals = {document, window: {workspaceCamera: 'cam'}, workspaceTimelineDate: '2026-11-22',
    workspaceTimelineRecords: [], workspaceTimelineLastValue: 0, workspaceTimelineSegmentSeconds: 30,
    verticalTimelineHeight: 3600, buildVerticalTimeline() {}, timelineDayLength: () => 86400,
    workspaceDayStart: day => new Date(`${day}T00:00:00Z`),
    workspaceNextDate: day => new Date(new Date(`${day}T00:00:00Z`).getTime() + 86400000),
    localDateKey: date => date.slice(0, 10), formatTimelineTime: () => '00:00:00',
    api: async () => ++calls === 1 ? {ok: false} : {ok: true, json: async () => ({recordings: [
      {start: '2026-11-21T12:00:00Z', duration: 30}]})},
    setTimeout() {return 0;}, clearTimeout, requestAnimationFrame() {}, console};
  vm.createContext(globals); vm.runInContext(paging, globals);
  vm.runInContext(`verticalTimelineLoadedDays=[{date:'2026-11-22',records:[]}];
    appendVerticalTimelineDay=(date)=>{document._layers.set(date,{dataset:{loading:'true'},querySelectorAll:()=>[],appendChild(){}})};`,
    Object.assign(globals, {document: Object.assign(document, {_layers: layers})}));
  await globals.window.loadPreviousTimelineDay();
  assert.equal(vm.runInContext('verticalTimelineLoadedDays.length', globals), 3, 'two days remain reserved after a failed request');
  assert.equal(vm.runInContext('verticalTimelineLoadedDays[1].failed', globals), true);
  await globals.window.loadPreviousTimelineDay();
  assert.equal(vm.runInContext('verticalTimelineLoadedDays.length', globals), 3, 'retry reuses the failed days');
  assert.equal(vm.runInContext('verticalTimelineLoadedDays[1].records.length', globals), 1, 'retry restores the older recordings');
  assert.equal(layers.get('2026-11-21').dataset.loading, 'false');
  vm.runInContext('buildVerticalTimeline()', globals);
  assert.equal(vm.runInContext('verticalTimelineLoadedDays.length', globals), 3, 'refresh preserves already loaded days');
  assert.equal(vm.runInContext('verticalTimelineLoadedDays[1].records.length', globals), 1, 'refresh keeps older recording metadata');
}

function checkZoom() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'cctv', 'dashboard.html'), 'utf8');
  const minimum = source.slice(source.indexOf('function verticalTimelineMinimumScroll('), source.indexOf('\nfunction verticalTimelineSecondsFromScroll('));
  const paging = source.slice(source.indexOf('let verticalTimelineDayPaging='), source.indexOf('\nconst verticalTimelineDayStyle='));
  const element = (className = '') => {
    const node = { className, style: {}, dataset: {}, children: [], textContent: '',
      appendChild(child) { if (child.fragment) node.children.push(...child.children); else node.children.push(child); return child; },
      setAttribute() {}, addEventListener() {},
      querySelector(selector) {
        const day = selector.match(/data-timeline-day="([^"]+)"/)?.[1];
        return node.children.find(child => (day ? child.dataset.timelineDay === day : selector === `.${child.className}`)) || null;
      },
      get childElementCount() { return node.children.length; } };
    Object.defineProperty(node, 'textContent', { get() { return ''; }, set() { node.children = []; } });
    return node;
  };
  const content = element('vertical-timeline-content'), help = { textContent: '' };
  const scroll = { scrollTop: 0, clientHeight: 600, dataset: {}, get scrollHeight() { return parseFloat(content.style.height) || 3600; } };
  const document = {
    querySelector(selector) {
      if (selector === '.vertical-timeline-scroll') return scroll;
      if (selector === '.vertical-timeline-content') return content;
      if (selector === '.vertical-timeline-label-band') return content.querySelector(selector);
      if (selector === '.vertical-timeline-help') return help;
      if (selector === '#workspace-live') return { dataset: { playback: 'recording' } };
      return null;
    },
    querySelectorAll() { return []; },
    createElement() { return element(); },
    createDocumentFragment() { const fragment = element(); fragment.fragment = true; return fragment; },
  };
  const date = '2026-11-20', frames = [];
  const globals = { document, window: {}, workspaceTimelineDate: date, workspaceTimelineRecords: [], workspaceTimelineLastValue: 0,
    workspaceTimelineSegmentSeconds: 30, verticalTimelineHeight: 3600, verticalTimelineProgrammatic: false,
    localDateKey: () => '2026-11-25', workspaceDayStart: day => new Date(`${day}T00:00:00`), timelineDayLength: () => 86400,
    formatTimelineTime: value => new Date(Math.floor(value) * 1000).toISOString().slice(11, 19),
    buildVerticalTimeline() {}, requestAnimationFrame(fn) { frames.push(fn); return frames.length; }, console, setTimeout, clearTimeout };
  vm.createContext(globals);
  vm.runInContext(minimum + '\n' + paging + '\nverticalTimelineLoadedDays=[{date:"2026-11-21",records:[]},{date:"2026-11-20",records:[{start:new Date("2026-11-20T09:15:00").toISOString(),duration:30}],loaded:true}];', globals);
  const at = seconds => { scroll.scrollTop = (2 - seconds / 86400) * 3600 - 26; };
  at(9 * 3600 + 15 * 60 + 10);
  const before = vm.runInContext('verticalTimelineSelection()', globals);
  assert.equal(vm.runInContext('setVerticalTimelineZoom("1h")', globals), true);
  assert.equal(globals.verticalTimelineHeight, 14400);
  const after = vm.runInContext('verticalTimelineSelection()', globals);
  assert.equal(after.date, before.date, 'zoom keeps the selected day');
  assert.ok(Math.abs(after.seconds - before.seconds) < 0.5, `zoom keeps the selected time (${before.seconds} -> ${after.seconds})`);
  assert.equal(globals.verticalTimelineProgrammatic, true, 'the zoom reposition does not count as a user scroll');
  frames.splice(0).forEach(fn => fn());
  assert.equal(globals.verticalTimelineProgrammatic, false);
  assert.equal(vm.runInContext('verticalTimelineLabelStep()', globals), 300, '1h zoom labels every five minutes');
  assert.match(help.textContent, /6 s per wheel notch/);
  assert.equal(vm.runInContext('setVerticalTimelineZoom("1m")', globals), true);
  const fine = vm.runInContext('verticalTimelineSelection()', globals);
  assert.ok(Math.abs(fine.seconds - before.seconds) < 0.5, 'the detailed zoom keeps the selected time');
  const band = content.querySelector('.vertical-timeline-label-band');
  assert.ok(band.children.length > 0 && band.children.length < 400, `detailed labels are drawn around the viewport only (${band.children.length})`);
  assert.equal(vm.runInContext('verticalTimelineLabelStep()', globals), 5);
  assert.ok(content.children.filter(child => child.dataset.timelineDay).every(layer => layer.children.length < 10), 'day layers skip per-day labels at detailed zoom');
  vm.runInContext('verticalTimelineLoadedDays=Array.from({length:30},(_, i)=>({date:`2026-10-${String(30-i).padStart(2,"0")}`,records:[]}))', globals);
  globals.verticalTimelineHeight = 3600;
  scroll.scrollTop = 25 * 3600;
  assert.equal(vm.runInContext('setVerticalTimelineZoom("1m")', globals), false, 'zooms that exceed the browser height limit are refused');
  assert.equal(globals.verticalTimelineHeight, 3600);
  assert.match(help.textContent, /not available this far back/);
}

const yesterday = new Date(); yesterday.setDate(yesterday.getDate() - 1);
const key = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
Promise.resolve().then(checkWheelSensitivity).then(checkRecordedDuration).then(() => checkLiveEdge(key(new Date()))).then(() => checkLiveEdge(key(yesterday))).then(checkHistoryRetry).then(checkZoom)
  .catch(error => { console.error(error); process.exitCode = 1; });
