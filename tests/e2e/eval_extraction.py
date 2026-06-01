"""
End-to-end extraction eval harness.

Usage:
    OPENROUTER_API_KEY=... python tests/e2e/eval_extraction.py [--scenario A,B,C,D,E] [--dry-run]

Runs each scenario through the real extraction pipeline (real OpenRouter/Cerebras
call) and scores the result against ground truth. Prints a summary table with
recall / false-positive-rate per scenario.

This script is standalone — it does NOT require pytest. It drives the async
``extract_and_store`` entry point via ``asyncio.run`` and reuses the same
``FakeDbClient`` and scoring code as the offline golden suite
(:mod:`tests.e2e.harness`), so live and offline runs are judged identically.

Provider/model selection mirrors ``ExtractionClient``:

* ``--provider openrouter`` (default) → ``meta-llama/llama-3.1-8b-instruct``
  (needs ``OPENROUTER_API_KEY``).
* ``--provider cerebras`` → ``gpt-oss-120b`` (needs ``CEREBRAS_API_KEY``).

``--dry-run`` exercises the full LLM + dedup path but skips KB writes (so the
``added`` list stays empty and nothing is scored as inserted) — useful for a
cheap "does the API key work and return JSON" smoke test.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Allow running as a plain script: `python tests/e2e/eval_extraction.py`.
# When executed directly the package context is absent, so add the repo root to
# sys.path and import via the fully-qualified package path.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.e2e.harness import FakeDbClient, ScenarioScore, format_table, score_scenario
    from tests.e2e.scenarios import SCENARIOS, Scenario
else:
    from .harness import FakeDbClient, ScenarioScore, format_table, score_scenario
    from .scenarios import SCENARIOS, Scenario

from lore.extraction import extract_and_store  # noqa: E402  (after sys.path fix)

# Lower than the production default (0.75) so the eval surfaces more candidates
# and we can see what the model *would* produce near the boundary.
EVAL_CONFIDENCE_THRESHOLD = 0.65

_API_KEY_BY_PROVIDER = {
    "openrouter": "OPENROUTER_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
}
_DEFAULT_MODEL_BY_PROVIDER = {
    "openrouter": "meta-llama/llama-3.1-8b-instruct",
    "cerebras": "gpt-oss-120b",
}


def build_config(provider: str, model: str | None, dry_run: bool) -> dict:
    """Construct an ``auto_extract`` config block for the eval run."""
    return {
        "auto_extract": {
            "enabled": True,
            "dry_run": dry_run,
            "provider": provider,
            "model": model or _DEFAULT_MODEL_BY_PROVIDER[provider],
            "confidence_threshold": EVAL_CONFIDENCE_THRESHOLD,
            "min_turns": 3,
            # Keep dedup quiet — FakeDbClient.kb_search returns [] anyway.
            "dedup_similarity_threshold": 0.12,
        }
    }


async def run_scenario(scenario: Scenario, config: dict) -> tuple[ScenarioScore, FakeDbClient]:
    """Run one scenario through the real pipeline and score the result."""
    db = FakeDbClient()
    await extract_and_store(scenario.turns, db, config)
    score = score_scenario(scenario, db.added)
    return score, db


async def run_all(keys: list[str], config: dict, *, verbose: bool) -> list[ScenarioScore]:
    """Run the selected scenarios sequentially and return their scores."""
    scores: list[ScenarioScore] = []
    for key in keys:
        scenario = SCENARIOS[key]
        score, db = await run_scenario(scenario, config)
        scores.append(score)
        if verbose:
            print(f"\n=== {key}: {scenario.name} ===")
            for entry in db.added:
                t = next((tag for tag in entry["tags"] if tag.startswith("type:")), "type:?")
                print(f"  [{t.split(':', 1)[1]:<12}] {entry['content']}")
            if not db.added:
                print("  (no entries written)")
    return scores


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto-memory extraction eval harness.")
    parser.add_argument(
        "--scenario",
        default="A,B,C,D,E",
        help="Comma-separated scenario keys to run (default: all → A,B,C,D,E).",
    )
    parser.add_argument(
        "--provider",
        default="openrouter",
        choices=sorted(_API_KEY_BY_PROVIDER),
        help="LLM provider (default: openrouter).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override model id (default: provider-specific small instruct model).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run LLM + dedup but skip KB writes (cheap API smoke test).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-scenario extracted-entry dump.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    keys = [k.strip().upper() for k in args.scenario.split(",") if k.strip()]
    unknown = [k for k in keys if k not in SCENARIOS]
    if unknown:
        print(f"Unknown scenario key(s): {unknown}. Valid: {sorted(SCENARIOS)}", file=sys.stderr)
        return 2

    api_key_var = _API_KEY_BY_PROVIDER[args.provider]
    if not os.getenv(api_key_var):
        print(
            f"{api_key_var} is not set. Export it to run the live eval, e.g.:\n"
            f"    {api_key_var}=... python tests/e2e/eval_extraction.py",
            file=sys.stderr,
        )
        return 2

    config = build_config(args.provider, args.model, args.dry_run)
    scores = asyncio.run(run_all(keys, config, verbose=not args.quiet))

    print("\n" + format_table(scores))

    if args.dry_run:
        print("\n[dry-run] KB writes were skipped; recall/FP reflect zero inserts by design.")
        return 0

    failed = [s.name for s in scores if not s.passed]
    if failed:
        print(f"\nFAILED scenarios: {', '.join(failed)}")
        return 1
    print("\nAll scenarios passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
