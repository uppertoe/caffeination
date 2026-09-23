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
        # The startup warm-up renders a page per create_app(); skip it here
        # (tests/test_warm_up.py exercises it explicitly).
        monkeypatch.setenv("WARM_ON_STARTUP", "false")

        from app.config import get_settings
        from app.db import get_engine

        get_settings.cache_clear()
        get_engine.cache_clear()
        yield
        get_settings.cache_clear()
        get_engine.cache_clear()
