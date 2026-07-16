/**
 * Recorder Processor (AudioWorklet)
 * Converts Float32 to Int16 (PCM) on a separate worker thread.
 */
class RecorderProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // 480 samples at 24 kHz = 20 ms for broadcast-grade Realtime latency.
    this.bufferSize = 480;
    this.buffer = new Int16Array(this.bufferSize);
    this.bufferIndex = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input.length > 0) {
      const channelData = input[0];
      for (let i = 0; i < channelData.length; i++) {
        // Float32 to Int16
        const s = Math.max(-1, Math.min(1, channelData[i]));
        this.buffer[this.bufferIndex++] = s < 0 ? s * 0x8000 : s * 0x7FFF;

        if (this.bufferIndex >= this.bufferSize) {
          this.port.postMessage(this.buffer);
          this.buffer = new Int16Array(this.bufferSize);
          this.bufferIndex = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('recorder-processor', RecorderProcessor);
