"""Keep the test suite isolated from a developer's configured production store."""

import pytest


@pytest.fixture(autouse=True)
def isolate_test_store(monkeypatch):
    """Prevent tests from writing to the developer's shared PostgreSQL store."""
    monkeypatch.delenv("AUTORESEARCH_DATABASE_URL", raising=False)
