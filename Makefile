.PHONY: lint format check fix test e2e-staging e2e-local soak-staging

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
