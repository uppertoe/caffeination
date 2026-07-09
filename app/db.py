from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    # SQLite-specific: allow connection sharing across threads.
    connect_args = (
        {"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {}
    )
    return create_engine(
        settings.database_url, echo=settings.debug, connect_args=connect_args
    )


def init_db() -> None:
    settings = get_settings()
    if settings.database_url.startswith("sqlite:///"):
        db_path = Path(settings.database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(get_engine())
    _ensure_last_active_column()


def _ensure_last_active_column() -> None:
    """One-off in-place migration: create_all never alters existing tables,
    so DBs created before User.last_active_at need the column added. Fresh
    DBs get it from the model and this is a no-op. (Still no Alembic — one
    guarded ADD COLUMN doesn't justify it; revisit if these accumulate.)"""
    engine = get_engine()
    with engine.connect() as conn:
        cols = [
            row[1]
            for row in conn.exec_driver_sql('PRAGMA table_info("user")').fetchall()
        ]
        if "last_active_at" not in cols:
            conn.exec_driver_sql(
                'ALTER TABLE "user" ADD COLUMN last_active_at TIMESTAMP'
            )
            conn.exec_driver_sql(
                'UPDATE "user" SET last_active_at = created_at'
            )
            conn.commit()


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
