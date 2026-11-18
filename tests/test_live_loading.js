const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'cctv', 'dashboard.html'), 'utf8');
const code = source.slice(source.indexOf('async function loadAll(){'), source.indexOf("\ndocument.querySelector('#assignments')?.addEventListener", source.indexOf('async function loadAll(){')));
let rendered = 0, releaseAdmin;
const stalled = new Promise(resolve => { releaseAdmin = resolve; });
const elements = new Map();
const context = {authUi:{user:{role:'admin'}},state:{devices:[]},liveSignature:'',Date,console,
  document:{querySelector(selector){if(selector==='#view-nvr.active'||selector.includes('data-routing-dirty'))return null;if(!elements.has(selector))elements.set(selector,{textContent:'',innerHTML:''});return elements.get(selector)}},
  api: path => path==='/api/health' ? Promise.resolve({ok:true,json:async()=>({devices:{online:1,offline:0,cameras:1,monitors:0}})}) : path==='/api/devices' ? Promise.resolve({ok:true,json:async()=>({devices:[{id:'cam',kind:'camera',status:'online'}]})}) : stalled,
  renderLive(){rendered++},renderPending(){},renderRouting(){},renderDevices(){},renderFleetUpdate(){},renderSettings(){},formatServerHealth(){return ''}};
vm.createContext(context);vm.runInContext(code,context);
(async()=>{
  const pending=context.loadAll();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(rendered,1,'camera cards render even if administrator requests are slow');
  assert.equal(context.state.devices[0].id,'cam');
  releaseAdmin({ok:false});
  assert.equal(await pending,false);
  assert.equal(rendered,1,'an unrelated request failure does not clear live cards');
  console.log('Live loading checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
