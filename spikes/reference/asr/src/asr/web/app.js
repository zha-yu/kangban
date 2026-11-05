const $ = id => document.getElementById(id);
let enrolled=false, loaded=false, mode='idle', media=null, context=null, worklet=null, detector=null;
let enrollmentSocket=null, enrollmentTimer=null, token=null, tokenExpiresAt=0, tokenTimer=null;
let preRoll=[], currentUtterance=null, utteranceQueue=[], processingUtterance=null, serial=0;
let finalText='', liveText='', audioClips=[];
let vuLevel=0, vuTimer=null;
const waveCanvas=()=>$('wave'); const waveCtx=()=>waveCanvas().getContext('2d');
function skipOn(){return $('skipVerify').checked;}
function vadThresh(){return Number($('vadThreshold').value);}
function render(){$('final').textContent=finalText||'你的话会出现在这里…';$('final').classList.toggle('empty',!finalText);$('live').textContent=liveText;}
function state(title,detail,badge){$('state').textContent=title;$('detail').textContent=detail;$('badge').textContent=badge||title;}
function controls(){const busy=mode!=='idle';$('enroll').disabled=busy||!loaded;$('skipVerify').disabled=busy;$('vadThreshold').disabled=busy;$('wake').disabled=busy||!loaded||(!enrolled&&!skipOn());$('name').disabled=busy;$('threshold').disabled=busy;$('stop').disabled=mode!=='conversation';$('session').classList.toggle('active',mode==='conversation');}
function showError(value){$('error').textContent=value.message||String(value);$('error').hidden=false;}
async function refresh(){const r=await fetch('/api/status');if(!r.ok)throw Error('无法读取本地服务状态');const s=await r.json();enrolled=s.enrolled;$('profile').textContent=s.enrolled?'已录入 · '+s.name:'尚未录入声纹';controls();}

function drawWave(samples){
  if(!samples||!samples.length)return;
  const c=waveCanvas();if(!c)return;
  if(c.width!==c.clientWidth){const dpr=window.devicePixelRatio||1;c.width=c.clientWidth*dpr;c.height=60*dpr;}
  const ctx=waveCtx(), w=c.width, h=c.height;
  ctx.fillStyle='#0d2a23';ctx.fillRect(0,0,w,h);
  ctx.strokeStyle='#4ea384';ctx.lineWidth=2;
  ctx.beginPath();
  // 归一化到 -1~1 再画波形
  let max=0;for(const v of samples){const a=Math.abs(v);if(a>max)max=a;}
  max=max||1;
  const step=samples.length/w;
  for(let x=0;x<w;x++){const i=Math.floor(x*step);const v=(samples[i]||0)/max;const y=(1-v)/2*h;if(x===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);}
  ctx.stroke();
}
function updateVu(samples){
  if(!samples||!samples.length)return;
  let peak=0;for(const v of samples){const a=Math.abs(v);if(a>peak)peak=a;}
  vuLevel=peak; // 原始峰值
  // Int16Array 峰值上限 32768，算成 0~1
  const normPeak=Math.min(1,peak/32768);
  const pct=Math.min(100,Math.round(normPeak*200));
  $('vuFill').style.width=pct+'%';
  const db=normPeak<0.0001?-60:Math.round(20*Math.log10(normPeak));
  $('vuDb').textContent=db+' dB';
}

