const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'cctv', 'dashboard.html'), 'utf8');
const code = source.slice(source.indexOf('const livePlayback=new WeakMap()'), source.indexOf('\nfunction reloadLive('));
let active = false, requests = 0, nextTimer = 0;
const timers = new Map(), listeners = {};
const video = {dataset:{live:'cam-1'}, isConnected:true, currentSrc:'', readyState:0, currentTime:0, error:null,
  closest(selector){if(selector==='#live-grid') return {}; return null},
  addEventListener(name, fn){listeners[name]=fn}, pause(){}, removeAttribute(){}, load(){},
  play(){return Promise.resolve()}};
const document = {head:{appendChild(){}}, createElement(){return {}},
  querySelector(selector){if(selector==='#view-live.active') return active ? {} : null; return null},
  querySelectorAll(){return [video]}};
const context = {document, state:{devices:[{id:'cam-1',status:'online'}]}, CSS:{escape:x=>x},
  location:{href:'http://example/',hostname:'example',protocol:'http:'}, URL, Date,
  api:async()=>{requests++;return {ok:true,json:async()=>({hls_url:'http://example/stream'})}},
  setTimeout(fn){const id=++nextTimer;timers.set(id,fn);return id},clearTimeout(id){timers.delete(id)}};
vm.createContext(context);
vm.runInContext(code, context);
(async()=>{
  await context.connectLive(video,'cam-1');
  assert.equal(requests,0,'a hidden grid must not start playback or accumulate retry backoff');
  active=true;
  context.resumeLiveGrid();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests,1,'opening the page starts an online camera automatically');
  assert.match(video.src,/\/api\/cameras\/cam-1\/hls\/index\.m3u8/, 'HLS remains available when WebRTC is unavailable');
  listeners.error();
  const before = timers.size;
  assert.ok(before>0,'a real error schedules recovery');
  listeners.playing();
  assert.equal(timers.size,before-1,'playback cancels an already scheduled retry');
  video.currentSrc='http://example/stream';video.readyState=3;
  context.resumeLiveGrid();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests,1,'returning to an available preview resumes it without reloading');
  console.log('Live grid recovery checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
