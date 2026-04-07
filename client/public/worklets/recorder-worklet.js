// AudioWorklet processor — runs in the audio thread.
//
// Downsamples the native AudioContext rate (typically 48 kHz, sometimes 44.1 kHz)
// to TARGET_RATE (16 kHz) before posting chunks to the main thread.
// This gives a 3× reduction in WebSocket payload and eliminates the server-side
// linear-interpolation resampling step entirely.
//
// Algorithm: step-accumulator nearest-neighbor.
//   For each input sample, advance the accumulator by 1. When it crosses the
//   step threshold (nativeRate / targetRate), emit that sample and subtract the
//   step. This handles both integer ratios (48 kHz → 16 kHz: step = 3.0) and
//   non-integer ratios (44.1 kHz → 16 kHz: step ≈ 2.756) correctly.
//
// Quality note: nearest-neighbor has no anti-aliasing filter. For speech (energy
// concentrated below 8 kHz), aliasing into the 0–8 kHz band is negligible.
// A sinc/polyphase filter can be added here if audio artefacts are ever observed.

const TARGET_RATE = 16000;

class RecorderProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._active = false;
    // step = how many input samples correspond to one output sample
    this._step = sampleRate / TARGET_RATE;
    // fractional accumulator — persists across process() calls
    this._accumulator = 0;

    this.port.onmessage = (e) => {
      if (e.data === 'start') {
        this._active = true;
      } else if (e.data === 'stop') {
        this._active = false;
        this._accumulator = 0; // reset phase on stop so next session starts clean
      }
    };
  }

  process(inputs) {
    if (!this._active) return true;
    const input = inputs[0];
    if (!input || !input[0]) return true;

    const inputSamples = input[0]; // Float32Array, 128 samples per quantum
    const downsampled = [];

    for (let i = 0; i < inputSamples.length; i++) {
      this._accumulator += 1;
      if (this._accumulator >= this._step) {
        this._accumulator -= this._step;
        downsampled.push(inputSamples[i]);
      }
    }

    if (downsampled.length > 0) {
      this.port.postMessage({
        type: 'chunk',
        samples: new Float32Array(downsampled),
        sampleRate: TARGET_RATE,
      });
    }

    return true;
  }
}

registerProcessor('recorder-processor', RecorderProcessor);
