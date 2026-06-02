"""Tests for cluster_results schema and grouping behaviour (Issue #22).

Originally contributed by ManjunathByadagi in PR #23. Adapted to use
_TOOL_DEFINITIONS (the canonical schema registry) instead of _TOOL_SCHEMA_MAP.
"""

from __future__ import annotations

import lore.server as srv


def test_cluster_results_schema_has_no_cluster_count_params():
    tool = next(t for t in srv._TOOL_DEFINITIONS if t.name == "cluster_results")
    props = tool.inputSchema["properties"]
    assert "num_clusters" not in props
    assert "n_clusters" not in props
    assert list(props) == ["results"]


def test_cluster_results_groups_by_source_bucket():
    results = [
        {"file": "docs/guide.md", "title": "Guide"},
        {"file": "src/app.py", "title": "App"},
        {"corpus": "research", "title": "Note"},
        {"speaker": "alice", "title": "Transcript"},
        {"title": "Fallback"},
    ]

    resp = srv.handle_cluster_results(results=results)

    assert resp["ok"] is True
    assert resp["data"]["total_results"] == 5
    assert set(resp["data"]["cluster_summary"]) == {".md", ".py", "corpus", "transcript", "other"}
    assert resp["data"]["cluster_summary"]["other"] == 1
