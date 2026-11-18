const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(require('node:path').join(__dirname,'..','cctv','dashboard.html'),'utf8');
const source=html.slice(html.indexOf('const liveRtcReaders='),html.indexOf('\nasync function connectLive('));
const readers=[];let disconnects=0, playCalls=0;
class Reader{constructor(options){this.options=options;this.closed=false;readers.push(this)} close(){this.closed=true}}
const video={srcObject:null,pause(){},removeAttribute(){},addEventListener(name,fn){if(name==='playing')this.onplaying=fn},play(){playCalls++;return Promise.resolve()}};
const context={window:{MediaMTXWebRTCReader:Reader},RTCPeerConnection:function(){},URL,document:{},
  browserReachableStreamUrl:x=>x,isHiddenLiveGrid:()=>false,setTimeout,clearTimeout};
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
 const first=context.startLiveRtc(video,'http://server:8889/cam/whep',()=>true,()=>disconnects++);
 await Promise.resolve();await Promise.resolve();
 readers[0].options.onTrack({track:{kind:'video'},streams:[{id:1}]});readers[0].options.onTrack({track:{kind:'audio'},streams:[{id:1}]});assert.equal(playCalls,1,'a second track must not interrupt playback');video.onplaying();assert.equal(await first,true);assert.equal(video.srcObject.id,1);
 const second=context.startLiveRtc(video,'http://server:8889/cam/whep',()=>true,()=>disconnects++);
 await Promise.resolve();await Promise.resolve();
 assert.equal(readers[0].closed,true);
 readers[0].options.onTrack({streams:[{id:'stale'}]});assert.equal(readers[1].closed,false,'late old frames must not close the new connection');
 readers[1].options.onTrack({streams:[{id:2}]});video.onplaying();assert.equal(await second,true);assert.equal(video.srcObject.id,2);
 context.closeLiveRtc(video);assert.equal(readers[1].closed,true);assert.equal(video.srcObject,null);
 const failed=context.startLiveRtc(video,'http://server:8889/cam/whep',()=>true,()=>disconnects++);
 await Promise.resolve();await Promise.resolve();readers[2].options.onError('unavailable');assert.equal(await failed,false,'failed WebRTC falls back to HLS');
 assert.equal(disconnects,0);console.log('Live WebRTC lifecycle checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
