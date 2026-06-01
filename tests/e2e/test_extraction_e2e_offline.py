"""CI-safe golden end-to-end tests for the extraction pipeline.

No API key is needed: ``httpx.AsyncClient.post`` is mocked to return a fixed,
realistic LLM JSON envelope per scenario. Each test drives the *real*
``extract_and_store`` orchestrator (filters → client parse → confidence/durable
gate → dedup → KB write) end-to-end and asserts the writes landed correctly.

Convention note: this project does **not** install ``pytest-asyncio`` and
deliberately avoids ``async def test_*`` (see ``pyproject.toml``). Async paths
are driven via ``asyncio.run()`` inside ordinary sync test functions, exactly
like ``tests/test_extraction_orchestrator.py``. These tests follow that pattern
rather than ``@pytest.mark.asyncio`` so they run under the existing suite with
no extra plugins.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest import mock

from lore.extraction import extract_and_store

from .harness import FakeDbClient, score_scenario
from .scenarios import SCENARIOS

# ---------------------------------------------------------------------------
# Golden LLM responses (one realistic chat-completions envelope per scenario)
# ---------------------------------------------------------------------------


def _envelope(memories: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a memories list in an OpenAI-style chat-completions envelope."""
    return {"choices": [{"message": {"content": json.dumps({"memories": memories})}}]}


GOLDEN_PROGRESSIVE = _envelope(
    [
        {
            "type": "user_fact",
            "subject": "user",
            "content": "is a backend engineer, primarily works with Python and Go",
            "confidence": 0.95,
            "durable": True,
            "tags": [],
        },
        {
            "type": "system_fact",
            "subject": "system:proxmox",
            "content": "runs Proxmox on 3 nodes as homelab infrastructure",
            "confidence": 0.90,
            "durable": True,
            "tags": [],
        },
        {
            "type": "preference",
            "subject": "user",
            "content": "prefers NVMe storage for databases over bulk storage",
            "confidence": 0.85,
            "durable": True,
            "tags": [],
        },
        {
            "type": "goal",
            "subject": "user",
            "content": "plans to migrate stateful services to Kubernetes (k8s) in Q3",
            "confidence": 0.88,
            "durable": True,
            "tags": [],
        },
    ]
)

GOLDEN_RESEARCH = _envelope(
    [
        {
            "type": "system_fact",
            "subject": "system:lore",
            "content": (
                "Lore uses PostgreSQL with pgvector for semantic search; each KB entry "
                "carries a vector embedding"
            ),
            "confidence": 0.93,
            "durable": True,
            "tags": [],
        },
        {
            "type": "system_fact",
            "subject": "system:lore",
            "content": (
                "rrf_score ranges from roughly 0.0 to 0.3 and is not a cosine score, so "
                "0.85 is the wrong threshold"
            ),
            "confidence": 0.9,
            "durable": True,
            "tags": [],
        },
        {
            "type": "relationship",
            "subject": "system:hermes",
            "content": "Hermes is the main AI assistant consumer of Lore via session hooks",
            "confidence": 0.9,
            "durable": True,
            "tags": [],
        },
    ]
)

GOLDEN_DEBUG = _envelope(
    [
        {
            "type": "event",
            "subject": "system:lore",
            "content": (
                "Fixed a kb_search timeout (>20 results) caused by a sequential scan on the "
                "pgvector index"
            ),
            "confidence": 0.88,
            "durable": True,
            "tags": [],
        },
        {
            "type": "system_fact",
            "subject": "system:postgres",
            "content": (
                "Lowering ivfflat.probes to 3 resolved the vector index scan timeout; search "
                "now under 50ms"
            ),
            "confidence": 0.9,
            "durable": True,
            "tags": [],
        },
    ]
)

# Noise scenario: a well-behaved model returns no durable memories.
GOLDEN_NOISE = _envelope([])

GOLDEN_CODE = _envelope(
    [
        {
            "type": "preference",
            "subject": "user",
            "content": "uses pgvector for all semantic search needs",
            "confidence": 0.85,
            "durable": True,
            "tags": [],
        },
    ]
)

# ---------------------------------------------------------------------------
# Golden responses for scenarios F–J.
#
# These are minimal-but-valid envelopes engineered to satisfy each scenario's
# ``must_contain`` ground truth while avoiding ``must_not_contain`` substrings
# and respecting ``max_extracted``. They mirror how a well-behaved model *should*
# resolve each edge case (correcting contradictions, skipping filler, attributing
# people, breadth across types, and preferring the refined goal). When live
# golden captures are recorded later, these can be replaced wholesale.
# ---------------------------------------------------------------------------

