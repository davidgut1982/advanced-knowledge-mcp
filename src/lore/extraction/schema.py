"""Pydantic models for the automatic memory-extraction pipeline.

These mirror the JSON contract the extraction LLM is asked to emit
(see ``prompts.EXTRACTION_PROMPT_V1``). Keeping the schema small and
strict lets us reject malformed model output cheaply before any KB write.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    """Category of an extracted memory.

    Inherits from ``str`` so the enum members serialize to their bare
    string values in JSON and compare equal to plain strings (e.g. the
    f-string tag ``f"type:{candidate.type}"`` renders as ``type:preference``).
    """

    USER_FACT = "user_fact"
    PREFERENCE = "preference"
    GOAL = "goal"
    RELATIONSHIP = "relationship"
    EVENT = "event"
    SYSTEM_FACT = "system_fact"


class MemoryCandidate(BaseModel):
    """A single candidate fact emitted by the extraction model."""

    type: MemoryType
    subject: str = "user"  # "user", "project:name", "system:name"
    content: str  # normalized canonical statement
    confidence: float = Field(ge=0.0, le=1.0)
    durable: bool = True
    tags: list[str] = []


class ExtractionResult(BaseModel):
    """Top-level container returned by the extraction client."""

    memories: list[MemoryCandidate] = []