async function openMicrophone(onAudio, deviceId){
  if(!navigator.mediaDevices?.getUserMedia)throw Error('请在 localhost 或 127.0.0.1 地址打开页面');
  const base={channelCount:1,echoCancellation:true,noiseSuppression:true};
  let audio=deviceId?{...base,deviceId:{exact:deviceId}}:base;
  try{
    media=await navigator.mediaDevices.getUserMedia({audio,video:false});
  }catch(e){
    if(deviceId){ // 指定设备可能已移除，回退到默认麦克风
      audio=base;
      media=await navigator.mediaDevices.getUserMedia({audio,video:false});
    } else throw e;
  }
  showInputInfo();
  context=new AudioContext();await context.resume();await context.audioWorklet.addModule('/audio-worklet.js');
  const source=context.createMediaStreamSource(media);worklet=new AudioWorkletNode(context,'capture');
  // AudioWorklet postMessage 发的是 ArrayBuffer（transferable），
  // 需要包成 Int16Array 才能遍历和使用。
  worklet.port.onmessage=e=>{
    if(e.data instanceof ArrayBuffer){onAudio(new Int16Array(e.data));}
    else{onAudio(e.data);}
  };
  source.connect(worklet);worklet.connect(context.destination);
  // 已授权，刷新设备列表（拿到可读的 label）
  populateInputDevices();
}
// 读取/列出音频输入设备
function getPreferredInput(){const v=localStorage.getItem('preferredAudioInput');return v||'';}
let devicesEnumerated=false;
async function ensurePermissionThenList(){
  // 先用一次临时授权拿到可识别的设备 label
  try{const tmp=await navigator.mediaDevices.getUserMedia({audio:true});tmp.getTracks().forEach(t=>t.stop());}catch(_){/* 拒绝也不影响，只是 label 为空 */}
  await populateInputDevices();devicesEnumerated=true;
}
async function populateInputDevices(){
  const sel=$('audioInput');if(!sel)return;
  let devices=[];
  try{devices=await navigator.mediaDevices.enumerateDevices();}catch(_){return;}
  const inputs=devices.filter(d=>d.kind==='audioinput');
  if(!inputs.length){sel.innerHTML='<option value="">无可用麦克风</option>';return;}
  // 未手动选择时，自动优先 AirPods（Mac Mini 无内置麦时它就是唯一可用设备）
  const preferred=getPreferredInput();
  const hasLabels=inputs.some(d=>d.label);
  const chosen=(preferred&&inputs.some(d=>d.deviceId===preferred))
    ? preferred
    : (inputs.find(d=>(d.label||'').toLowerCase().includes('airpods'))?.deviceId || inputs[0].deviceId);
  sel.innerHTML='';
  inputs.forEach((d,i)=>{const o=document.createElement('option');o.value=d.deviceId;o.textContent=d.label||`麦克风 ${i+1}`;sel.append(o);});
  sel.value=chosen;
  // 仅在已拿到设备 label（即已授权）时记住自动选择，避免授权前误存默认设备
  if(!preferred&&hasLabels)localStorage.setItem('preferredAudioInput',chosen);
}
function showInputInfo(){
  const track=media?.getAudioTracks?.()[0];
  const s=track?track.getSettings():{};
  if(s.deviceId){const sel=$('audioInput');if(sel&&sel.value!==s.deviceId)sel.value=s.deviceId;}
  const label=s.deviceLabel||'未知设备';
  const rate=s.sampleRate||context?.sampleRate||'未知';
  const el=$('inputInfo');if(el)el.textContent=`当前输入设备：${label} · ${rate} Hz · 通道 ${s.channelCount||1}`;
  console.log('[MIC] settings=',s);
}
async function closeMicrophone(){
  if(worklet){worklet.port.onmessage=null;worklet.disconnect();worklet=null;}
  if(media){media.getTracks().forEach(t=>t.stop());media=null;}
  if(detector){detector.free();detector=null;}
  if(context){const old=context;context=null;await old.close();}
  // 停止 VU 更新定时器
  if(vuTimer){clearInterval(vuTimer);vuTimer=null;}
  const c=waveCanvas();if(c){const ctx=waveCtx();ctx.fillStyle='#0d2a23';ctx.fillRect(0,0,c.width,c.height);}
  $('vuFill').style.width='0%';$('vuDb').textContent='-60 dB';
}
function tokenIsValid(){if(token&&Date.now()<tokenExpiresAt*1000)return true;token=null;tokenExpiresAt=0;clearTimeout(tokenTimer);tokenTimer=null;return false;}
function acceptToken(event){
  if(!event.token)return;token=event.token;tokenExpiresAt=Number(event.expires_at);clearTimeout(tokenTimer);
  const delay=Math.max(0,tokenExpiresAt*1000-Date.now());tokenTimer=setTimeout(()=>{if(!tokenIsValid()&&mode==='conversation'){$('score').textContent='授权已过期，下一句话将重新验证声纹';state('持续监听中','授权已过期；下一句话会自动重新验证','等待人声');}},Math.min(delay+20,2147483647));
}
async function revokeToken(){
  const value=token;token=null;tokenExpiresAt=0;clearTimeout(tokenTimer);tokenTimer=null;if(!value)return;
  try{await fetch('/api/session/end',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token:value}),keepalive:true});}catch(_){/* server copy expires shortly */}
}

