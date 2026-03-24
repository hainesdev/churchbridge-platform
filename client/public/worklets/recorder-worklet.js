// AudioWorklet processor — runs in the audio thread
// Streaming mode only: posts each chunk immediately for real-time forwarding to server

class RecorderProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._active = false;

    this.port.onmessage = (e) => {
      if (e.data === 'start') {
        this._active = true;
      } else if (e.data === 'stop') {
        this._active = false;
      }
    };
  }

  process(inputs) {
    if (!this._active) return true;
    const input = inputs[0];
    if (input && input[0]) {
      this.port.postMessage({
        type: 'chunk',
        samples: new Float32Array(input[0]),
        sampleRate: sampleRate,
      });
    }
    return true;
  }
}

registerProcessor('recorder-processor', RecorderProcessor);
