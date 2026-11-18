const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'cctv', 'dashboard.html'), 'utf8');
const hidden = source.slice(source.indexOf('function isHiddenLiveGrid('), source.indexOf('\nfunction attachLiveRecovery('));
const preview = source.slice(source.indexOf('window.loadCameraPreview=async function(id){'), source.indexOf('\nwindow.reloadCameraPreview='));
let active = false, connections = 0, plays = 0;
const video = {dataset:{live:'cam-1'}, currentSrc:'', readyState:0, error:null,
  classList:{add(){}}, closest(){return null}, matches(s){return s==='[data-camera-preview]'},
  play(){plays++;return Promise.resolve()}};
const document = {querySelector(selector){if(selector==='#view-settings.active')return active?{}:null;if(selector.includes('data-camera-preview'))return video;return null}};
const context = {document,window:{},CSS:{escape:x=>x},Date,clearTimeout(){},
  attachLiveRecovery(){return {retry:1}}, setLiveState(){},connectLive(){connections++}};
vm.createContext(context);vm.runInContext(hidden+'\n'+preview,context);
(async()=>{
  await context.window.loadCameraPreview('cam-1');
  assert.equal(connections,0,'do not connect while camera settings is hidden');
  active=true;
  await context.window.loadCameraPreview('cam-1');
  assert.equal(connections,1,'start the preview when settings is shown');
  video.currentSrc='http://camera/stream';video.readyState=3;
  await context.window.loadCameraPreview('cam-1');
  assert.equal(plays,1,'resume a loaded preview without starting another connection');
  assert.equal(connections,1);
  console.log('Camera settings preview checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
