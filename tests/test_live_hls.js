const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname,'..','cctv','dashboard.html'),'utf8');
const code = html.slice(html.indexOf('const liveHlsPlayers='), html.indexOf('\nasync function connectLive('));
const created=[];let retries=0;
class Hls {constructor(){this.destroyed=false;created.push(this)} static isSupported(){return true} on(event,fn){this.handler=fn} loadSource(url){this.url=url} attachMedia(video){this.video=video} destroy(){this.destroyed=true}}
Hls.Events={ERROR:'error'};
const context={window:{Hls},closeLiveRtc(){},document:{},setTimeout,clearTimeout};
vm.createContext(context);vm.runInContext(code,context);
(async()=>{
 const video={canPlayType:()=>'',play:()=>Promise.resolve()};
 await context.playLiveHls(video,'/stream/index.m3u8',()=>true,()=>retries++);
 assert.equal(created.length,1);assert.equal(created[0].url,'/stream/index.m3u8');assert.equal(created[0].video,video);
 created[0].handler('error',{fatal:true});assert.equal(retries,1);assert.equal(created[0].destroyed,true);
 const native={canPlayType:()=> 'probably',load(){this.loaded=true},play:()=>Promise.resolve()};
 await context.playLiveHls(native,'/native.m3u8',()=>true,()=>retries++);
 assert.equal(native.src,'/native.m3u8');assert.ok(native.loaded);assert.equal(created.length,1);
 console.log('Cross-browser HLS checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
