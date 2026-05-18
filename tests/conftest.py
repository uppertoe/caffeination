import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_sqlite(monkeypatch):
    """Point each test at a throwaway SQLite file so they never share state."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("SECRET_KEY", "test-secret")
        # Drop the cached settings so the env vars are picked up.
        from app.config import get_settings
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()
