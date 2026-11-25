// Downsample the browser input to mono PCM16 at 16 kHz and post 64 ms packets.
class RobotMic extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = 0;
    this.total = 0;
    this.count = 0;
    this.packet = new Int16Array(1024);
    this.index = 0;
  }

  process(inputs) {
    for (const value of inputs[0]?.[0] || []) {
      this.total += value;
      this.count++;
      this.phase += 16000;
      if (this.phase < sampleRate) continue;
      this.phase -= sampleRate;
      const sample = Math.max(-1, Math.min(1, this.total / this.count));
      this.packet[this.index++] = Math.round(sample * 32767);
      this.total = this.count = 0;
      if (this.index === this.packet.length) {
        this.port.postMessage(this.packet.buffer, [this.packet.buffer]);
        this.packet = new Int16Array(1024);
        this.index = 0;
      }
    }
    return true;
  }
}
registerProcessor('robot-mic', RobotMic);
