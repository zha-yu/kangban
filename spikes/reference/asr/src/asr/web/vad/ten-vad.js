/** Small reusable adapter for the local sherpa-onnx WASM API.
 * Input: mono Float32Array at 16 kHz. Output: speechstart, speechend, segment.
 */
class BrowserTenVad extends EventTarget {
  constructor(module, {threshold=.5, silence=.5, minSpeech=.25, maxSpeech=20}={}) {
    super();
    this.detector=createVad(module, {
      sileroVad:{model:''},
      tenVad:{model:'ten-vad.onnx',threshold,minSilenceDuration:silence,
        minSpeechDuration:minSpeech,maxSpeechDuration:maxSpeech,windowSize:256},
      sampleRate:16000,numThreads:1,provider:'cpu',debug:0,bufferSizeInSeconds:30
    });
    if(!this.detector.handle) throw Error('TEN-VAD 初始化失败');
    this.pending=new Float32Array(256);this.used=0;this.active=false;this.total=0;
  }
  event(type, detail) {this.dispatchEvent(new CustomEvent(type,{detail}));}
  feed(samples) {
    if(!this.detector)throw Error('检测器已释放');
    for(const value of samples) {
      this.pending[this.used++]=value;
      if(this.used===256){this.window(this.pending);this.used=0;}
    }
  }
  window(samples) {
    this.detector.acceptWaveform(samples);this.total+=samples.length;
    const active=this.detector.isDetected();
    if(active!==this.active){this.active=active;this.event(active?'speechstart':'speechend',{detectedAt:this.total/16000});}
    this.drain();
  }
  drain() {
    while(!this.detector.isEmpty()) {
      const segment=this.detector.front();this.detector.pop();
      this.event('segment',{samples:segment.samples,start:segment.start/16000,
        end:(segment.start+segment.samples.length)/16000});
    }
  }
  flush() {
    if(this.used){this.pending.fill(0,this.used);this.window(this.pending);this.used=0;}
    this.detector.flush();this.drain();
    if(this.active){this.active=false;this.event('speechend',{detectedAt:this.total/16000,reason:'stop'});}
  }
  reset() {
    if(!this.detector)return;
    if(typeof this.detector.reset==='function')this.detector.reset();
    this.active=false;this.used=0;
  }
  free(){if(this.detector){this.detector.free();this.detector=null;}}
}

function vadWav(samples) {
  const buffer=new ArrayBuffer(44+samples.length*2),v=new DataView(buffer);
  function str(at,text){for(let i=0;i<text.length;i++)v.setUint8(at+i,text.charCodeAt(i));}
  str(0,'RIFF');v.setUint32(4,36+samples.length*2,true);str(8,'WAVE');str(12,'fmt ');
  v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
  v.setUint32(24,16000,true);v.setUint32(28,32000,true);v.setUint16(32,2,true);v.setUint16(34,16,true);
  str(36,'data');v.setUint32(40,samples.length*2,true);
  for(let i=0;i<samples.length;i++)v.setInt16(44+i*2,Math.round(Math.max(-1,Math.min(1,samples[i]))*32767),true);
  return new Blob([buffer],{type:'audio/wav'});
}
