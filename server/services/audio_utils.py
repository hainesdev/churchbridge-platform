import numpy as np
import struct


def resample_float32_to_pcm16(
    data: bytes,
    src_rate: int,
    dst_rate: int = 16000,
) -> bytes:
    """Resample raw Float32 LE audio bytes to PCM16 LE at dst_rate.

    Browser captures Float32 at native rate (44.1/48kHz).
    Google Speech expects linear16 at 16kHz.
    Linear interpolation — no DSP library required.
    """
    samples = np.frombuffer(data, dtype=np.float32)
    if src_rate == dst_rate:
        pcm16 = (samples * 32767).clip(-32768, 32767).astype(np.int16)
        return pcm16.tobytes()

    ratio = dst_rate / src_rate
    new_len = max(1, int(len(samples) * ratio))
    indices = np.linspace(0, len(samples) - 1, new_len)
    resampled = np.interp(indices, np.arange(len(samples)), samples)
    pcm16 = (resampled * 32767).clip(-32768, 32767).astype(np.int16)
    return pcm16.tobytes()


def base64_to_float32_bytes(b64: str) -> bytes:
    """Decode base64-encoded Float32 LE bytes from browser audio worklet."""
    import base64
    return base64.b64decode(b64)
