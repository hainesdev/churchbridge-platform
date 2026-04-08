#!/usr/bin/env python3
"""
ChurchBridge AI — LLM Benchmark Interpreter
=============================================
Sends structured benchmark artifacts to Claude and returns a pattern analysis
with a specific, evidence-backed hypothesis and (when warranted) a targeted
code or directive proposal.

The LLM is an INTERPRETER only. The action recommendation is always determined
by the deterministic tier-priority rules in review.py. The LLM:

  - identifies patterns in the event log that metrics cannot capture
  - explains the most likely root cause with specific evidence
  - proposes ONE narrow code change when action is investigate/revert_or_fix
  - drafts ONE structured directive diff when action is propose_directive_update
  - explains why promotion is warranted when action is promote

It never overrides the action, never proposes broad refactors, and never
suggests directive changes without repeated multi-run evidence.

Output schema:
  {
    "pattern_analysis":      str,   -- what the event log reveals beyond the metrics
    "root_cause_hypothesis": str,   -- most likely explanation with named evidence
    "confidence":            str,   -- "high" | "medium" | "low"
    "evidence_quotes":       [str], -- verbatim excerpts from event log or scorecard
    "proposed_change": {           -- null if action is collect_more_runs
      "type":        str,  -- "code" | "directive" | "none"
      "target":      str,  -- file:function or DIRECTIVE.md section
      "description": str,  -- what to change and why
      "current_text": str | null,   -- for directive diffs: exact current wording
      "proposed_text": str | null,  -- for directive diffs: proposed wording
    } | null,
    "open_questions": [str],  -- what to watch for in the next run
  }

Usage:
    from tests.benchmark.llm_interpret import interpret_run
    analysis = interpret_run(scorecard, trajectory, result, review_md, cycle_history)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


# ── Event log sampling ─────────────────────────────────────────────────────────

def _sample_event_log(result: dict, max_events: int = 60) -> list[dict]:
    """
    Build a compact, readable event sample from the full run.
    Prioritises committed sentences, translations, corrections, and verse events.
    Fills remaining budget from the beginning and end of all_messages.
    """
    all_msgs = result.get("all_messages", [])

    priority_types = {
        "final_spanish", "translation", "translation_update",
        "correction", "verse_detected", "verse_range_update",
        "mode_change", "stt_final",
    }

    priority  = [m for m in all_msgs if m.get("type") in priority_types]
    remainder = [m for m in all_msgs if m.get("type") not in priority_types]

    budget      = max_events - len(priority)
    fill        = remainder[:max(0, budget // 2)] + remainder[-max(0, budget // 2):]
    combined    = priority + fill
    combined.sort(key=lambda m: m.get("_elapsed_s", 0))

    # Trim to max_events, keeping chronological order
    return combined[:max_events]


def _committed_pairs(result: dict) -> list[dict]:
    """
    Return a list of {spanish, english_google, english_llm, ts, elapsed_s}
    aligned by ts so each sentence shows both translation versions side by side.
    """
    all_msgs = result.get("all_messages", [])

    by_ts: dict[int, dict] = {}
    for m in all_msgs:
        ts = m.get("ts")
        if ts is None:
            continue
        entry = by_ts.setdefault(ts, {"ts": ts})

        if m["type"] == "final_spanish":
            entry["spanish"]   = m.get("text", "")
            entry["elapsed_s"] = m.get("_elapsed_s")
        elif m["type"] == "translation":
            entry["english_google"] = m.get("english", "")
            entry["translation_elapsed_s"] = m.get("_elapsed_s")
        elif m["type"] in ("translation_update", "correction"):
            entry["english_llm"] = m.get("english", "")
            entry["correction_elapsed_s"] = m.get("_elapsed_s")

    pairs = sorted(
        [v for v in by_ts.values() if "spanish" in v],
        key=lambda x: x.get("elapsed_s", 0),
    )
    return pairs


def _directive_summary() -> str:
    """Return a compact summary of DIRECTIVE.md for context injection."""
    directive_path = ROOT / "DIRECTIVE.md"
    if not directive_path.exists():
        return "(DIRECTIVE.md not found)"
    text = directive_path.read_text(encoding="utf-8")
    # Return first 3000 chars — mission + architecture overview
    return text[:3000]


def _quality_report_summary(result: dict, pipeline_dir: Path | None) -> str:
    """
    Load the translation quality report for this run (if it exists) and return
    a compact summary for injection into the interpretation prompt.

    Returns an empty string when no quality report is available — the prompt
    degrades gracefully rather than failing.
    """
    if pipeline_dir is None:
        return ""

    run_id = result.get("run_id", "")
    quality_path = pipeline_dir / "translation_quality" / f"{run_id}.json"
    if not quality_path.exists():
        return ""

    try:
        raw = quality_path.read_bytes()
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                report = json.loads(raw.decode(enc))
                break
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        else:
            return ""
    except Exception:
        return ""

    rating   = report.get("overall_quality_rating")
    conf     = report.get("confidence", "unknown")
    summary  = report.get("overall_summary", "")
    flagged  = report.get("flagged_pairs") or []
    chunks   = report.get("chunk_evaluations") or []
    recs     = report.get("recommendations") or []

    winners  = [c.get("llm_vs_google_winner") for c in chunks]
    llm_wins = winners.count("llm")
    goog_wins = winners.count("google")
    mixed    = winners.count("mixed")

    lines = [
        f"Overall quality rating: {rating}/5.0 (confidence: {conf})",
        f"LLM vs Google: {llm_wins} LLM wins / {goog_wins} Google wins / {mixed} mixed across {len(chunks)} chunks",
        f"Flagged pairs (high deviation): {len(flagged)}",
    ]
    if summary:
        lines.append(f"Summary: {summary}")

    high_severity = [p for p in flagged if p.get("severity") == "high"]
    if high_severity:
        lines.append(f"High-severity flagged pairs ({len(high_severity)}):")
        for p in high_severity[:3]:
            lines.append(
                f"  [{p['index']}] deviation={p.get('deviation_score', '?'):.2f} "
                f"risk={p.get('reconstruction_risk', False)}: {p.get('verdict', '')}"
            )

    if recs:
        lines.append("Quality recommendations:")
        for r in recs[:3]:
            lines.append(f"  - {r}")

    return "\n".join(lines)


# ── Prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(
    scorecard:      dict,
    trajectory:     dict,
    result:         dict,
    review_md:      str,
    cycle_history:  list[dict],
    action:         str,
    pipeline_dir:   Path | None = None,
) -> str:
    event_sample   = _sample_event_log(result)
    pairs          = _committed_pairs(result)
    directive_info = _directive_summary()
    quality_info   = _quality_report_summary(result, pipeline_dir)

    n_runs         = trajectory.get("n_runs", 0)
    audio_dir      = scorecard.get("audio_dir", "unknown")

    # Summarise relevant trajectory metrics (Tier 1 and Tier 2 only)
    traj_metrics = trajectory.get("metrics", {})
    key_metrics  = [
        "wer_committed_pct", "wer_raw_pct",
        "out_of_order_event_count", "orphan_correction_count",
        "fragment_leak_count", "duplicate_commit_count",
        "time_to_first_translation_s", "llm_correction_count",
        "verse_event_count", "mode_flip_count",
    ]
    traj_summary = []
    for k in key_metrics:
        entry = traj_metrics.get(k)
        if entry:
            current = entry.get("current")
            delta   = entry.get("delta_vs_prev")
            trend   = entry.get("trend", "unknown")
            traj_summary.append(
                f"  {k}: current={current}, delta_vs_prev={delta}, trend={trend}"
            )
    traj_block = "\n".join(traj_summary) if traj_summary else "  (no trajectory data)"

    # Recent cycle history (last 5 cycles)
    recent_cycles = cycle_history[-5:] if cycle_history else []
    cycle_block   = json.dumps(recent_cycles, indent=2) if recent_cycles else "  (no prior cycles)"

    # Committed sentence pairs
    pairs_lines = []
    for p in pairs:
        line = (
            f"  [{p.get('elapsed_s', '?')}s] ES: {p.get('spanish', '')}\n"
            f"    Google: {p.get('english_google', 'n/a')}\n"
            f"    LLM:    {p.get('english_llm', 'n/a')}"
        )
        pairs_lines.append(line)
    pairs_block = "\n".join(pairs_lines) if pairs_lines else "  (none)"

    # Event log sample
    event_lines = []
    for m in event_sample:
        t = m.get("type", "?")
        e = m.get("_elapsed_s", "?")
        preview = (
            m.get("text") or m.get("spanish") or m.get("english") or
            str({k: v for k, v in m.items() if k not in ("_elapsed_s",) and not k.startswith("_")})[:120]
        )
        event_lines.append(f"  [{e}s] {t}: {preview}")
    event_block = "\n".join(event_lines) if event_lines else "  (empty)"

    quality_block = (
        f"\nTRANSLATION QUALITY EVALUATION (this run):\n{quality_info}\n"
        if quality_info else
        "\nTRANSLATION QUALITY EVALUATION: (not yet run — use --translation-quality to enable)\n"
    )

    prompt = f"""You are a specialist benchmark evaluator for ChurchBridge AI.

