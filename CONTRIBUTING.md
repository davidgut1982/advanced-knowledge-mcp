# Contributing to Lore

Thank you for contributing to Lore, a FastMCP-based knowledge-base server that powers production knowledge operations. This guide ensures contributions land clean, tested, and safe — protecting a system people depend on daily.

**TL;DR:** Install pre-commit hooks, run tests locally, update snapshots when tool schemas change, ensure CI passes. That's it.

## Philosophy

Lore is a production MCP server. Contributors are welcome at all skill levels — including AI-assisted code — but the bar is practical: does it work, is it tested, and does it break the live KB? We aim for zero regression surprises.

We're not gatekeeping on style polish. We *are* gatekeeping on correctness and test coverage. If your code passes CI and includes tests, it's mergeable. If it doesn't, we'll help you fix it.

## Development Setup

### Prerequisites

- Python 3.11 or 3.12
- `uv` package manager (preferred) or `venv`
- Git with pre-commit hooks

### Option 1: Setup with uv (Recommended)

```bash
git clone https://github.com/davidgut1982/lore-mcp.git
cd lore-mcp
uv sync
uv run pre-commit install
```

This creates a `.venv/` directory, installs dependencies, and sets up pre-commit hooks.

**Important:** `uv sync` creates `.venv/` (not `venv/`). The `make` targets expect `venv/bin/` and will fail after `uv sync`. When using uv, run commands with `uv run` instead:

```bash
uv run pytest tests/ --ignore=tests/e2e/    # Instead of: make test
uv run ruff check                            # Instead of: make lint
uv run ruff format                           # Instead of: make format
```

Or use Option 2 if you prefer `make` targets.

### Option 2: Setup with venv

```bash
git clone https://github.com/davidgut1982/lore-mcp.git
cd lore-mcp
python3.11 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

This creates a `venv/` directory and makes all `make` targets work directly.

### Verify Setup

```bash
make check
```

This runs linting and an imports smoke test. If it passes, you're ready.

## Pre-Commit Hooks

Pre-commit hooks run *before* each commit and catch common issues early. They're installed by the setup commands above.

**What they check:**

| Hook | Purpose |
|------|---------|
| trailing-whitespace, end-of-file-fixer | Whitespace cleanup |
| check-yaml | YAML syntax |
| check-added-large-files | Blocks files >500KB |
| check-merge-conflict | Blocks unresolved merge markers |
| debug-statements | Detects leftover `pdb`, `breakpoint()` |
| black | Code formatting (88-char line length) |
| isort | Import sorting (Black-compatible profile) |
| ruff | Fast linting + auto-fix |
| mypy | Type checking (--ignore-missing-imports) |
| ggshield | GitGuardian secret scan (blocks commits with API keys, credentials, etc.) |

**If a hook fails:**

- Most failures auto-fix. Re-run `git add` and `git commit`.
- `ggshield` blocks commits with secrets. Remove the credential, re-stage, and try again.
- `mypy` type errors require manual fixes.

**ggshield and External Contributors:**

`ggshield` requires a [GitGuardian](https://www.gitguardian.com/) account and `GITGUARDIAN_API_KEY` environment variable. External contributors without one will have every commit blocked. To skip this hook:

```bash
SKIP=ggshield git commit -m "your message"
```

This is safe for local development. CI does not run ggshield.

**To run all hooks manually:**

```bash
pre-commit run --all-files
```

## Testing

Lore has three test layers. All are run by CI; run them locally before pushing.

### Layer 1: Unit + Integration Tests

The primary local test command. Runs unit tests and integration tests together.

```bash
make test
```

Or directly:

```bash
pytest tests/ --ignore=tests/e2e/
```

**What it tests:** Individual functions, tool schemas, parsing, error paths, and in-process MCP tool behavior.

**Note:** This includes `tests/integration/`. Integration tests are not a separate step — they are part of `make test`.

### Layer 2: Integration Tests Only

If you want to run only integration tests:

```bash
pytest tests/integration/
```

This is redundant if you already ran `make test`, which includes them.

### Layer 3: End-to-End Tests

Requires a live Lore instance. Optional locally.

```bash
# Against a local instance
make e2e-local

