import json
import logging
import time
import wave
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

AUDIO_SAMPLE_RATE = 16000  # PCM16 mono, matches Deepgram input


@dataclass
class CaptureResult:
    audio_path: str | None
    events_path: str | None
    duration_s: float
    segment_count: int  # number of stt_final events seen
    latency_ms: dict    # {"stt_to_sentence": {"p50":…, "p90":…, "count":…}, …}
    started_at: float
    ended_at: float


class SessionRecorder:
    """Side-car that attaches to a ServiceSession and captures:
    - PCM16 audio → WAV file (directly replayable by the benchmark)
    - All pipeline events → JSONL file
    - Per-stage wall-clock timing → latency percentiles

    This class is a pure side-effect — it never participates in pipeline control
    flow. All calls from ServiceSession are guarded by ``if self._recorder:``,
    so a crash here cannot disrupt a live service.
    """

    def __init__(self, session_id: int, church_id: str) -> None:
        self._session_id = session_id
        self._church_id = church_id
        self._started_at = time.time()
        self._audio_chunks: list[bytes] = []
        self._events_buf: list[str] = []
        # {segment_ts: {"stt": wall_ms, "sentence": wall_ms, "translation": wall_ms, "enrichment": wall_ms}}
        self._timing: dict[int, dict[str, int]] = {}
        self._segment_count = 0
        self._audio_path, self._events_path = self._setup_paths()

    def _setup_paths(self) -> tuple[Path, Path]:
        ts_str = str(int(self._started_at))
        audio_dir = Path("tests/audio/captured")
        events_dir = Path("logs/sessions")
        audio_dir.mkdir(parents=True, exist_ok=True)
        events_dir.mkdir(parents=True, exist_ok=True)
        return (
            audio_dir / f"{ts_str}_{self._church_id}_{self._session_id}.wav",
            events_dir / f"{self._session_id}.jsonl",
        )

    def record_audio(self, pcm16_bytes: bytes) -> None:
        """Accumulate resampled PCM16 bytes for WAV output."""
        self._audio_chunks.append(pcm16_bytes)

    def record_event(self, event_type: str, payload: dict) -> None:
        """Write one JSONL line with a monotonic timestamp."""
        entry = {"t": time.monotonic(), "type": event_type, **payload}
        self._events_buf.append(json.dumps(entry, ensure_ascii=False))

    def record_timing(self, stage: str, segment_ts: int) -> None:
        """Record wall-clock ms for a pipeline stage transition.

        stage: 'stt' | 'sentence' | 'translation' | 'enrichment'
        segment_ts: the shared integer key used throughout the pipeline (wall-clock ms)
        """
        rec = self._timing.setdefault(segment_ts, {})
        rec[stage] = int(time.time() * 1000)
        if stage == "stt":
            self._segment_count += 1

    def compute_latency(self) -> dict:
        """Compute p50/p90 latency in ms for each pipeline stage transition.

        Safe to call mid-session — uses whatever timing records exist so far.
        """
        stt_to_sent: list[int] = []
        sent_to_tran: list[int] = []
        tran_to_enr: list[int] = []

        for rec in self._timing.values():
            s = rec.get("stt")
            se = rec.get("sentence")
            t = rec.get("translation")
            e = rec.get("enrichment")
            if s and se:
                stt_to_sent.append(se - s)
            if se and t:
                sent_to_tran.append(t - se)
            if t and e:
                tran_to_enr.append(e - t)

        def pct(data: list[int]) -> dict:
            if not data:
                return {"p50": None, "p90": None, "count": 0}
            s = sorted(data)
            n = len(s)
            return {"p50": s[int(n * 0.5)], "p90": s[min(int(n * 0.9), n - 1)], "count": n}

        return {
            "stt_to_sentence": pct(stt_to_sent),
            "sentence_to_translation": pct(sent_to_tran),
            "translation_to_enrichment": pct(tran_to_enr),
        }

    def stop(self) -> CaptureResult:
        """Flush WAV + JSONL and return a summary."""
        ended_at = time.time()
        audio_path = self._flush_wav()
        events_path = self._flush_jsonl()
        return CaptureResult(
            audio_path=str(audio_path) if audio_path else None,
            events_path=str(events_path) if events_path else None,
            duration_s=ended_at - self._started_at,
            segment_count=self._segment_count,
            latency_ms=self.compute_latency(),
            started_at=self._started_at,
            ended_at=ended_at,
        )

    def _flush_wav(self) -> Path | None:
        if not self._audio_chunks:
            return None
        pcm = b"".join(self._audio_chunks)
        try:
            with wave.open(str(self._audio_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)   # 16-bit = 2 bytes
                wf.setframerate(AUDIO_SAMPLE_RATE)
                wf.writeframes(pcm)
            logger.info(
                "[recorder:%s] WAV written: %s (%.1fs, %d bytes)",
                self._session_id,
                self._audio_path,
                len(pcm) / (AUDIO_SAMPLE_RATE * 2),
                len(pcm),
            )
            return self._audio_path
        except Exception as e:
            logger.error("[recorder:%s] WAV write failed: %s", self._session_id, e)
            return None

    def _flush_jsonl(self) -> Path | None:
        if not self._events_buf:
            return None
        try:
            self._events_path.write_text(
                "\n".join(self._events_buf) + "\n", encoding="utf-8"
            )
            logger.info(
                "[recorder:%s] JSONL written: %s (%d events)",
                self._session_id,
                self._events_path,
                len(self._events_buf),
            )
            return self._events_path
        except Exception as e:
            logger.error("[recorder:%s] JSONL write failed: %s", self._session_id, e)
            return None