function flushUtterance(u){
  if(!u.ready||u.closed)return;
  try{while(u.frames.length){if(u.ws.bufferedAmount>320000)throw Error('音频发送积压，请稍后重新说话');u.ws.send(u.frames.shift());}if(u.ended&&!u.stopSent){u.stopSent=true;u.ws.send(JSON.stringify({action:'stop'}));}}
  catch(e){showError(e);u.ws.close();}
}
function handleEvent(u,e){
  console.log('[ASR event]', e.type, e.mode||'', 'final='+e.is_final, JSON.stringify(e.text||'').slice(0,80));
  if(e.type==='ready'){u.ready=true;flushUtterance(u);}
  if(e.type==='verification'){
    acceptToken(e);
    if(e.accepted){
      if(e.skip_verify){$('score').textContent='声纹验证已跳过 · 直接进入识别';}
      else{const score=Number.isFinite(e.score)?`相似度 ${e.score.toFixed(3)} · `:'';$('score').textContent=score+'验证通过'+(e.cached?'（会话授权）':'');}
    }
    else{token=null;tokenExpiresAt=0;$('score').textContent='声纹验证未通过，下一句话将重新验证';}
  }
  if(e.type==='state'&&e.state==='verifying'){$('step1').classList.add('current');state('正在确认你的声音','首句话进行声纹验证','声纹验证');}
  if(e.type==='state'&&e.state==='recognizing'){$('step2').classList.add('current');state('正在识别','麦克风与端侧 VAD 仍在持续监听','实时识别');}
  if(e.type==='state'&&e.state==='insufficient_speech')$('score').textContent='语音不足，下一句话将重新验证';
  if(e.type==='transcript'){
    console.log('[ASR transcript] mode='+e.mode, 'text='+(e.text||''), 'is_final='+e.is_final);
    if(e.mode==='2pass-offline'){finalText+=e.text||'';liveText='';console.log('[ASR finalText now]=', finalText);}
    else if(e.mode==='2pass-online'||e.mode==='online'){liveText+=e.text||'';}
    render();
  }
  if(e.type==='audio'){
    try{
      const bin=atob(e.data);const bytes=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
      const blob=new Blob([bytes],{type:e.mime||'audio/wav'});const url=URL.createObjectURL(blob);
      const clips=$('clips');
      const row=document.createElement('div');row.className='clip';
      // 左侧：播放按钮 + 时长
      const playBtn=document.createElement('button');playBtn.className='play-btn';playBtn.textContent='▶';
      playBtn.title='播放';
      const audio=document.createElement('audio');audio.src=url;audio.preload='metadata';
      audio.onloadedmetadata=()=>{playBtn.textContent='▶ '+audio.duration.toFixed(1)+'s';};
      playBtn.onclick=()=>{
        if(audio.paused){audio.play();playBtn.textContent='⏸ '+audio.duration.toFixed(1)+'s';}
        else{audio.pause();audio.currentTime=0;playBtn.textContent='▶ '+audio.duration.toFixed(1)+'s';}
      };
      audio.onended=()=>{playBtn.textContent='▶ '+audio.duration.toFixed(1)+'s';};
      audio.onpause=()=>{if(!audio.ended)playBtn.textContent='▶ '+audio.duration.toFixed(1)+'s';};
      audio.onplay=()=>{playBtn.textContent='⏸ '+audio.duration.toFixed(1)+'s';};
      // 右侧：下载
      const a=document.createElement('a');a.href=url;a.download='utterance-'+(Date.now()%100000)+'.wav';a.textContent='下载 WAV';
      row.append(playBtn,audio,a);clips.prepend(row);
      audioClips.push({row,audio,url});
    }catch(err){console.warn('audio clip decode failed',err);}
  }
  if(e.type==='error')showError(Error(e.message));
  if(e.type==='done'){console.log('[ASR done] finalText=', finalText);u.done=true;u.ws.close();}
}
function startUpload(u){
  processingUtterance=u;const validToken=tokenIsValid()?token:null;
  const ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/audio`);u.ws=ws;
  const timer=setTimeout(()=>{if(ws.readyState===WebSocket.CONNECTING)ws.close();},10000);
  ws.onopen=()=>{clearTimeout(timer);ws.send(JSON.stringify({action:'listen',threshold:Number($('threshold').value),endpointing:'client',token:validToken,skip_verify:skipOn()}));};
  ws.onmessage=m=>handleEvent(u,JSON.parse(m.data));ws.onerror=()=>showError(Error('无法连接语音服务，请确认后端和 Docker 服务正在运行'));
  ws.onclose=()=>{clearTimeout(timer);u.closed=true;if(processingUtterance===u)processingUtterance=null;const i=utteranceQueue.indexOf(u);if(i>=0)utteranceQueue.splice(i,1);if(mode==='conversation'){if(u.done){$('step3').classList.add('current');const detail=skipOn()?'持续监听，每句话直接识别':tokenIsValid()?'会话授权有效，继续说话无需重复验证':'下一句话将验证声纹';state('持续监听中',detail,'等待人声');}pumpUploads();}};
}
function pumpUploads(){if(mode==='conversation'&&!processingUtterance&&utteranceQueue[0])startUpload(utteranceQueue[0]);}
function beginUtterance(){
  if(mode!=='conversation'||currentUtterance)return;
  currentUtterance={id:++serial,frames:preRoll.splice(0),ended:false,ready:false,stopSent:false,closed:false,done:false,ws:null};utteranceQueue.push(currentUtterance);
  if(utteranceQueue.length>4){showError(Error('连续语音过多，请稍等识别完成'));void endConversation();return;}
  const skipping=skipOn();const valid=tokenIsValid()||skipping;
  $('step1').classList.add('current');
  if(skipping){state('检测到人声','已跳过声纹验证，直接进入识别','识别中');}
  else{state(valid?'检测到人声':'正在确认你的声音',valid?'会话授权有效，直接进入识别':'这是本次会话的首次声纹验证',valid?'已授权':'声纹验证');}
  pumpUploads();
}
function endUtterance(){
  const u=currentUtterance;if(!u)return;
  u.ended=true;currentUtterance=null;flushUtterance(u);
  if(detector&&typeof detector.reset==='function')detector.reset();
  if(mode==='conversation')state('正在处理这句话','端侧 VAD 继续监听，可以继续说下一句','等待结果');
}
function captureConversation(data){
  if(mode!=='conversation'||!detector)return;
  // data 来自 AudioWorklet，应该是 Int16Array
  if(!data||typeof data[Symbol.iterator]!=='function'){
    console.warn('[captureConversation] 收到非可迭代数据，类型=',typeof data,data?.constructor?.name);
    return;
  }
  updateVu(data);drawWave(data);
  const active=currentUtterance;
  if(active){active.frames.push(data);flushUtterance(active);}else{preRoll.push(data);if(preRoll.length>10)preRoll.shift();}
  const pcm=new Int16Array(data);detector.feed(Float32Array.from(pcm,x=>x/32768));
}

async function startConversation(){
  if(mode!=='idle'||!loaded||(!enrolled&&!skipOn()))return;mode='conversation';token=null;tokenExpiresAt=0;preRoll=[];currentUtterance=null;utteranceQueue=[];processingUtterance=null;
  const skipping=skipOn();
  $('error').hidden=true;$('score').textContent=skipping?'已跳过声纹验证，直接开始识别':'首句话验证声纹，通过后获得临时会话授权';finalText='';liveText='';render();controls();state('正在启动持续监听','请允许浏览器使用麦克风','准备中');
  try{
    // threshold: 0.35 比原来的 0.5 更敏感，让 VAD 更容易触发
    detector=new BrowserTenVad(Module,{threshold:vadThresh(),silence:.8,minSpeech:.25,maxSpeech:30});
    detector.addEventListener('speechstart',beginUtterance);
    detector.addEventListener('speechend',endUtterance);
    await ensurePermissionThenList();
    await openMicrophone(captureConversation, getPreferredInput()||undefined);
    state('持续监听中',skipping?'已跳过声纹，请直接说话':'请说第一句话完成声纹验证','等待人声');
  }
  catch(e){showError(e);await endConversation();}
}
async function endConversation(){
  if(mode!=='conversation')return;mode='stopping';for(const u of utteranceQueue){if(u.ws&&u.ws.readyState<WebSocket.CLOSING)u.ws.close();}
  currentUtterance=null;utteranceQueue=[];processingUtterance=null;preRoll=[];await closeMicrophone();await revokeToken();mode='idle';controls();state('对话已结束','麦克风已关闭','已结束');$('score').textContent=skipOn()?'已跳过声纹验证':'再次唤醒后，第一句话会重新验证声纹';
}

async function startEnrollment(){
  if(mode!=='idle'||!loaded)return;const name=$('name').value.trim();if(!name){showError(Error('请先填写用户名称'));return;}
  mode='enroll';controls();$('error').hidden=true;state('正在连接','请允许浏览器使用麦克风','准备中');
  try{
    await ensurePermissionThenList();
    await openMicrophone(data=>{if(enrollmentSocket?.readyState===WebSocket.OPEN&&enrollmentSocket.captureReady)enrollmentSocket.send(data);}, getPreferredInput()||undefined);
    const ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/audio`);enrollmentSocket=ws;
    ws.onopen=()=>ws.send(JSON.stringify({action:'enroll',name,threshold:Number($('threshold').value),endpointing:'client'}));
    ws.onmessage=async m=>{const e=JSON.parse(m.data);if(e.type==='ready'){ws.captureReady=true;state('正在录入声音','请连续自然说话，6 秒后自动保存','声纹录入');enrollmentTimer=setTimeout(()=>{ws.send(JSON.stringify({action:'stop'}));ws.captureReady=false;void closeMicrophone();},6000);}if(e.type==='enrolled'){enrolled=true;state('已经记住你的声音','现在可以开始持续对话','录入成功');await refresh();}if(e.type==='error')showError(Error(e.message));if(e.type==='done')ws.close();};
    ws.onerror=()=>showError(Error('无法连接本地语音服务'));
    ws.onclose=async()=>{clearTimeout(enrollmentTimer);enrollmentTimer=null;await closeMicrophone();if(enrollmentSocket===ws)enrollmentSocket=null;mode='idle';controls();};
  }catch(e){showError(e);await closeMicrophone();mode='idle';controls();}
}

