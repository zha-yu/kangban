const $=id=>document.getElementById(id);
let loaded=false,busy=false,opening=false,vad=null,stream=null,context=null,worklet=null;
let urls=[],lines=[],count=0;
function log(message){lines.push(message);lines=lines.slice(-100);$('events').textContent=lines.join('\n');$('events').scrollTop=$('events').scrollHeight;}
function controls(){ $('start').disabled=!loaded||busy;$('sample').disabled=!loaded||busy;$('stop').disabled=!busy||opening;for(const id of ['threshold','silence','minSpeech'])$(id).disabled=busy;}
function display(text,active=false){$('status').textContent=text;$('indicator').classList.toggle('speaking',active);}
function fail(e){$('error').textContent=e.name==='NotAllowedError'?'麦克风权限被拒绝，请在浏览器中允许访问后重试。':(e.message||String(e));}
var Module={
 locateFile:path=>'/vad-assets/'+path,
 setStatus:status=>{if(!loaded&&status)$('detail').textContent='正在加载：'+status;},
 onAbort:reason=>{loaded=false;controls();display('模型加载失败');fail(Error(String(reason)));},
 onRuntimeInitialized:()=>{
  try{const test=new BrowserTenVad(Module);test.free();loaded=true;controls();display('模型就绪');$('detail').textContent='点击开始后才会请求麦克风；也可先用示例音频测试，无需麦克风权限。';}
  catch(e){display('初始化失败');fail(e);}
 }
};
function detector(){
 const threshold=Number($('threshold').value),silence=Number($('silence').value),minSpeech=Number($('minSpeech').value);
 if(!Number.isFinite(threshold)||threshold<.1||threshold>.9||!Number.isFinite(silence)||silence<.2||silence>2||!Number.isFinite(minSpeech)||minSpeech<.1||minSpeech>1)throw Error('请在设置范围内填写有效参数');
 const instance=new BrowserTenVad(Module,{threshold,silence,minSpeech});
 instance.addEventListener('speechstart',e=>{display('检测到人声',true);log(`人声开始 · 检测时刻 ${e.detail.detectedAt.toFixed(3)} s`);});
 instance.addEventListener('speechend',e=>{display('等待人声');log(`人声结束 · 检测时刻 ${e.detail.detectedAt.toFixed(3)} s${e.detail.reason?'（手动停止）':''}`);});
 instance.addEventListener('segment',e=>{
  const {samples,start,end}=e.detail;count++;$('count').textContent=count+' 段';
  log(`片段 ${count} · ${start.toFixed(3)}–${end.toFixed(3)} s · ${(end-start).toFixed(3)} s`);
  const url=URL.createObjectURL(vadWav(samples));urls.push(url);
  const article=document.createElement('article'),label=document.createElement('span'),audio=document.createElement('audio'),link=document.createElement('a');
  label.textContent=`#${count} · ${(end-start).toFixed(2)} 秒`;audio.controls=true;audio.src=url;
  link.href=url;link.download=`speech-${count}.wav`;link.textContent='下载 WAV';article.append(label,audio,link);$('segments').append(article);
  // Keep bounded browser memory during long-running demos.
  if(urls.length>30){URL.revokeObjectURL(urls.shift());$('segments').firstElementChild.remove();}
 });return instance;
}
async function cleanup(flush=true){
 if(worklet){worklet.port.onmessage=null;worklet.disconnect();worklet=null;}
 if(stream){stream.getTracks().forEach(t=>t.stop());stream=null;}
 if(context){const c=context;context=null;await c.close();}
 if(vad){try{if(flush)vad.flush();}finally{vad.free();vad=null;}}
 busy=false;opening=false;$('level').value=0;controls();
}
$('start').onclick=async()=>{
 if(busy)return;busy=true;opening=true;controls();$('error').textContent='';
 try{
  vad=detector();if(!navigator.mediaDevices?.getUserMedia)throw Error('请通过 localhost 或 HTTPS 打开页面');
  display('等待麦克风授权');stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true},video:false});
  context=new AudioContext();await context.resume();await context.audioWorklet.addModule('/audio-worklet.js');
  const source=context.createMediaStreamSource(stream);worklet=new AudioWorkletNode(context,'capture');
  worklet.port.onmessage=e=>{
   try{const pcm=new Int16Array(e.data),samples=Float32Array.from(pcm,x=>x/32768);let energy=0;for(const x of samples)energy+=x*x;$('level').value=Math.min(1,Math.sqrt(energy/samples.length)*5);vad.feed(samples);}
   catch(error){fail(error);cleanup(false);display('检测失败');}
  };
  source.connect(worklet);worklet.connect(context.destination);opening=false;controls();display('等待人声');$('detail').textContent=`设备采样率 ${context.sampleRate} Hz → 16000 Hz；原始音频不上传。`;log('麦克风检测开始 · 新会话时刻归零');
 }catch(e){fail(e);await cleanup(false);display('无法开始检测');}
};
$('stop').onclick=async()=>{try{await cleanup();display('已停止');log('检测停止 · 麦克风已释放');}catch(e){fail(e);}};
$('sample').onclick=async()=>{
 if(busy)return;busy=true;opening=true;controls();$('error').textContent='';display('正在检测示例');
 try{
  vad=detector();const response=await fetch('/vad/sample.wav');if(!response.ok)throw Error('示例音频读取失败');
  const decoder=new OfflineAudioContext(1,1,16000),audio=await decoder.decodeAudioData(await response.arrayBuffer());
  const samples=audio.getChannelData(0);log(`示例测试开始 · ${audio.sampleRate} Hz · ${audio.duration.toFixed(2)} 秒`);
  for(let i=0;i<samples.length;i+=960){vad.feed(samples.subarray(i,i+960));if(i%9600===0)await new Promise(resolve=>setTimeout(resolve,0));}
  // Include trailing silence to demonstrate normal speech-end detection.
  vad.feed(new Float32Array(16000*3));await cleanup();display('示例检测完成');log('示例测试完成 · 未使用麦克风或 ASR');
 }catch(e){fail(e);await cleanup(false);display('示例检测失败');}
};
$('clear').onclick=()=>{for(const url of urls)URL.revokeObjectURL(url);urls=[];count=0;lines=[];$('count').textContent='0 段';$('segments').replaceChildren();$('events').textContent='等待开始。';};
window.addEventListener('pagehide',()=>{stream?.getTracks().forEach(t=>t.stop());if(vad)vad.free();for(const url of urls)URL.revokeObjectURL(url);});