# F: contradiction_update — keep the CORRECTED fact (k3s), drop the error.
GOLDEN_CONTRADICTION = _envelope(
    [
        {
            "type": "system_fact",
            "subject": "system:cluster",
            "content": "runs k3s (lightweight Kubernetes) on a 2-node homelab cluster",
            "confidence": 0.9,
            "durable": True,
            "tags": [],
        },
        {
            "type": "event",
            "subject": "system:cluster",
            "content": "switched the cluster CNI from Flannel to a WireGuard mesh last month",
            "confidence": 0.88,
            "durable": True,
            "tags": [],
        },
    ]
)

# G: skip_rules_validation — only the two real facts survive the filler.
GOLDEN_SKIP_RULES = _envelope(
    [
        {
            "type": "preference",
            "subject": "user",
            "content": "uses vim for everything, configured with vim-plug",
            "confidence": 0.88,
            "durable": True,
            "tags": [],
        },
        {
            "type": "user_fact",
            "subject": "user",
            "content": "primary programming language is Python",
            "confidence": 0.9,
            "durable": True,
            "tags": [],
        },
    ]
)

# H: relationship_org — the user's role plus the two named teammates.
GOLDEN_RELATIONSHIP = _envelope(
    [
        {
            "type": "user_fact",
            "subject": "user",
            "content": "leads the platform team, owning the internal developer platform",
            "confidence": 0.92,
            "durable": True,
            "tags": [],
        },
        {
            "type": "relationship",
            "subject": "person:sarah",
            "content": "Sarah handles the secrets vault on the platform team",
            "confidence": 0.88,
            "durable": True,
            "tags": [],
        },
        {
            "type": "relationship",
            "subject": "person:miguel",
            "content": "Miguel owns the CI/CD pipelines on the platform team",
            "confidence": 0.88,
            "durable": True,
            "tags": [],
        },
    ]
)

# I: dense_technical — breadth across many fact types.
GOLDEN_DENSE = _envelope(
    [
        {
            "type": "system_fact",
            "subject": "user:devenv",
            "content": "runs Arch Linux on a ThinkPad X1",
            "confidence": 0.9,
            "durable": True,
            "tags": [],
        },
        {
            "type": "preference",
            "subject": "user",
            "content": "uses Neovim configured with LazyVim, all config in Lua",
            "confidence": 0.9,
            "durable": True,
            "tags": [],
        },
        {
            "type": "preference",
            "subject": "user",
            "content": "manages Python with pyenv plus uv for packages (uv much faster than pip)",
            "confidence": 0.88,
            "durable": True,
            "tags": [],
        },
        {
            "type": "preference",
            "subject": "user",
            "content": (
                "deploys with Ansible for provisioning and Kamal for apps; Docker for dev, "
                "Podman for production"
            ),
            "confidence": 0.87,
            "durable": True,
            "tags": [],
        },
    ]
)

# J: goal_refinement — the refined goal, not the vague initial one.
GOLDEN_GOAL = _envelope(
    [
        {
            "type": "goal",
            "subject": "user",
            "content": (
                "build an observability stack with P95 latency dashboards for all services "
                "by end of month"
            ),
            "confidence": 0.9,
            "durable": True,
            "tags": [],
        },
        {
            "type": "system_fact",
            "subject": "system:observability",
            "content": (
                "observability stack standardizes on Prometheus, Loki, and Tempo, fed by "
                "Otel collectors across 8 services"
            ),
            "confidence": 0.88,
            "durable": True,
            "tags": [],
        },
    ]
)

GOLDEN_BY_KEY: dict[str, dict[str, Any]] = {
    "A": GOLDEN_PROGRESSIVE,
    "B": GOLDEN_RESEARCH,
    "C": GOLDEN_DEBUG,
    "D": GOLDEN_NOISE,
    "E": GOLDEN_CODE,
    "F": GOLDEN_CONTRADICTION,
    "G": GOLDEN_SKIP_RULES,
    "H": GOLDEN_RELATIONSHIP,
    "I": GOLDEN_DENSE,
    "J": GOLDEN_GOAL,
}


