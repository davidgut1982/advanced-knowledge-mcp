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