ChurchBridge AI provides real-time Spanish → English caption translation for live
Pentecostal church services. The pipeline is:

  audio → Deepgram STT → SentenceBuffer (flush hierarchy) → Google Translate
  → LLM enrichment (Claude Haiku, fire-and-forget) → display WebSocket

The flush hierarchy priority order:
  1. Terminal punctuation (.?!…;)
  2. UtteranceEnd VAD event (with incomplete-tail soft guard)
  3. ABSOLUTE_MAX_WORDS = 60 (unconditional safety valve)
  4. Fallback timer (3.5s + discourse holds, up to 8× × 2.0s)

DIRECTIVE OVERVIEW (first 3000 chars):
{directive_info}

---

BENCHMARK SET: {audio_dir}  |  Runs in history: {n_runs}
ACTION (determined by deterministic review): {action}

DETERMINISTIC REVIEW:
{review_md}

---

SCORECARD (current run):
{json.dumps(scorecard, indent=2)}

TRAJECTORY (key metrics, last {n_runs} runs):
{traj_block}

---

COMMITTED SENTENCES WITH TRANSLATIONS (aligned by ts):
{pairs_block}

---
{quality_block}
---

EVENT LOG SAMPLE ({len(event_sample)} events):
{event_block}

---

RECENT CYCLE HISTORY (last 5 cycles):
{cycle_block}