# Against staging (internal only — see note below)
make e2e-staging
```

**What it tests:** Full request/response cycle against a running server.

**Note on e2e-staging:** This target requires internal DNS resolution of the `lore-staging` hostname and is only accessible from the internal network. External contributors cannot use it. Use `make e2e-local` to test against a local instance.

### Layer 4: Smoke Tests (Post-Deployment Only)

Smoke tests are **not for CI** — they run against live deployed instances after a deployment to verify the critical path works before declaring the release done.

```bash
make smoke LORE_URL=http://<host>:<port>
```

See [Deployment Verification](#deployment-verification) below for when and how to run them. Do not add smoke tests to the CI pipeline — they require a running server with a real database.

### Running All Tests Locally

```bash
make test
```

This single command is sufficient for local validation before pushing. It covers unit and integration tests. End-to-end tests run in CI automatically.

## Testing Methodology — What Tests to Write

Different changes require different test depths. Use this to know what tests to write for your change.

### Logic and Utility Changes

**What:** Pure functions, parsing logic, configuration loading, validation helpers.

**Test with:** Unit tests in `tests/` (not `tests/integration/` or `tests/e2e/`).

```python
def test_validate_search_query_valid():
    result = validate_search_query("author:alice topic:knowledge")
    assert result.is_valid

def test_validate_search_query_invalid():
    with pytest.raises(ValueError, match="unbalanced quotes"):
        validate_search_query('query:"unclosed')
```

### Tool Behavior Changes

**What:** Adding a new MCP tool, modifying tool parameters, changing response fields.

**Test with:** Unit test the logic + integration test the tool call + update snapshots if schema changed.

Example for adding a `min_score` parameter to `kb_search`:
1. Unit test: mock the search function, assert `min_score` filters results
2. Integration test: call the tool via MCP, assert the parameter is respected
3. Update snapshots: `pytest tests/test_tool_schemas.py --snapshot-update`

**Why snapshots matter:** MCP tools are contracts. A parameter name change or new enum value breaks clients without warning. Snapshots make these changes visible.

### New Search Modes or Strategies

**What:** Adding a new `search_mode` value, new ranking algorithm, query expansion.

**Test with:** Unit test the strategy logic + integration test the tool call + e2e test against a live server.

For search tests, assert specific values — not just "no exception":

```python
def test_kb_search_hybrid_mode():
    response = client.kb_search("authentication", search_mode="hybrid")
    assert response["requested_mode"] == "hybrid"
    assert response["search_mode"] in ["hybrid", "fts", "semantic"]  # may fall back
    assert isinstance(response["results"], list)
```

### Bug Fixes

**What:** Fixing a crash, incorrect output, race condition.

**Test with:** A regression test at the lowest layer that would have caught the bug, plus the fix.

If the bug doesn't show up in a test, CI will not prevent it from being reintroduced.

### Infrastructure and Database Changes

**What:** Schema migrations, caching, connection pooling, embedding storage.

**Test with:** Integration tests against the in-process server. E2e tests before releasing.

### The "Did I Write Enough?" Test

Ask: **If this change broke silently in production, what test would have caught it?**

If the answer is "none," write that test.

### Test Quality

Assert specific values, not just that no exception was raised:

```python
# Bad
def test_search():
    result = kb_search("topic:knowledge")
    assert result is not None

# Good
def test_search():
    result = kb_search("topic:knowledge")
    assert result["status"] == "success"
    assert len(result["results"]) > 0
    assert "search_mode" in result
    assert result["search_mode"] in ["fts", "semantic", "hybrid"]
