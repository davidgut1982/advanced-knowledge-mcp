"""Deduplication for extracted memory candidates.

Before writing an extracted fact we check whether the KB already holds a
near-identical entry. If it does, the orchestrator updates that entry instead
of creating a duplicate. Similarity is read from the ``rrf_score`` the real
``LoreClient.kb_search`` surfaces in hybrid mode (the only score field present
in production results), compared against an rrf_score-calibrated threshold.
"""

from __future__ import annotations

import logging
from typing import Any

from .schema import MemoryCandidate

logger = logging.getLogger(__name__)

AUTO_MEMORY_TOPIC = "auto-memory"

# The only score field the real LoreClient.kb_search returns in hybrid mode.
# Earlier candidates ("similarity"/"cosine"/"score") never appear in production
# results, so matching on them silently disabled dedup.
_SIMILARITY_KEYS = ("rrf_score",)


def _hit_similarity(hit: dict[str, Any]) -> float:
    """Best available rrf_score for a single search hit (0.0 if absent)."""
    for key in _SIMILARITY_KEYS:
        value = hit.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _best_match(hits: Any) -> tuple[float, str | None]:
    """Return (highest_similarity, kb_id) across hits, or (0.0, None).

    ``hits`` is whatever ``kb_search`` returned (untyped at the boundary), so
    each row is validated as a dict before use.
    """
    best_score = 0.0
    best_id: str | None = None
    for hit in hits or []:
        if not isinstance(hit, dict):  # defensive: tolerate malformed rows
            continue
        score = _hit_similarity(hit)
        if score > best_score:
            best_score = score
            best_id = hit.get("kb_id")
    return best_score, best_id


async def should_merge(
    candidate: MemoryCandidate,
    db_client: Any,
    rrf_threshold: float = 0.12,
) -> tuple[bool, str | None]:
    """Decide whether ``candidate`` duplicates an existing KB entry.

    Returns ``(should_merge, existing_kb_id)``. Searches the auto-memory topic
    first, then the whole KB (topic=None) to catch manually-added duplicates,
    and returns the highest-scoring match if it meets ``rrf_threshold`` (an
    rrf_score-calibrated value; rrf_score maxes around 0.3 in hybrid mode).
    Best-effort: a search failure yields ``(False, None)`` so the candidate is
    inserted as new rather than silently dropped.
    """
    best_score = 0.0
    best_id: str | None = None

    for topic in (AUTO_MEMORY_TOPIC, None):
        try:
            hits = db_client.kb_search(candidate.content, topic=topic, top_k=3)
        except Exception as exc:  # noqa: BLE001 - dedup is best-effort
            logger.debug("dedup search failed (topic=%s): %s", topic, exc)
            continue

        score, kb_id = _best_match(hits)
        if score > best_score:
            best_score = score
            best_id = kb_id

    if best_id is not None and best_score >= rrf_threshold:
        return True, best_id
    return False, None