# ---------------------------------------------------------------------------
# Test plumbing
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` used by ExtractionClient.extract."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:  # no-op: always 200
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _patch_post(payload: dict[str, Any]):
    """Patch ``httpx.AsyncClient.post`` to return a fixed golden envelope."""
    return mock.patch(
        "httpx.AsyncClient.post",
        new=mock.AsyncMock(return_value=_FakeResponse(payload)),
    )


def _config(**overrides: Any) -> dict[str, Any]:
    auto = {
        "enabled": True,
        "provider": "openrouter",
        "model": "meta-llama/llama-3.1-8b-instruct",
        "confidence_threshold": 0.65,
        "dedup_similarity_threshold": 0.12,
        "min_turns": 3,
    }
    auto.update(overrides)
    # ExtractionClient._build_request reads the API key from os.getenv first,
    # then the TOP-LEVEL config key (not auto_extract). Provide a stub there so
    # the request is built and httpx.AsyncClient.post (mocked) is reached.
    return {"auto_extract": auto, "openrouter_api_key": "test-key"}


def _run(key: str, config: dict[str, Any] | None = None) -> tuple[FakeDbClient, dict[str, Any]]:
    """Run a scenario through the pipeline against its golden response."""
    db = FakeDbClient()
    scenario = SCENARIOS[key]
    cfg = config or _config()
    with _patch_post(GOLDEN_BY_KEY[key]):
        summary = asyncio.run(extract_and_store(scenario.turns, db, cfg))
    return db, summary


# ---------------------------------------------------------------------------
# Per-scenario tests
# ---------------------------------------------------------------------------


def test_progressive_personal_writes_all_four():
    db, summary = _run("A")
    assert summary["inserted"] == 4
    assert len(db.added) == 4
    assert all(e["topic"] == "auto-memory" for e in db.added)

    score = score_scenario(SCENARIOS["A"], db.added)
    assert score.passed, score.notes
    assert score.recall_hits == score.recall_total == 4
    assert score.false_positives == 0


def test_progressive_personal_tag_contract():
    db, _ = _run("A")
    # Every entry carries the auto-extraction provenance + type + confidence tags.
    for entry in db.added:
        tags = entry["tags"]
        assert "source:auto-extracted" in tags
        assert any(t.startswith("type:") for t in tags)
        assert any(t.startswith("confidence:") for t in tags)
    # Spot-check the encoded confidence tag value (rounded to 2 dp).
    eng = next(e for e in db.added if "backend engineer" in e["content"])
    assert "type:user_fact" in eng["tags"]
    assert "confidence:0.95" in eng["tags"]


def test_research_architecture_system_facts_and_relationship():
    db, summary = _run("B")
    assert summary["inserted"] == 3
    score = score_scenario(SCENARIOS["B"], db.added)
    assert score.passed, score.notes
    # pgvector + rrf_score system facts, Hermes relationship.
    contents = " ".join(e["content"] for e in db.added)
    assert "pgvector" in contents
    assert "rrf_score" in contents
    assert any("type:relationship" in e["tags"] for e in db.added)


def test_debug_session_event_and_system_fact():
    db, summary = _run("C")
    assert summary["inserted"] == 2
    score = score_scenario(SCENARIOS["C"], db.added)
    assert score.passed, score.notes
    types = {next(t for t in e["tags"] if t.startswith("type:")) for e in db.added}
    assert "type:event" in types
    assert "type:system_fact" in types


def test_noise_only_produces_zero_writes():
    db, summary = _run("D")
    assert summary["extracted"] == 0
    assert summary["inserted"] == 0
    assert db.added == []
    score = score_scenario(SCENARIOS["D"], db.added)
    assert score.passed, score.notes  # cardinality gate (<=1) holds at 0


def test_code_heavy_extracts_fact_not_code():
    db, summary = _run("E")
    assert summary["inserted"] == 1
    score = score_scenario(SCENARIOS["E"], db.added)
    assert score.passed, score.notes
    # No raw SQL leaked into any extracted memory.
    for entry in db.added:
        assert "SELECT" not in entry["content"]
        assert "embedding <=>" not in entry["content"]


def test_filter_strips_sql_before_llm_sees_it():
    """The code_heavy turns contain SQL; filter_turns must strip it.

    We assert the conversation text handed to the client carries no raw SQL,
    proving the filter (not just the model) is doing precision work.
    """
    captured: dict[str, str] = {}

    async def _capture_extract(self, conversation_text, config=None):  # noqa: ANN001
        captured["text"] = conversation_text
        from lore.extraction.schema import ExtractionResult

        return ExtractionResult()

    with mock.patch("lore.extraction.ExtractionClient.extract", new=_capture_extract):
        asyncio.run(extract_and_store(SCENARIOS["E"].turns, FakeDbClient(), _config()))

    assert "text" in captured
    # Fenced/long inline code is replaced by the [code] sentinel.
    assert "SELECT id, content, embedding" not in captured["text"]
    # The durable prose fact still survives the filter.
    assert "pgvector" in captured["text"]


def test_low_confidence_is_skipped_not_written():
    """A golden response below the threshold is counted but not written."""
    low = _envelope(
        [
            {
                "type": "preference",
                "subject": "user",
                "content": "maybe prefers tabs, unsure",
                "confidence": 0.40,
                "durable": True,
                "tags": [],
            }
        ]
    )
    db = FakeDbClient()
    with _patch_post(low):
        summary = asyncio.run(extract_and_store(SCENARIOS["A"].turns, db, _config()))
    assert summary["extracted"] == 1
    assert summary["skipped"] == 1
    assert summary["inserted"] == 0
    assert db.added == []


def test_all_scenarios_pass_against_golden():
    """Sweep: every golden response satisfies its scenario's ground truth."""
    for key in SCENARIOS:
        db, _ = _run(key)
        score = score_scenario(SCENARIOS[key], db.added)
        assert score.passed, f"{key}/{SCENARIOS[key].name}: {score.notes}"