```

## Snapshot Tests — Critical

Lore uses [syrupy](https://github.com/syrupy-project/syrupy) for snapshot testing of MCP tool schemas. Snapshots are stored in `tests/__snapshots__/` as `.ambr` files.

**Why:** MCP tool parameter names, types, and enum values are a public contract. Snapshots catch unintentional breaking changes.

### When to Update Snapshots

Update snapshots when you intentionally change:
- A tool parameter name or type
- An enum value (adding or removing a value like `search_mode="hybrid"`)
- A tool description

### How to Update Snapshots

```bash
pytest tests/test_tool_schemas.py --snapshot-update
git diff tests/__snapshots__/   # Review the change is intentional
git add tests/__snapshots__/
git commit -m "chore: update tool schema snapshots"
```

**CRITICAL:** Commit the `.ambr` files. If you push without committing updated snapshots, CI will fail for everyone.

If the diff looks wrong, revert your tool change rather than updating the snapshot.

## Path to Main — The Full Merge Workflow

From "I have a change" to "it's merged." Follow these steps in order.

### Step 1: Start from a Fresh Branch

```bash
git checkout main
git pull origin main
git checkout -b feature/your-feature-name
```

Always branch from the latest main to avoid merge conflicts.

Branch naming: `feature/`, `fix/`, or `chore/` prefixes. Examples: `feature/semantic-boost`, `fix/embedding-race`, `chore/update-snapshots`.

### Step 2: Develop and Test Locally

Run tests frequently:

```bash
make test
```

If you modify a tool's schema, update snapshots before committing:

```bash
pytest tests/test_tool_schemas.py --snapshot-update
git add tests/__snapshots__/
```

### Step 3: Final Local Check

```bash
make check    # Linting, type checking, imports
make test     # Unit + integration tests
git status    # No uncommitted changes
```

All three must pass before pushing.

### Step 4: Push and Open a Pull Request

```bash
git push origin feature/your-feature-name
```

On GitHub, open a Pull Request against `main`. Write a clear title and description: what changed, why, and any testing notes.

### Step 5: CI Must Pass

GitHub Actions runs automatically. Three required jobs must go green:

| Job | What It Tests | Python |
|-----|--------------|--------|
| `test (3.11)` | Unit + integration tests | 3.11 |
| `test (3.12)` | Unit + integration tests | 3.12 |
| `integration` | Full integration suite | 3.11 |

Watch the PR page. If any job fails:
1. Click the failing job in GitHub Actions to read the error
2. Reproduce locally: `make test` or `pytest tests/path/to/failing_test.py -v`
3. Fix, commit, push — CI re-runs automatically

Common CI fixes:
- Snapshot mismatch: `pytest tests/test_tool_schemas.py --snapshot-update && git add tests/__snapshots__/ && git commit -m "chore: update snapshots" && git push`
- Type error: fix locally, push
- Import error: add the missing dependency

### Step 6: Optional — Staging Validation (Internal Only)

For internal contributors, once CI is green, you can run end-to-end tests against the live staging server:

```bash
make e2e-staging
```

This requires internal network access. External contributors: skip this step — CI is sufficient.

### Step 7: Merge

Once all three CI jobs are green, click "Merge pull request" on GitHub.

**Direct pushes to `main` are blocked by branch protection.** This is intentional — it ensures all changes go through CI.

### Why Each Step Matters

| Step | Why |
|------|-----|
| Fresh branch from main | Prevents merge conflicts; you're testing against the latest code |
| `make test` locally | Faster feedback than waiting for CI |
| Committing snapshots | Forgetting this breaks CI for the entire team |
| Two Python versions in CI | Catches version compatibility issues |
| Branch protection | Prevents broken commits from landing on main |

## Release Process

This section is for maintainers — including AI assistants acting as maintainers. Follow these steps to publish a new version of `lore-knowledge-mcp` to PyPI.

### When to Release

Release after meaningful changes are merged to `main`. There is no fixed schedule. Good triggers:
- New features ready
- Critical bugs fixed
- Batch of improvements ready to ship

### Version Numbering (Semantic Versioning)

| Bump | When | Example |
|------|------|--------|
| **Patch** (0.8.6 → 0.8.7) | Bug fixes, internal refactors, test additions | `fix: resolve race in embedding backfill` |
| **Minor** (0.8.x → 0.9.0) | New features, new tool parameters, new search modes | `feat: add semantic search mode` |
| **Major** (0.x → 1.0) | Breaking changes to MCP tool interface | Removing a parameter or changing response format |

**Examples:**
- Fixing a crash → `0.8.6` → `0.8.7`
- Adding `min_score` parameter to `kb_search` → `0.8.6` → `0.9.0`
- Removing `exact_match` parameter → `0.9.0` → `1.0.0`

### Release Steps

**1. Confirm main is green**

Check GitHub Actions. All three jobs on `main` must pass.

**2. Create a release branch**

```bash
git checkout main
git pull origin main
git checkout -b chore/release-X.Y.Z
```

**3. Bump the version**

Edit `pyproject.toml`, find and update the `version` field:

```toml
[project]
name = "lore-knowledge-mcp"
version = "0.9.0"  # ← change this
```

**4. Update CHANGELOG.md**

Add a new section at the top:

```markdown
## [0.9.0] - YYYY-MM-DD

