const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'cctv', 'dashboard.html'), 'utf8');
const relay = fs.readFileSync(path.join(root, 'mediamtx.yml'), 'utf8');
assert.match(relay, /^webrtcEncryption:\s*false\s*$/m, 'WebRTC player must accept the dashboard HTTP URL');
assert.match(relay, /^hlsEncryption:\s*true\s*$/m, 'HLS remains encrypted');
assert.match(fs.readFileSync(path.join(root, 'docker-compose.yml'), 'utf8'), /^\s+MTX_WEBRTCENCRYPTION:\s*"false"\s*$/m, 'Compose recreates the relay with matching HTTP signaling');
const get = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
let destination = '';
const context = {URL,location:{href:'http://192.168.10.10:8080/',hostname:'192.168.10.10',protocol:'http:'},
  window:{open:()=>({location:{replace:url=>destination=url},close(){}})},alert:message=>{throw Error(message)},
  api:async()=>({json:async()=>({webrtc_url:'http://192.168.10.10:8889/cctv-cam-01/whep'})})};
vm.createContext(context);
vm.runInContext(get('function browserReachableStreamUrl(', '\nfunction browserHlsUrl(')+'\n'+get('function openCameraFocusPreview(', '\nasync function saveSettings('), context);
context.openCameraFocusPreview('cctv-cam-01');
setImmediate(()=>{assert.equal(destination,'http://192.168.10.10:8889/cctv-cam-01/');console.log('Focus view URL checks passed')});
