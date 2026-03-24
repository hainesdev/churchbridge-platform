'use client';
import { useEffect, useRef } from 'react';

interface VUMeterProps {
  stream: MediaStream | null;
}

export function VUMeter({ stream }: VUMeterProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rafRef = useRef<number>(0);
  const ctxRef = useRef<AudioContext | null>(null);

  useEffect(() => {
    if (!stream || !canvasRef.current) return;

    const audioCtx = new AudioContext();
    ctxRef.current = audioCtx;
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    audioCtx.createMediaStreamSource(stream).connect(analyser);

    const data = new Uint8Array(analyser.frequencyBinCount);
    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d')!;

    const draw = () => {
      analyser.getByteTimeDomainData(data);

      // RMS level
      const rms = Math.sqrt(
        data.reduce((s, v) => s + ((v - 128) / 128) ** 2, 0) / data.length
      );
      const db = 20 * Math.log10(rms + 1e-10);
      const pct = Math.min(1, Math.max(0, (db + 60) / 60));

      const w = canvas.width;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);

      // Background
      ctx.fillStyle = '#1f2937';
      ctx.fillRect(0, 0, w, h);

      // Level bar
      const barW = Math.floor(pct * w);
      const color = db > -6 ? '#ef4444' : db > -18 ? '#eab308' : '#22c55e';
      ctx.fillStyle = color;
      ctx.fillRect(0, 0, barW, h);

      // dB markers at -18 and -6
      ctx.fillStyle = '#6b7280';
      ctx.fillRect(Math.floor(w * 0.7), 0, 1, h);   // -18dB
      ctx.fillRect(Math.floor(w * 0.9), 0, 1, h);   // -6dB

      rafRef.current = requestAnimationFrame(draw);
    };

    draw();

    return () => {
      cancelAnimationFrame(rafRef.current);
      audioCtx.close();
    };
  }, [stream]);

  return (
    <div className="w-full">
      <div className="flex justify-between text-xs text-gray-400 mb-1">
        <span>Input Level</span>
        <span className="text-gray-500">-18db &nbsp;&nbsp; -6db</span>
      </div>
      <canvas
        ref={canvasRef}
        width={400}
        height={20}
        className="w-full h-5 rounded"
      />
    </div>
  );
}
