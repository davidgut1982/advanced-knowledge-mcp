"""
Turn filtering for memory extraction.

Reduces token cost and improves extraction quality by removing noise
(code blocks, short filler turns, tool output blobs) before sending
conversation turns to the LLM.
"""

import re

# Defaults — overridable via config
DEFAULT_MAX_TURNS = 20
DEFAULT_MIN_TURN_CHARS = 20  # turns shorter than this after cleaning are skipped
DEFAULT_MAX_TURN_CHARS = 1200  # per-turn content is truncated to this length


def filter_turns(
    turns: list[dict],
    max_turns: int = DEFAULT_MAX_TURNS,
    min_turn_chars: int = DEFAULT_MIN_TURN_CHARS,
    max_turn_chars: int = DEFAULT_MAX_TURN_CHARS,
) -> list[dict]:
    """
    Prepare conversation turns for memory extraction.

    Steps:
    1. Take the last max_turns turns (most recent context)
    2. Clean each turn's content (strip code, truncate)
    3. Drop turns that are too short after cleaning (noise)

    Returns a new list of dicts with keys: role, content.
    Original turn dicts are not mutated.
    """
    # Take the last max_turns
    recent = turns[-max_turns:] if len(turns) > max_turns else turns

    filtered = []
    for turn in recent:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        if not isinstance(content, str):
            # Some frameworks store content as list of blocks — flatten to text
            if isinstance(content, list):
                content = " ".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                )
            else:
                # Non-string, non-list content has no recoverable text value;
                # str(None) -> "None" would otherwise leak into extraction.
                content = ""

        cleaned = _clean_content(content, max_turn_chars)

        if len(cleaned.strip()) < min_turn_chars:
            continue  # skip noise turns

        filtered.append({"role": role, "content": cleaned})

    return filtered


def _clean_content(content: str, max_chars: int) -> str:
    """
    Clean a single turn's content for extraction.

    - Strip fenced code blocks (``` ... ```)
    - Strip inline code spans (` ... `)  — keep short ones, remove long ones
    - Strip tool call / JSON blob sections
    - Truncate to max_chars
    """
    # Remove fenced code blocks (```lang\n...\n```)
    content = re.sub(r"```[\s\S]*?```", "[code]", content)

    # Remove long inline code (>40 chars) — short ones may be meaningful (e.g. `True`)
    content = re.sub(r"`[^`]{40,}`", "[code]", content)

    # Remove JSON-like blobs (lines that are mostly punctuation/braces)
    lines = content.splitlines()
    prose_lines = [line for line in lines if not _is_json_line(line)]
    content = "\n".join(prose_lines)

    # Collapse multiple blank lines
    content = re.sub(r"\n{3,}", "\n\n", content)

    # Truncate
    if len(content) > max_chars:
        content = content[:max_chars] + "…"

    return content.strip()


def _is_json_line(line: str) -> bool:
    """
    Heuristic: is this line mostly JSON/code rather than prose?
    Triggers if >60% of non-space chars are punctuation typical of JSON/code.
    """
    stripped = line.strip()
    if not stripped or len(stripped) < 5:
        return False
    code_chars = sum(1 for c in stripped if c in "{}[](),;:\"'=><|\\")
    return (code_chars / len(stripped)) > 0.60
