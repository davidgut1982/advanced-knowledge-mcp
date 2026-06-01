"""Tests for dedup logic and DEDUP_THRESHOLD calibration.

Lore's hybrid-mode search returns ``rrf_score`` where HIGHER = more
similar (verified live against CT 133 / 192.168.1.21:5555). Semantic-only
mode returns score=None, so hybrid + rrf_score is the reliable dedup
signal. Dedup rule: if the top hit's rrf_score >= DEDUP_THRESHOLD, the new
content is a near-duplicate -> kb_update the existing entry instead of
kb_add.
"""

from __future__ import annotations

from lore.lore_client import DEDUP_THRESHOLD, add_or_update

# ---------------------------------------------------------------------------
# Threshold calibration
# ---------------------------------------------------------------------------


def test_dedup_threshold_is_sensible_default():
    # rrf_score for hybrid search is in (0, ~0.3]. Threshold must be a
    # positive float strictly inside that band, not 0 or 1.
    assert isinstance(DEDUP_THRESHOLD, float)
    assert 0.0 < DEDUP_THRESHOLD < 0.3


# Fixture pairs calibrated from live observation: near-duplicate content
# fuses to a high rrf_score; unrelated content scores low.
SIMILAR_TOP_HIT = {"kb_id": "kb_existing", "rrf_score": 0.18, "score": 8.4}
DISSIMILAR_TOP_HIT = {"kb_id": "kb_other", "rrf_score": 0.03, "score": 2.1}


def test_threshold_separates_similar_from_dissimilar():
    # The calibrated default must classify the similar pair as a dup and
    # the dissimilar pair as new.
    assert SIMILAR_TOP_HIT["rrf_score"] >= DEDUP_THRESHOLD
    assert DISSIMILAR_TOP_HIT["rrf_score"] < DEDUP_THRESHOLD


# ---------------------------------------------------------------------------
# add_or_update behavior
# ---------------------------------------------------------------------------


def test_near_duplicate_triggers_update(mock_client):
    mock_client.search_queue = [[SIMILAR_TOP_HIT]]
    result = add_or_update(
        mock_client,
        topic="hermes-conversations",
        title="Deploy",
        content="You deploy via the hermes-scheduler cron job.",
    )
    assert result["action"] == "updated"
    assert result["kb_id"] == "kb_existing"
    assert len(mock_client.updated) == 1
    assert len(mock_client.added) == 0


def test_dissimilar_triggers_add(mock_client):
    mock_client.search_queue = [[DISSIMILAR_TOP_HIT]]
    result = add_or_update(
        mock_client,
        topic="hermes-conversations",
        title="New fact",
        content="A totally unrelated brand new fact.",
    )
    assert result["action"] == "added"
    assert len(mock_client.added) == 1
    assert len(mock_client.updated) == 0


def test_no_results_triggers_add(mock_client):
    mock_client.search_queue = [[]]  # empty search result
    result = add_or_update(mock_client, topic="t", title="x", content="first ever entry")
    assert result["action"] == "added"
    assert len(mock_client.added) == 1


def test_dedup_uses_hybrid_search(mock_client):
    mock_client.search_queue = [[]]
    add_or_update(mock_client, topic="t", title="x", content="some content")
    assert len(mock_client.search_calls) == 1
    call = mock_client.search_calls[0]
    assert call["search_mode"] == "hybrid"
    # dedup probes with the content as the query
    assert call["query"] == "some content"


def test_dedup_respects_custom_threshold(mock_client):
    # rrf_score 0.05 is below default but above a very low custom threshold
    mock_client.search_queue = [[{"kb_id": "kb_x", "rrf_score": 0.05}]]
    result = add_or_update(mock_client, topic="t", title="x", content="c", threshold=0.04)
    assert result["action"] == "updated"


def test_missing_rrf_score_treated_as_dissimilar(mock_client):
    # Defensive: if a result lacks rrf_score (e.g. fts mode), do not dedup.
    mock_client.search_queue = [[{"kb_id": "kb_x", "score": 5.0}]]
    result = add_or_update(mock_client, topic="t", title="x", content="c")
    assert result["action"] == "added"


def test_dedup_probe_failure_falls_back_to_add():
    # LOW finding: add_or_update no longer does a pre-flight is_available()
    # /health GET. It trusts the exception-based error handling — when the
    # dedup probe (kb_search) raises, it falls back to a plain add rather
    # than crashing.
    from lore.tests.conftest import MockLoreClient

    client = MockLoreClient(search_raises=True)
    result = add_or_update(client, topic="t", title="x", content="c")
    assert result["action"] == "added"
    assert len(client.added) == 1
    assert len(client.updated) == 0


def test_add_or_update_does_not_call_is_available(mock_client):
    # LOW finding: confirm the redundant /health pre-check was removed —
    # add_or_update must not invoke client.is_available() on the write path.
    called = {"is_available": False}
    original = mock_client.is_available

    def _tracked() -> bool:
        called["is_available"] = True
        return original()

    mock_client.is_available = _tracked  # type: ignore[method-assign]
    mock_client.search_queue = [[]]
    add_or_update(mock_client, topic="t", title="x", content="c")
    assert called["is_available"] is False
