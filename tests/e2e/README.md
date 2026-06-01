# Extraction eval harness

End-to-end evaluation for the auto-memory extraction pipeline
(`src/lore/extraction/`). It exercises the real orchestrator
`extract_and_store(turns, db_client, config)` — filtering, LLM parse,
confidence/durability gate, dedup, and KB writes — against a set of
ground-truth conversation scenarios.

There are two execution modes that share the **same scenarios and the same
scorer**, so a green offline run and a green live run mean the same thing:

| Mode | File | Needs API key? | Runs in CI? |
|------|------|----------------|-------------|
| Offline golden | `test_extraction_e2e_offline.py` | No (HTTP mocked) | Yes |
| Live eval | `eval_extraction.py` | Yes | No (manual) |

## Files

- `scenarios.py` — five `Scenario` fixtures (A–E) with ground truth.
- `harness.py` — `FakeDbClient` + `score_scenario()` + `format_table()`.
- `eval_extraction.py` — standalone live eval script (real OpenRouter/Cerebras).
- `test_extraction_e2e_offline.py` — CI-safe golden pytest suite.

> The rest of this directory (`client.py`, `conftest.py`, `test_*.py`) is the
> pre-existing HTTP e2e suite that targets a live Lore server via `LORE_E2E_URL`.
> Those tests are skipped when `LORE_E2E_URL` is unset; the offline extraction
> suite is explicitly exempted from that skip (see `conftest.py`
> `_OFFLINE_MODULES`).

## Offline tests (CI-safe)

No API key, no network, no live server. `httpx.AsyncClient.post` is mocked to
return a per-scenario `GOLDEN_*` chat-completions envelope.

```bash
python -m pytest tests/e2e/test_extraction_e2e_offline.py -v
```

These assert the pipeline plumbing end-to-end: correct number of writes, the
`auto-memory` topic, the tag contract (`source:auto-extracted`, `type:…`,
`confidence:…`), substring content checks, and that the noise scenario writes
nothing.

> Async note: this project does not install `pytest-asyncio` and avoids
> `async def test_*`. These tests drive async code with `asyncio.run()` inside
> sync test functions, matching `tests/test_extraction_orchestrator.py`.

## Online eval (manual)

Runs the real model. Costs a few tokens per scenario.

```bash
# OpenRouter (default): meta-llama/llama-3.1-8b-instruct
OPENROUTER_API_KEY=... python tests/e2e/eval_extraction.py

# Cerebras: gpt-oss-120b
CEREBRAS_API_KEY=... python tests/e2e/eval_extraction.py --provider cerebras

# Subset of scenarios
OPENROUTER_API_KEY=... python tests/e2e/eval_extraction.py --scenario A,D

# Cheap smoke test: full LLM + dedup path, no KB writes
OPENROUTER_API_KEY=... python tests/e2e/eval_extraction.py --dry-run
```

The eval uses `confidence_threshold=0.65` (below the production default of
0.75) so more near-boundary candidates surface for inspection. Exit code is
`1` if any scenario fails, `0` if all pass, `2` on usage errors (missing key,
bad scenario).

Sample output:

```
Scenario              Extracted  Recall  FP_rate  Notes
-------------------------------------------------------
progressive_personal  4          4/4     0/4      PASS
research_architecture 3          3/3     0/3      PASS
debug_session         2          2/2     0/2      PASS
noise_only            0          -       0/0      PASS
code_heavy            1          1/1     0/1      PASS
```

## Interpreting recall / precision

- **Recall** (`hits/total`) — of the scenario's `must_contain` expectations,
  how many were satisfied by some extracted entry. `-` means the scenario has
  no positive expectations (e.g. `noise_only`).
- **FP_rate** (`fp/extracted`) — extracted entries whose content contains a
  `must_not_contain` substring. These are hallucinations or garbage: questions
  extracted as facts, assistant filler, or raw code/SQL. A single false
  positive fails the scenario.
- **Cardinality** — `noise_only` sets `max_extracted`; exceeding it fails the
  scenario even with zero false positives. This is the precision gate against
  the model inventing memories from filler.

A scenario **passes** only when recall is complete, FP rate is zero, and the
cardinality ceiling holds.

## What "the right things" means

A good extraction is:

- **Durable** — true beyond this session (a job, a homelab topology, a stable
  preference), not transient state. Non-durable candidates are dropped by the
  orchestrator's `durable` gate.
- **Actionable** — worth recalling later to personalize future help.
- **Specific** — "is a backend engineer working in Python/Go", not "likes
  computers".
- **Grounded** — stated by the user/assistant in the transcript, not inferred
  or hallucinated. Questions ("What's your storage setup?") and filler
  ("Nice", "Got it") are not facts.

The scenarios encode these as ground truth: `must_contain` rewards durable,
specific, grounded facts; `must_not_contain` penalizes questions, filler, and
raw code leaking into memories.

## Adding a new scenario

1. In `scenarios.py`, add a `Scenario(...)` with `turns`, `must_contain`,
   `must_not_contain`, and (optionally) `max_extracted`. Register it in
   `SCENARIOS` under the next letter key.
   - `must_contain` entries are `{"type": ..., "contains": ...}`. Use a **list**
     of such dicts when the model could reasonably phrase a fact two ways (any
     alternative satisfies the expectation, e.g. `Kubernetes`/`k8s`).
2. In `test_extraction_e2e_offline.py`, add a `GOLDEN_<NAME>` response (a
   realistic LLM JSON envelope via `_envelope([...])`) and register it in
   `GOLDEN_BY_KEY`. The `test_all_scenarios_pass_against_golden` sweep will
   pick it up automatically.
3. Run `python -m pytest tests/e2e/test_extraction_e2e_offline.py -v` and,
   optionally, the live eval to see how the real model scores.
