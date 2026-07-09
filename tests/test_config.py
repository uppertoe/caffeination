"""Settings guardrails."""

import pytest


def _create_app_with(monkeypatch, **env):
    # Import first: app.main builds a module-level app at import time, and the
    # conftest's safe SECRET_KEY must still be in force for that.
    from app.main import create_app

    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        return create_app()
    finally:
        get_settings.cache_clear()


def test_default_secret_key_refused_outside_debug(monkeypatch):
    """Identity cookies signed with the known dev key are forgeable — the
    app must refuse to start rather than run production with it."""
    from app.config import DEV_SECRET_KEY

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _create_app_with(monkeypatch, SECRET_KEY=DEV_SECRET_KEY, DEBUG=None)


def test_default_secret_key_allowed_in_debug(monkeypatch):
    from app.config import DEV_SECRET_KEY

    _create_app_with(monkeypatch, SECRET_KEY=DEV_SECRET_KEY, DEBUG="true")
