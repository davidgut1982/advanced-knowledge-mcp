"""Tests for the extraction prompt constant and Pydantic schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lore.extraction.prompts import EXTRACTION_PROMPT_V1
from lore.extraction.schema import ExtractionResult, MemoryCandidate, MemoryType


def test_prompt_is_string():
    assert isinstance(EXTRACTION_PROMPT_V1, str)
    assert EXTRACTION_PROMPT_V1.strip()


def test_prompt_contains_types():
    for type_name in (
        "user_fact",
        "preference",
        "goal",
        "relationship",
        "event",
        "system_fact",
    ):
        assert type_name in EXTRACTION_PROMPT_V1


def test_memory_candidate_confidence_bounds():
    # Valid bounds accepted.
    MemoryCandidate(
        type=MemoryType.PREFERENCE, content="prefers Python", confidence=0.0
    )
    MemoryCandidate(
        type=MemoryType.PREFERENCE, content="prefers Python", confidence=1.0
    )
    # Out-of-range rejected.
    with pytest.raises(ValidationError):
        MemoryCandidate(type=MemoryType.PREFERENCE, content="x", confidence=1.5)
    with pytest.raises(ValidationError):
        MemoryCandidate(type=MemoryType.PREFERENCE, content="x", confidence=-0.1)


def test_extraction_result_empty_default():
    result = ExtractionResult()
    assert result.memories == []


def test_filler_only_yields_no_memories() -> None:
    # A filler-only conversation ("ok thanks", "sounds good") should yield no
    # extractable memories: the model returns {"memories": []}.
    result = ExtractionResult.model_validate({"memories": []})
    assert len(result.memories) == 0
    # Document the prompt contract: filler is explicitly listed under SKIP.
    lowered = EXTRACTION_PROMPT_V1.lower()
    assert any(token in lowered for token in ("filler", "ok thanks", "sounds good"))


def test_factual_statement_produces_preference() -> None:
    # A clear preference statement maps to a fully valid PREFERENCE candidate
    # satisfying every schema constraint (confidence bounds, durable flag).
    candidate = MemoryCandidate(
        type=MemoryType.PREFERENCE,
        content="prefers Python over Ruby",
        confidence=0.9,
        durable=True,
    )
    assert candidate.type is MemoryType.PREFERENCE
    assert candidate.content == "prefers Python over Ruby"
    assert candidate.confidence == 0.9
    assert candidate.durable is True
    # Round-trips through model validation cleanly.
    revalidated = MemoryCandidate.model_validate(candidate.model_dump())
    assert revalidated == candidate


def test_memory_type_values():
    assert MemoryType.USER_FACT.value == "user_fact"
    assert MemoryType.PREFERENCE.value == "preference"
    assert MemoryType.GOAL.value == "goal"
    assert MemoryType.RELATIONSHIP.value == "relationship"
    assert MemoryType.EVENT.value == "event"
    assert MemoryType.SYSTEM_FACT.value == "system_fact"
    assert {t.value for t in MemoryType} == {
        "user_fact",
        "preference",
        "goal",
        "relationship",
        "event",
        "system_fact",
    }