$('wake').textContent='唤醒并持续监听';
$('stop').textContent='结束对话';
$('vadThreshold').oninput=()=>{$('vadThreshValue').textContent=$('vadThreshold').value;};
$('threshold').oninput=()=>{$('thresholdValue').textContent=$('threshold').value;};
$('skipVerify').onchange=()=>{controls();$('skipHint').textContent=skipOn()?'已开启：跳过声纹验证，无需录入档案即可直接测试 ASR':'未开启：唤醒后第一句话会验证声纹身份';};
$('enroll').onclick=startEnrollment;
$('wake').onclick=()=>{['step1','step2','step3'].forEach(id=>$(id).classList.remove('current'));void startConversation();};
$('stop').onclick=()=>void endConversation();
$('clear').onclick=()=>{finalText='';liveText='';render();};
$('audioInput').addEventListener('focus', async ()=>{ if(devicesEnumerated)return; await ensurePermissionThenList(); });
navigator.mediaDevices.addEventListener?.('devicechange', ()=>populateInputDevices());
$('audioInput').addEventListener('change', e=>{
  const id=e.target.value;
  if(id)localStorage.setItem('preferredAudioInput', id); else localStorage.removeItem('preferredAudioInput');
  $('inputHint').textContent='已选设备将在下次唤醒 / 录入时生效';
});
refresh().catch(showError);
// index.html 预创建了带 onRuntimeInitialized 的 Module，
// Emscripten 初始化完会触发自定义事件 wasm-ready。
window.addEventListener('wasm-ready', ()=>{
  loaded=true;
  state('WASM 加载完成','可以开始录音和识别了','就绪');
  controls();
  populateInputDevices();
  console.log('[WASM] ready ✅ loaded=true');
});
window.addEventListener('wasm-failed', e=>{
  showError(Error('端侧 VAD 模型加载失败：'+e.detail));
});
controls();
window.addEventListener('pagehide',()=>{media?.getTracks().forEach(t=>t.stop());if(token)navigator.sendBeacon('/api/session/end',new Blob([JSON.stringify({token})],{type:'application/json'}));});
