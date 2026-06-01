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
    "PENDING_TOPIC",
]

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = 0.75
DEFAULT_DEDUP_THRESHOLD = 0.85
DEFAULT_MIN_TURNS = 3
_AUTO_TAG = "source:auto-extracted"
_PENDING_TAG = "status:pending"
PENDING_TOPIC = "auto-memory-pending"


def _empty_summary() -> dict[str, int]:
    return {"extracted": 0, "inserted": 0, "merged": 0, "skipped": 0}


def _effective_threshold(candidate: MemoryCandidate, config: dict) -> float:
    """Resolve the confidence threshold for a candidate.

    Per-type overrides in ``auto_extract.type_thresholds`` win; otherwise the
    global ``confidence_threshold`` (falling back to the package default).
    """
    auto = (config or {}).get("auto_extract", {})
    type_thresholds = auto.get("type_thresholds") or {}
    global_threshold = auto.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)
    return float(type_thresholds.get(candidate.type.value, global_threshold))


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


def _kb_add(db_client: Any, candidate: MemoryCandidate, *, review_mode: bool = False) -> None:
    """Add a new auto-memory entry.

    When ``review_mode`` is set, the entry lands in the pending topic with a
    ``status:pending`` tag instead of going straight into the main KB.
    """
    topic = PENDING_TOPIC if review_mode else AUTO_MEMORY_TOPIC
    tags = _candidate_tags(candidate)
    if review_mode:
        tags = [*tags, _PENDING_TAG]
    db_client.kb_add(
        topic=topic,
        title=_candidate_title(candidate),
        content=candidate.content,
        tags=tags,
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

    dedup_threshold = float(auto.get("dedup_similarity_threshold", DEFAULT_DEDUP_THRESHOLD))
    dry_run = bool(auto.get("dry_run", False))
    review_mode = bool(auto.get("review_mode", False))

    conversation_text = _format_turns(turns)
    client = ExtractionClient(config)
    result = await client.extract(conversation_text, config)

    summary["extracted"] = len(result.memories)

    for candidate in result.memories:
        if candidate.confidence < _effective_threshold(candidate, config) or not candidate.durable:
            summary["skipped"] += 1
            continue

        try:
            merge, existing_id = await should_merge(
                candidate, db_client, rrf_threshold=dedup_threshold
            )
        except Exception as exc:  # noqa: BLE001 - dedup must not break the pipeline
            logger.debug("dedup raised, treating as new entry: %s", exc)
            merge, existing_id = False, None

        if dry_run:
            # Full pipeline ran (LLM + dedup); we only skip the KB writes so
            # operators can tune thresholds against real candidate logs.
            logger.info(
                "[dry-run] would %s: [%s] %s (confidence=%.2f, subject=%s)",
                "merge" if merge else "insert",
                candidate.type.value,
                candidate.content[:80],
                candidate.confidence,
                candidate.subject,
            )
            summary["skipped"] += 1
            continue

        try:
            if merge and existing_id:
                # A merge confirms an already-accepted fact, so it is applied
                # directly to the main auto-memory entry regardless of
                # review_mode — only brand-new inserts are held for review.
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
                _kb_add(db_client, candidate, review_mode=review_mode)
                summary["inserted"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad write must not abort the rest
            logger.warning("Auto-memory KB write failed: %s", exc)
            summary["skipped"] += 1

    if dry_run:
        summary["dry_run"] = True

    logger.info(
        "Auto-extraction summary: extracted=%d inserted=%d merged=%d skipped=%d dry_run=%s",
        summary["extracted"],
        summary["inserted"],
        summary["merged"],
        summary["skipped"],
        dry_run,
    )
    return summary
