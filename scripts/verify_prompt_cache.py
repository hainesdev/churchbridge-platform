from __future__ import annotations

import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.services.llm_enrichment_service import (
    _build_system_prompt,
    _build_user_message_blocks,
)


def main() -> None:
    load_dotenv(".env")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    client = anthropic.Anthropic(api_key=api_key)
    model = "claude-haiku-4-5-20251001"
    cache_control = {"type": "ephemeral", "ttl": "5m"}

    history = [
        (
            "Vamos al texto biblico en primera de Juan capitulo uno",
            "Let us go to the biblical text in First John chapter one.",
        ),
        (
            "Dios es luz y no hay ningunas tinieblas en el",
            "God is light and there is no darkness in him.",
        ),
        (
            "Si nosotros decimos que tenemos comunion con el y andamos en tinieblas mentimos",
            "If we say that we have fellowship with him and walk in darkness, we lie.",
        ),
    ]
    extra_context = " ".join(
        [
            "El predicador esta exponiendo primera de Juan y contrastando profesion con practica, luz con tinieblas, verdad con autoengano."
            for _ in range(45)
        ]
    )

    user_blocks = _build_user_message_blocks(
        spanish="Y no practicamos la verdad cuando confesamos una cosa y vivimos otra.",
        google_english="And we do not practice the truth when we confess one thing and live another.",
        topic_context=(
            "Active scripture: 1 John 1:5-6 - God is light; in him there is no darkness at all. "
            "The preacher is pressing the congregation toward honest self-examination. "
            + extra_context
        ),
        sentence_history=history,
        active_passage={
            "reference": "1 John 1:5-6",
            "canonical_english": (
                "God is light; in him there is no darkness at all. "
                "If we claim to have fellowship with him and yet walk in the darkness, "
                "we lie and do not live out the truth."
            ),
        },
        shown_suggestions={"John 8:12", "Ephesians 5:8", "Psalm 119:105"},
        current_mode_label="scripture reading followed by exposition",
        prev_discourse={
            "discourse_tag": "scripture_quote",
            "introduces_quote": False,
            "thought_complete": False,
            "continuation_required": True,
            "source_quality": "clean",
            "display_ready": False,
        },
        recent_modes=["exposition", "scripture", "exposition"],
        current_stt_context={
            "detected_language": "es",
            "detected_languages": ["es", "en"],
            "segment_language_mode": "spanish",
            "dominant_speaker": 1,
            "speaker_switch_count": 0,
            "mixed_speaker_segment": False,
        },
        prev_stt_context={
            "detected_language": "es",
            "detected_languages": ["es"],
            "segment_language_mode": "spanish",
            "dominant_speaker": 1,
            "speaker_switch_count": 0,
            "mixed_speaker_segment": False,
        },
    )
    system_blocks = [
        {
            "type": "text",
            "text": _build_system_prompt(
                {
                    "Espiritu Santo": "Holy Spirit",
                    "gloria a Dios": "glory to God",
                    "arrepentimiento": "repentance",
                }
            ),
            "cache_control": cache_control,
        }
    ]

    count = client.messages.count_tokens(
        model=model,
        system=system_blocks,
        messages=[{"role": "user", "content": user_blocks}],
    )
    print(f"count_tokens_input={count.input_tokens}")

    for call_number in range(1, 3):
        response = client.messages.create(
            model=model,
            max_tokens=64,
            temperature=0,
            system=system_blocks,
            messages=[{"role": "user", "content": user_blocks}],
        )
        usage = response.usage
        print(
            f"call_{call_number}: "
            f"input={getattr(usage, 'input_tokens', 0)} "
            f"output={getattr(usage, 'output_tokens', 0)} "
            f"cache_write={getattr(usage, 'cache_creation_input_tokens', 0)} "
            f"cache_read={getattr(usage, 'cache_read_input_tokens', 0)}"
        )


if __name__ == "__main__":
    main()
