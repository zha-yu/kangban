const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict'),path=require('node:path');
const elements=new Map();
const element=()=>({textContent:'',value:'.55',classList:{toggle(){},add(){},remove(){}},disabled:false});
const sandbox={
  document:{getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);}},
  window:{addEventListener(){}},navigator:{},location:{protocol:'http:',host:'127.0.0.1:8765'},
  fetch:async()=>({ok:true,json:async()=>({enrolled:true,name:'test'})}),
  setTimeout:()=>1,clearTimeout(){},Int16Array,Float32Array,JSON,console,Date,Error,
  WebSocket:{CONNECTING:0,OPEN:1,CLOSING:2,CLOSED:3},Blob:class {},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/asr/web/app.js'),'utf8'),sandbox);
vm.runInContext(`
  loaded=true;mode='conversation';processingUtterance={};
  var n=0;
  detector={feed(){n++;if(n===31)beginUtterance();if(n===35)endUtterance();},free(){}};
  for(let i=1;i<=35;i++)captureConversation(new Int16Array(960).fill(i).buffer);
`,sandbox);
const frames=vm.runInContext('utteranceQueue[0].frames.map(x=>new Int16Array(x)[0])',sandbox);
assert.deepEqual(Array.from(frames),Array.from({length:14},(_,i)=>i+22));
assert.equal(vm.runInContext('utteranceQueue[0].ended',sandbox),true);
assert.equal(vm.runInContext('currentUtterance',sandbox),null);

vm.runInContext(`token='opaque';tokenExpiresAt=Date.now()/1000+60;`,sandbox);
assert.equal(vm.runInContext('tokenIsValid()',sandbox),true);
vm.runInContext('tokenExpiresAt=Date.now()/1000-1',sandbox);
assert.equal(vm.runInContext('tokenIsValid()',sandbox),false);
assert.equal(vm.runInContext('token',sandbox),null);
console.log('PASS: continuous VAD pre-roll, ending frame, and browser token expiry');