### Added
- Semantic search mode for kb_search tool

### Fixed
- Race condition in embedding backfill

### Changed
- Improved reranking algorithm
```

Group entries under **Added**, **Fixed**, **Changed**, **Removed**.

**5. Commit and push**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: release X.Y.Z"
git push origin chore/release-X.Y.Z
```

**6. Open PR, wait for CI, merge**

Open a PR from `chore/release-X.Y.Z` to `main`. Once CI passes, merge.

**7. Create a GitHub Release (this publishes to PyPI)**

After the release PR is merged to main:

1. Go to GitHub → Releases → "Draft a new release"
2. Tag: `vX.Y.Z` (include the `v`)
3. Title: `Release X.Y.Z`
4. Body: paste the CHANGELOG.md section for this release
5. Click "Publish release"

This triggers `publish.yml`, which builds the package with `uv build` and publishes to PyPI via trusted publishing (no API key needed). It completes in 2–3 minutes.

**8. Deploy and verify each node**

After deploying to each node, run the smoke test before moving on:

```bash
# Staging (CT 200, port 5556)
make smoke LORE_URL=http://<staging-node>:5556

# Production (CT 121, port 5555)
make smoke LORE_URL=http://<prod-node>:5555
```

All steps must print **PASS** and the script must exit 0 before you declare the deployment done.

**Rules:**
1. Do not deploy to prod until the staging smoke test passes.
2. If any step fails, treat the deployment as failed — roll back and investigate.
3. The smoke test exercises the live database, not a mock. A passing run confirms the DB connection, schema, and write/read/delete path are all functional.

**9. Verify PyPI publication**

```bash
pip install lore-knowledge-mcp==X.Y.Z
python -c "import lore; print(lore.__version__)"
```

Should print the new version.

### Rollback

If a release breaks production:

1. Revert the release commit on main: `git revert <commit-hash>`
2. Push, open PR, merge the revert
3. Fix the issue, cut a new patch release

The broken version remains on PyPI but won't be installed unless explicitly requested.

## Deployment Verification

After every deployment — staging or production — run the smoke test before declaring it done.

```bash
# Generic (override URL as needed)
make smoke LORE_URL=http://<node>:<port>

# Convenience targets
make smoke-staging   # CT 200, port 5556
make smoke-prod      # CT 121, port 5555
```

The smoke test (see `scripts/smoke_test.py`) runs five steps:

| Step | What it checks |
|------|----------------|
| `GET /health` | Server process is up |
| `kb_add` | DB connection + write path |
| `kb_search` | FTS indexing + search path |
| `kb_get` | Direct lookup by ID |
| `kb_get_batch` | Batch-get endpoint |

Cleanup (`kb_delete`) runs best-effort after every test run.

**Rules:**
- All steps must print **PASS** before the deployment is declared done.
- Do not deploy to prod until staging smoke passes.
- If any step fails, roll back and investigate — do not push forward.

## Makefile Reference

