.PHONY: lint format check fix test e2e-staging e2e-local soak-staging smoke smoke-staging smoke-prod

lint:
	venv/bin/ruff check src/ tests/
	venv/bin/ruff format src/ tests/ --check

format:
	venv/bin/ruff format src/ tests/

fix:
	venv/bin/ruff check src/ tests/ --fix
	venv/bin/ruff format src/ tests/

check: lint
	venv/bin/python3 -c "from lore.server import main; print('import OK')"

test:
	venv/bin/pytest tests/ -v --ignore=tests/e2e/

# ---------------------------------------------------------------------------
# End-to-end tests (require a live Lore instance)
# ---------------------------------------------------------------------------

e2e-staging:
	LORE_E2E_URL=http://lore-staging:5556 venv/bin/pytest tests/e2e/ -v

e2e-local:
	LORE_E2E_URL=http://localhost:5556 venv/bin/pytest tests/e2e/ -v

# ---------------------------------------------------------------------------
# Soak test (long-running; use tmux or nohup)
# ---------------------------------------------------------------------------

soak-staging:
	LORE_E2E_URL=http://lore-staging:5556 venv/bin/python3 -m tests.e2e.soak_runner --duration 24h

# ---------------------------------------------------------------------------
# Smoke tests — post-deployment verification against live instances
# Run after every deployment before declaring the release done.
# LORE_URL can be overridden: make smoke LORE_URL=http://192.168.1.21:5555
# ---------------------------------------------------------------------------

LORE_URL ?= http://localhost:5555

smoke:  ## Run smoke test against a live instance (LORE_URL defaults to http://localhost:5555)
	venv/bin/python scripts/smoke_test.py --url $(LORE_URL)

smoke-staging:  ## Run smoke test against staging (CT 200, port 5556)
	venv/bin/python scripts/smoke_test.py --url http://lore-staging:5556

smoke-prod:  ## Run smoke test against production (CT 121, port 5555)
	venv/bin/python scripts/smoke_test.py --url http://lore-prod:5555