---

YOUR TASK:

You are the INTERPRETER layer. The action above was already determined by
deterministic tier-priority rules. Do not override it.

Your job:
1. Identify patterns in the event log and committed sentence pairs that the
   numeric metrics cannot see.
2. Form a root cause hypothesis. Be specific — name the flush trigger, the
   component, or the event type involved.
3. Based on the action:

   - collect_more_runs: explain what to watch for in the next run. No fix.
   - promote: explain why the metrics support promotion. Confirm guardrails hold.
   - investigate: propose ONE specific, narrow code change. Name the exact
     file and function. Do not propose broad refactors.
   - revert_or_fix: identify the exact regression and propose ONE targeted fix.
   - propose_directive_update: draft ONE structured directive diff with the
     exact current wording and the proposed replacement.

CONSTRAINTS:
- Never propose changes unless you can cite specific evidence from the event
  log or committed sentence pairs above.
- Distinguish three cases explicitly when relevant: true pipeline regression,
  evaluator uncertainty/noise, and expected degradation on clipped or damaged
  stress audio.
- Do not treat stress-window weakness alone as proof that the clean-audio path
  regressed.
- For directive changes: only propose if the trajectory shows {n_runs} runs
  of consistent evidence. If evidence is thin, say so and lower confidence.
- For code changes: name file:function, not a general area.
- Keep proposed_change.description under 200 words.

