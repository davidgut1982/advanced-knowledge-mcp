"""pytest configuration and shared fixtures for end-to-end tests.

All e2e tests require a live Lore instance.  The ``LORE_E2E_URL`` environment
variable must be set to the base URL of that instance, e.g.::

    LORE_E2E_URL=http://lore-staging:5555 pytest tests/e2e/ -v

When the variable is absent, ``pytest_collection_modifyitems`` attaches a
``skip`` marker to every collected test, so ``pytest --collect-only`` still
works in CI without a server.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

# Use a relative import so ``tests/`` does not need to be an installed package.
# pytest adds the rootdir to sys.path which makes this work with --rootdir or
# when run from the project root as:  pytest tests/e2e/
try:
    from .client import LoreClient
except ImportError:  # fallback when run as a plain module (e.g. during CI debugging)
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from tests.e2e.client import LoreClient  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Environment gate
# ---------------------------------------------------------------------------

LORE_E2E_URL: str | None = os.environ.get("LORE_E2E_URL")


def pytest_collection_modifyitems(
    config: pytest.Config,  # noqa: ARG001
    items: list[pytest.Item],
) -> None:
    """Skip all e2e tests when no live endpoint is configured."""
    if LORE_E2E_URL:
        return

    skip_marker = pytest.mark.skip(
        reason="LORE_E2E_URL not set; e2e tests require a live Lore endpoint"
    )
    for item in items:
        # Only skip items that live inside this package; exempt offline tests
        # (e.g. test_extraction_e2e_offline.py) that use mocked HTTP.
        path = str(item.fspath)
        if "e2e" in path and "_offline" not in path:
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def lore_url() -> str:
    """Return the base URL of the Lore instance under test.

    Raises:
        pytest.skip.Exception: When ``LORE_E2E_URL`` is not configured.
    """
    if not LORE_E2E_URL:
        pytest.skip("LORE_E2E_URL not set")
    return LORE_E2E_URL


@pytest.fixture(scope="session")
def session_client(lore_url: str) -> Iterator[LoreClient]:
    """Long-lived :class:`LoreClient` shared across the entire test session.

    Prefer the function-scoped ``client`` fixture for most tests.  Use this
    fixture only when you need session-level caching (e.g. ``test_smoke.py``).
    """
    with LoreClient(lore_url, timeout=60.0) as c:
        yield c


# ---------------------------------------------------------------------------
# Function-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(lore_url: str) -> Iterator[LoreClient]:
    """Fresh :class:`LoreClient` for each test function.

    Yields the client and closes it after the test completes (pass or fail).
    """
    with LoreClient(lore_url, timeout=30.0) as c:
        yield c


@pytest.fixture
def cleanup_topic(client: LoreClient) -> Iterator[str]:
    """Provide a unique topic name and clean up all entries after the test.

    The topic is ``e2e-test-<8-hex-chars>``.  After the test (pass *or* fail)
    the fixture searches for every entry under that topic and deletes it on a
    best-effort basis — failures during teardown are logged but not re-raised.

    Yields:
        A unique topic string safe for the test to use.
    """
    topic = f"e2e-test-{uuid.uuid4().hex[:8]}"
    yield topic

    # Teardown: delete all entries created under this topic
    try:
        result = client.kb_list(topic=topic)
        entries = result.get("entries") or result.get("results") or []
        for entry in entries:
            entry_id = entry.get("kb_id")
            if entry_id:
                try:
                    client.kb_delete(entry_id, confirm=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"[cleanup] failed to delete {entry_id}: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"[cleanup] teardown error for topic {topic!r}: {exc}")


@pytest.fixture
def unique_id() -> str:
    """Return a unique hex string suitable for use in titles / content."""
    return uuid.uuid4().hex


@pytest.fixture
def slow_client(lore_url: str) -> Iterator[LoreClient]:
    """Fresh :class:`LoreClient` with extended timeout for long-running operations.

    Use this fixture for tests that call ``kb_backfill_embeddings`` or other
    operations that may take several minutes on a large knowledge base.
    """
    with LoreClient(lore_url, timeout=300.0) as c:
        yield c