| Target | What it does |
|--------|--------------|
| `make lint` | ruff check + format check |
| `make format` | ruff auto-format |
| `make fix` | ruff --fix + ruff format |
| `make check` | lint + import smoke test |
| `make test` | pytest tests/ --ignore=tests/e2e/ (unit + integration) |
| `make e2e-staging` | End-to-end tests against lore-staging (internal only) |
| `make e2e-local` | End-to-end tests against local instance |
| `make soak-staging` | Long-running soak test (internal only) |
| `make smoke` | Post-deployment smoke test (LORE_URL=...) |
| `make smoke-staging` | Smoke test against staging (CT 200, port 5556) |
| `make smoke-prod` | Smoke test against production (CT 121, port 5555) |

**Note:** `make` targets use `venv/bin/`. If you set up with `uv sync`, use `uv run` equivalents instead.

## PR Checklist

Before opening a PR:

- [ ] `make check` passes
- [ ] `make test` passes
- [ ] If tool schema changed: `pytest tests/test_tool_schemas.py --snapshot-update` run and `.ambr` files committed
- [ ] No leftover `pdb` or `breakpoint()` statements
- [ ] No secrets or credentials in code
- [ ] Bug fix: includes a regression test
- [ ] New feature: includes tests for happy path and error cases

**Note on smoke tests:** Smoke tests (`make smoke`) are for post-deployment verification against live instances. They are not part of the PR checklist and should not be run in CI — they require a running server with a real database.

## CI Requirements

Three required jobs must pass before merging to `main`.

| Job | What It Does | Python |
|-----|--------------|--------|
| `test (3.11)` | Unit + integration tests | 3.11 |
| `test (3.12)` | Unit + integration tests | 3.12 |
| `integration` | Full integration suite | 3.11 |

Branch protection is active. Direct pushes to `main` are blocked. PRs must pass all three jobs to merge.

## Common Pitfalls

### Snapshots not updated

**Symptom:** CI fails on `test_tool_schemas.py` — "Snapshot does not match."

```bash
pytest tests/test_tool_schemas.py --snapshot-update
git add tests/__snapshots__/*.ambr
git commit -m "chore: update tool schema snapshots"
git push
```

### ggshield blocks commit

**Symptom:** `ggshield` fails — "Secrets detected."

If you have actual secrets in code: remove them. If you don't have a GitGuardian account:

```bash
SKIP=ggshield git commit -m "your message"
```

### `make` targets fail after `uv sync`

**Symptom:** `venv/bin/pytest: No such file or directory`

`uv sync` creates `.venv/`, not `venv/`. Use `uv run` equivalents:

```bash
uv run pytest tests/ --ignore=tests/e2e/
uv run ruff check
```

### Import check fails

**Symptom:** `make check` fails with import errors.

```bash
python -c "import lore.something"   # See the actual error
uv add package-name                  # Add missing dependency
make check
```

## Reporting Issues

Open an issue at https://github.com/davidgut1982/lore-mcp/issues with:

- **Title:** Specific and descriptive. `"kb_search returns 0 results on hybrid mode"` not `"search broken"`
- **Steps to reproduce:** Exact input, expected output, actual output
- **Environment:** Python version, OS, how you installed (uv/venv/pip)
- **Logs:** Run with `LORE_DEBUG=true` if applicable

## Branch and Commit Conventions

**Branch naming:** `feature/`, `fix/`, or `chore/` prefix. Examples: `feature/semantic-boost`, `fix/embedding-race`, `chore/update-snapshots`.

**Commit messages:** Use imperative mood.

| Prefix | Use for |
|--------|---------|
| `feat:` | New functionality |
| `fix:` | Bug fixes |
| `chore:` | Maintenance (snapshots, deps, version bumps) |
| `docs:` | Documentation only |
| `refactor:` | Code restructuring without behavior change |
| `test:` | Adding or fixing tests |

Examples: `feat: add semantic search to kb_search`, `fix: resolve race in embedding backfill`, `chore: update snapshots for enum change`.