Respond ONLY with a JSON object matching this schema exactly:
{{
  "pattern_analysis":      "<what the event log reveals beyond the metrics>",
  "root_cause_hypothesis": "<most likely explanation with named evidence>",
  "confidence":            "<high|medium|low>",
  "evidence_quotes":       ["<verbatim excerpt 1>", "<verbatim excerpt 2>"],
  "proposed_change": {{
    "type":          "<code|directive|none>",
    "target":        "<file:function or DIRECTIVE.md section or none>",
    "description":   "<what to change and why>",
    "current_text":  "<exact current wording, or null>",
    "proposed_text": "<proposed replacement wording, or null>"
  }},
  "open_questions": ["<what to watch for in next run 1>", "..."]
}}
"""
    return prompt


# ── API call ───────────────────────────────────────────────────────────────────

def interpret_run(
    scorecard:     dict,
    trajectory:    dict,
    result:        dict,
    review_md:     str,
    cycle_history: list[dict],
    action:        str,
    model:         str = "claude-opus-4-6",
    pipeline_dir:  Path | None = None,
) -> dict:
    """
    Call the Claude API and return a structured interpretation dict.
    Returns an error dict if the API call fails.

    Uses claude-opus-4-6 by default — this is an analytical task requiring
    careful reading of long context. Haiku would miss subtle patterns.
    """
    try:
        import anthropic
    except ImportError:
        return _error("anthropic SDK not installed — run: pip install anthropic")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return _error("ANTHROPIC_API_KEY not set in environment")

    # collect_more_runs: there's nothing to interpret yet
    if action == "collect_more_runs":
        n_runs = trajectory.get("n_runs", 0)
        return {
            "pattern_analysis": (
                f"Only {n_runs} run(s) recorded. Withholding LLM analysis until "
                "at least 3 comparable runs exist to distinguish signal from noise."
            ),
            "root_cause_hypothesis": "Insufficient data.",
            "confidence": "low",
            "evidence_quotes": [],
            "proposed_change": {
                "type": "none", "target": "none",
                "description": "Collect more runs first.",
                "current_text": None, "proposed_text": None,
            },
            "open_questions": [
                "Do key metrics (wer_committed_pct, out_of_order_event_count) remain "
                "stable across the next 1-2 runs?",
                "Does fragment_leak_count stay at 1 or grow?",
            ],
        }

    prompt = _build_prompt(
        scorecard, trajectory, result, review_md, cycle_history, action, pipeline_dir
    )

    print(f"  Calling Claude ({model}) for benchmark interpretation...")

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()

    # Strip markdown code fences if the model wrapped the JSON
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError as e:
        return _error(f"Claude returned non-JSON: {e}\n\nRaw response:\n{raw[:500]}")

    # Validate required keys
    required = [
        "pattern_analysis", "root_cause_hypothesis", "confidence",
        "evidence_quotes", "proposed_change", "open_questions",
    ]
    for key in required:
        if key not in analysis:
            analysis[key] = None

    return analysis


def _error(msg: str) -> dict:
    return {
        "pattern_analysis": f"[ERROR] {msg}",
        "root_cause_hypothesis": "n/a",
        "confidence": "low",
        "evidence_quotes": [],
        "proposed_change": {
            "type": "none", "target": "none", "description": msg,
            "current_text": None, "proposed_text": None,
        },
        "open_questions": [],
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 4:
        print("Usage: llm_interpret.py <scorecard.json> <trajectory.json> <run.json> [review.md]")
        sys.exit(1)

    sc_path   = Path(sys.argv[1])
    traj_path = Path(sys.argv[2])
    run_path  = Path(sys.argv[3])
    rev_path  = Path(sys.argv[4]) if len(sys.argv) >= 5 else None

    scorecard  = json.loads(sc_path.read_text(encoding="utf-8"))
    trajectory = json.loads(traj_path.read_text(encoding="utf-8"))

    raw = run_path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            result = json.loads(raw.decode(enc))
            break
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    else:
        print("Could not decode run JSON")
        sys.exit(1)

    review_md = rev_path.read_text(encoding="utf-8") if rev_path and rev_path.exists() else ""
    action    = (trajectory.get("metrics") or {})  # placeholder

    # Infer action from latest review file if not provided
    action_str = "investigate"  # default for standalone CLI use

    analysis = interpret_run(
        scorecard, trajectory, result, review_md, [], action_str
    )
    print(json.dumps(analysis, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
