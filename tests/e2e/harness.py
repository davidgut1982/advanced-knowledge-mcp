"""Shared eval machinery: FakeDbClient + scoring against ground truth.

This module is provider-agnostic. It does not know whether the candidates it
scores came from a real OpenRouter/Cerebras call (``eval_extraction.py``) or
from a pre-recorded golden response (``test_extraction_e2e_offline.py``). Both
entry points funnel the same ``FakeDbClient.added`` records through
:func:`score_scenario`, so live and offline runs are judged identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .scenarios import ContainsAny, Expectation, Scenario


class FakeDbClient:
    """Records KB writes and serves empty searches (no merges in eval mode).

    Mirrors the surface ``extract_and_store`` touches on the real
    ``LoreClient``:

    * ``kb_add`` / ``kb_update`` are **sync** (matching the production client;
      the orchestrator calls them without ``await``).
    * ``kb_search`` is **sync** and returns ``[]`` so ``should_merge`` always
      decides "new entry" — we want to observe raw inserts, not merges.

    ``should_merge`` *awaits* the orchestrator-level coroutine, not this
    method, so a plain sync ``kb_search`` is correct here (it mirrors the real
    client, whose ``kb_search`` is also sync).
    """

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def kb_search(self, query: str | None = None, *, top_k: int = 3, topic: str | None = None):
        # Empty results => should_merge returns (False, None) => everything is
        # treated as a fresh insert, which is what the eval wants to measure.
        return []

    def kb_add(self, *, topic, title, content, tags=None, author=None):
        self.added.append({"topic": topic, "title": title, "content": content, "tags": tags or []})
        return {"kb_id": f"kb_eval{len(self.added)}"}

    def kb_update(self, kb_id, *, content=None, title=None, tags=None):
        self.updated.append(
            {"kb_id": kb_id, "content": content, "title": title, "tags": tags or []}
        )
        return {"kb_id": kb_id}


def _type_of(entry: dict[str, Any]) -> str | None:
    """Extract the memory type from an entry's ``type:<value>`` tag."""
    for tag in entry.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("type:"):
            return tag.split(":", 1)[1]
    return None


def _content_matches(entry: dict[str, Any], exp: Expectation) -> bool:
    """Does the entry's content satisfy the expectation's substring requirement?

    Supports either ``contains`` (a single substring) or ``contains_any`` (a
    list of substrings, any of which satisfies the match). Matching is
    case-insensitive. Type is *not* considered here.
    """
    content = (entry.get("content") or "").lower()
    if "contains_any" in exp:
        needles = exp["contains_any"]
        assert isinstance(needles, list)
        return any(str(sub).lower() in content for sub in needles)
    contains = exp.get("contains", "")
    assert isinstance(contains, str)
    return contains.lower() in content


def _wanted_types(exp: Expectation) -> list[str] | None:
    """The acceptable type label(s) for an expectation, or ``None`` for any.

    ``type_any`` (a list) takes precedence over ``type`` (a single value).
    Returns ``None`` when the expectation does not constrain type.
    """
    if "type_any" in exp:
        types = exp["type_any"]
        assert isinstance(types, list)
        return [str(t) for t in types]
    want = exp.get("type")
    if want is None:
        return None
    assert isinstance(want, str)
    return [want]


def _entry_matches(entry: dict[str, Any], exp: Expectation) -> bool:
    """Strict match: content satisfies the expectation AND type matches.

    A primary (full-credit) match requires both the substring and the type
    label to line up. ``None`` wanted-types means type is unconstrained.
    """
    if not _content_matches(entry, exp):
        return False
    wanted = _wanted_types(exp)
    return wanted is None or _type_of(entry) in wanted


def _describe_content(exp: Expectation) -> str:
    """Short label for the content an expectation is looking for."""
    if "contains_any" in exp:
        needles = exp["contains_any"]
        assert isinstance(needles, list)
        return "/".join(str(s) for s in needles)
    return str(exp.get("contains", ""))


def _flexible_match(
    entries: list[dict[str, Any]], expectation: Expectation | ContainsAny
) -> tuple[bool, str | None]:
    """Resolve one (possibly alternative-list) expectation against entries.

    Returns ``(matched, warning)``:

    * **Primary match** — some entry satisfies type *and* content. Returns
      ``(True, None)``: full credit, no warning.
    * **Type-flexible match** — no entry matches the wanted type, but some
      entry's content matches under *any* type. Returns ``(True, warning)``:
      still counts as matched, with a type-mismatch warning string.
    * **No match** — content not found anywhere. Returns ``(False, None)``.
    """
    alternatives = expectation if isinstance(expectation, list) else [expectation]

    # Primary: type + content both line up for some alternative/entry pair.
    for alt in alternatives:
        if any(_entry_matches(e, alt) for e in entries):
            return True, None

    # Type-flexible fallback: content matches under a different type label.
    for alt in alternatives:
        for entry in entries:
            if _content_matches(entry, alt):
                wanted = _wanted_types(alt)
                if wanted is None:
                    # No type constraint to violate — treat as a primary match.
                    return True, None
                got = _type_of(entry)
                warning = (
                    f"  ⚠ type mismatch: expected {'/'.join(wanted)!r}, "
                    f"got {got!r} for content matching {_describe_content(alt)!r}"
                )
                return True, warning

    return False, None


