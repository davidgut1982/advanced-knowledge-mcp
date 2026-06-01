"""Automatic memory extraction pipeline.

End-to-end: conversation turns → LLM extraction → dedup → KB writes.

The single public entry point is :func:`extract_and_store`. It is designed to
be fired as a fire-and-forget background task at the end of a Hermes session,
so it is fully best-effort: disabled by config, a missing API key, a flaky
extraction call, or an individual KB write failure all degrade to a smaller
(or empty) summary rather than raising.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import ExtractionClient
from .dedup import AUTO_MEMORY_TOPIC, should_merge
from .filters import DEFAULT_MAX_TURNS, filter_turns
from .schema import ExtractionResult, MemoryCandidate, MemoryType

__all__ = [
    "extract_and_store",
    "filter_turns",
    "ExtractionClient",
    "ExtractionResult",
    "MemoryCandidate",
    "MemoryType",
    "should_merge",
    "AUTO_MEMORY_TOPIC",
]

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = 0.75
DEFAULT_DEDUP_THRESHOLD = 0.85
DEFAULT_MIN_TURNS = 3
_AUTO_TAG = "source:auto-extracted"


def _empty_summary() -> dict[str, int]:
    return {"extracted": 0, "inserted": 0, "merged": 0, "skipped": 0}


def _format_turns(turns: list[dict]) -> str:
    """Render turns into a flat ``[role]: content`` transcript."""
    lines: list[str] = []
    for turn in turns:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        lines.append(f"[{role}]: {content}")
    return "\n".join(lines) + "\n" if lines else ""


def _candidate_title(candidate: MemoryCandidate) -> str:
    """Short, stable title derived from the canonical statement."""
    content = candidate.content.strip()
    return content[:60] if content else f"{candidate.type.value} memory"


def _candidate_tags(candidate: MemoryCandidate) -> list[str]:
    """Tags for an auto-memory entry.

    Confidence is encoded as a ``confidence:<score>`` tag because the real
    LoreClient.kb_add has no ``trust_score`` kwarg — tags are the durable,
    queryable carrier for the extraction confidence.
    """
    confidence_tag = f"confidence:{round(candidate.confidence, 2)}"
    return [
        _AUTO_TAG,
        f"type:{candidate.type.value}",
        confidence_tag,
        *candidate.tags,
    ]


def _kb_add(db_client: Any, candidate: MemoryCandidate) -> None:
    """Add a new auto-memory entry."""
    db_client.kb_add(
        topic=AUTO_MEMORY_TOPIC,
        title=_candidate_title(candidate),
        content=candidate.content,
        tags=_candidate_tags(candidate),
    )


async def extract_and_store(
    turns: list[dict],
    db_client: Any,
    config: dict,
) -> dict:
    """Run the full extraction → dedup → KB-write pipeline.

    Returns a summary ``{"extracted", "inserted", "merged", "skipped"}``.
    Returns an all-zero summary when extraction is disabled, the session is
    below ``min_turns``, or the API key is missing.
    """
    summary = _empty_summary()

    auto = (config or {}).get("auto_extract", {})
    if not auto.get("enabled", False):
        return summary

    min_turns = int(auto.get("min_turns", DEFAULT_MIN_TURNS))
    if not turns or len(turns) < min_turns:
        logger.debug(
            "Skipping extraction: %d turns < min_turns=%d",
            len(turns or []),
            min_turns,
        )
        return summary

    max_turns = int(auto.get("max_turns", DEFAULT_MAX_TURNS))
    min_turn_chars = int(auto.get("min_turn_chars", 20))
    max_turn_chars = int(auto.get("max_turn_chars", 1200))

    turns = filter_turns(
        turns,
        max_turns=max_turns,
        min_turn_chars=min_turn_chars,
        max_turn_chars=max_turn_chars,
    )

    if not turns:
        return summary

    confidence_threshold = float(
        auto.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)
    )
    dedup_threshold = float(
        auto.get("dedup_similarity_threshold", DEFAULT_DEDUP_THRESHOLD)
    )

    conversation_text = _format_turns(turns)
    client = ExtractionClient(config)
    result = await client.extract(conversation_text, config)

    summary["extracted"] = len(result.memories)

    for candidate in result.memories:
        if candidate.confidence < confidence_threshold or not candidate.durable:
            summary["skipped"] += 1
            continue

        try:
            merge, existing_id = await should_merge(
                candidate, db_client, rrf_threshold=dedup_threshold
            )
        except Exception as exc:  # noqa: BLE001 - dedup must not break the pipeline
            logger.debug("dedup raised, treating as new entry: %s", exc)
            merge, existing_id = False, None

        try:
            if merge and existing_id:
                # Refresh title, content AND tags so the merged entry doesn't
                # keep a stale title/confidence/type from the original write.
                db_client.kb_update(
                    existing_id,
                    title=_candidate_title(candidate),
                    content=candidate.content,
                    tags=_candidate_tags(candidate),
                )
                summary["merged"] += 1
            else:
                _kb_add(db_client, candidate)
                summary["inserted"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad write must not abort the rest
            logger.warning("Auto-memory KB write failed: %s", exc)
            summary["skipped"] += 1

    logger.info(
        "Auto-extraction summary: extracted=%d inserted=%d merged=%d skipped=%d",
        summary["extracted"],
        summary["inserted"],
        summary["merged"],
        summary["skipped"],
    )
    return summary
