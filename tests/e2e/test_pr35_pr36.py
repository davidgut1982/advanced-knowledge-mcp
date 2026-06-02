"""End-to-end tests for PR #35 (default search mode) and PR #36 (summary removed).

PR #35: ``handle_kb_search`` resolves the search mode from the
``LORE_SEARCH_MODE_DEFAULT`` env var (default ``"hybrid"``) when the caller does
not pass an explicit mode, instead of hardcoding ``"fts"``.  The response now
echoes both ``requested_mode`` (the resolved-but-pre-degradation mode) and
``search_mode`` (the mode actually executed, which may degrade on staging where
semantic search is disabled).

PR #36: ``"summary"`` was removed from the ``search_mode`` enum
(``Literal["fts", "semantic", "hybrid"]``) in ``server_fastmcp.py``.  Passing
``search_mode="summary"`` is no longer a valid strategy and must surface as an
MCP error (raised as :class:`LoreClientError` by the client).

Why: lock in the new default-mode behaviour and guard against a regression that
re-introduces the removed ``summary`` strategy.
How to test: run against a live Lore instance with ``LORE_E2E_URL`` set; the
conftest env-gate skips these when no endpoint is configured.
"""

from __future__ import annotations

import pytest

from .client import LoreClient, LoreClientError

# Tag as e2e so the suite honours the LORE_E2E_URL gate and `-m` filtering.
pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# PR #35 — default search mode resolves to hybrid (not fts)
# ---------------------------------------------------------------------------


class TestDefaultSearchMode:
    """Verify the requested mode echoed by kb_search reflects PR #35 semantics.

    These assertions key off ``requested_mode`` rather than ``search_mode``: on
    staging semantic search is disabled, so ``search_mode`` may degrade to
    ``"fts"`` even when the *requested* mode is ``"hybrid"``.  ``requested_mode``
    captures the caller's intent before any degradation.
    """

    def test_no_mode_requests_hybrid(self, client: LoreClient) -> None:
        """kb_search with no mode arg must report requested_mode == 'hybrid'.

        Why: PR #35 switched the implicit default from 'fts' to the
        LORE_SEARCH_MODE_DEFAULT value (default 'hybrid').
        """
        result = client.kb_search("pr35 default mode probe")
        assert result.get("requested_mode") == "hybrid", (
            "Default kb_search should request 'hybrid' (PR #35); "
            f"got requested_mode={result.get('requested_mode')!r}, "
            f"search_mode={result.get('search_mode')!r}"
        )

    def test_explicit_fts_requests_fts(self, client: LoreClient) -> None:
        """Explicit search_mode='fts' must report requested_mode == 'fts'."""
        result = client.kb_search("pr35 explicit fts probe", search_mode="fts")
        assert result.get("requested_mode") == "fts", (
            f"Expected requested_mode='fts', got {result.get('requested_mode')!r}"
        )

    def test_explicit_hybrid_requests_hybrid(self, client: LoreClient) -> None:
        """Explicit search_mode='hybrid' must report requested_mode == 'hybrid'."""
        result = client.kb_search("pr35 explicit hybrid probe", search_mode="hybrid")
        assert result.get("requested_mode") == "hybrid", (
            f"Expected requested_mode='hybrid', got {result.get('requested_mode')!r}"
        )


# ---------------------------------------------------------------------------
# PR #36 — 'summary' is no longer a valid search mode
# ---------------------------------------------------------------------------


class TestSummaryModeRemoved:
    """Verify the removed 'summary' strategy is rejected.

    Why: PR #36 dropped Literal["summary"] from the search_mode enum. A request
    for it must fail rather than silently fall back to another mode.
    How to test: assert kb_search(search_mode='summary') raises LoreClientError.
    """

    def test_summary_mode_raises(self, client: LoreClient) -> None:
        """search_mode='summary' must raise LoreClientError (invalid enum value)."""
        with pytest.raises(LoreClientError):
            client.kb_search("pr36 summary mode probe", search_mode="summary")