def _expectation_satisfied(
    entries: list[dict[str, Any]], expectation: Expectation | ContainsAny
) -> bool:
    """True if any entry satisfies the expectation (type-flexible)."""
    matched, _ = _flexible_match(entries, expectation)
    return matched


def _describe(expectation: Expectation | ContainsAny) -> str:
    """Human-readable label for a (possibly alternative-list) expectation."""
    if isinstance(expectation, list):
        return " | ".join(_describe(alt) for alt in expectation)
    wanted = _wanted_types(expectation)
    t = "/".join(wanted) if wanted else "*"
    return f"{t}:'{_describe_content(expectation)}'"


@dataclass
class ScenarioScore:
    """Result of scoring one scenario's extracted entries against ground truth."""

    name: str
    extracted: int
    recall_hits: int
    recall_total: int
    false_positives: int
    fp_total: int
    cardinality_ok: bool
    passed: bool
    missing: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def recall_str(self) -> str:
        return "-" if self.recall_total == 0 else f"{self.recall_hits}/{self.recall_total}"

    @property
    def fp_str(self) -> str:
        return f"{self.false_positives}/{max(self.fp_total, self.extracted)}"


def score_scenario(scenario: Scenario, entries: list[dict[str, Any]]) -> ScenarioScore:
    """Score extracted ``entries`` against a scenario's ground truth.

    Metrics:

    * **Recall** — fraction of ``must_contain`` expectations satisfied.
    * **False positives** — number of extracted entries that contain a
      ``must_not_contain`` substring (questions, filler, raw code).
    * **Cardinality** — whether ``max_extracted`` (if set) was respected.

    A scenario *passes* when recall is complete, there are zero false
    positives, and the cardinality ceiling holds.
    """
    # Recall. A match is full-credit when type+content line up, or
    # type-flexible (still a hit) when only the content lines up — the latter
    # emits a warning but does not fail the scenario.
    missing: list[str] = []
    recall_hits = 0
    for expectation in scenario.must_contain:
        matched, warning = _flexible_match(entries, expectation)
        if matched:
            recall_hits += 1
            if warning:
                print(warning)
        else:
            missing.append(_describe(expectation))
    recall_total = len(scenario.must_contain)

    # False positives: an extracted entry containing a forbidden substring.
    violations: list[str] = []
    for entry in entries:
        content = entry.get("content") or ""
        for forbidden in scenario.must_not_contain:
            sub = forbidden.get("contains", "")
            if sub and sub.lower() in content.lower():
                violations.append(f"'{sub}' in {content[:50]!r}")
    false_positives = len(violations)

    # Cardinality
    cardinality_ok = True
    if scenario.max_extracted is not None:
        cardinality_ok = len(entries) <= scenario.max_extracted

    recall_ok = recall_hits == recall_total
    passed = recall_ok and false_positives == 0 and cardinality_ok

    notes_parts: list[str] = []
    if missing:
        notes_parts.append("missing: " + ", ".join(missing))
    if violations:
        notes_parts.append("garbage: " + "; ".join(violations))
    if not cardinality_ok:
        notes_parts.append(f"over cap ({len(entries)} > {scenario.max_extracted})")
    notes = " | ".join(notes_parts)

    return ScenarioScore(
        name=scenario.name,
        extracted=len(entries),
        recall_hits=recall_hits,
        recall_total=recall_total,
        false_positives=false_positives,
        fp_total=len(entries),
        cardinality_ok=cardinality_ok,
        passed=passed,
        missing=missing,
        violations=violations,
        notes=notes,
    )


def format_table(scores: list[ScenarioScore]) -> str:
    """Render scored scenarios as a fixed-width summary table."""
    header = f"{'Scenario':<22}{'Extracted':<11}{'Recall':<8}{'FP_rate':<9}{'Notes'}"
    sep = "-" * len(header)
    rows = [header, sep]
    for s in scores:
        status = "PASS" if s.passed else "FAIL"
        note = s.notes or status
        rows.append(
            f"{s.name:<22}{s.extracted:<11}{s.recall_str:<8}{s.fp_str:<9}{status}  {note}".rstrip()
        )
    return "\n".join(rows)
