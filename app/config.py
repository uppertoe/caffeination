from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Per-deployment branding. Override with the APP_NAME / TAGLINE env vars.
    # app_name renders as "name@domain" in the header (split on the first @).
    app_name: str = "caffeine@RCH"
    tagline: str = "Enabling safe and smooth anaesthesia since 2026"
    debug: bool = False

    # Where the SQLite file lives. Override with DATABASE_URL in prod.
    database_url: str = "sqlite:///./data/coffee.db"

    # Used to sign the identity cookie. MUST be overridden in production.
    secret_key: str = "dev-only-change-me"
    cookie_name: str = "coffee_rch_id"
    cookie_max_age: int = 60 * 60 * 24 * 365  # 1 year

    base_dir: Path = Path(__file__).resolve().parent


@lru_cache
def get_settings() -> Settings:
    return Settings()
