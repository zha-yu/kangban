// Continuous resampling from device rate to 16 kHz, packed into 60 ms PCM16 frames.
class Capture extends AudioWorkletProcessor {
  constructor() { super(); this.phase=0; this.sum=0; this.count=0; this.out=new Int16Array(960); this.index=0; }
  process(inputs) {
    const input=inputs[0]?.[0];
    if(input) for(const value of input) {
      this.sum+=value; this.count++; this.phase+=16000;
      if(this.phase>=sampleRate) {
        this.phase-=sampleRate;
        this.out[this.index++]=Math.round(Math.max(-1,Math.min(1,this.sum/this.count))*32767);
        this.sum=0; this.count=0;
        if(this.index===960) {this.port.postMessage(this.out.buffer,[this.out.buffer]);this.out=new Int16Array(960);this.index=0;}
      }
    }
    return true;
  }
}
registerProcessor('capture',Capture);
