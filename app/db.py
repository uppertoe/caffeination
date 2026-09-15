from collections.abc import Iterator
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

# Per-request SQL statement counter, surfaced in the Server-Timing header by
# app.timing. A one-element list rather than a bare int: sync route handlers
# run in a worker thread under a *copy* of the request's context, so a
# ContextVar re-set from the thread would never reach the middleware, whereas
# a shared list mutated in place does.
_query_count: ContextVar[Optional[list[int]]] = ContextVar("query_count", default=None)


def start_query_count() -> None:
    _query_count.set([0])


def query_count() -> Optional[int]:
    box = _query_count.get()
    return box[0] if box is not None else None


def _count_statement(*_args, **_kwargs) -> None:
    box = _query_count.get()
    if box is not None:
        box[0] += 1


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    # SQLite-specific: allow connection sharing across threads.
    connect_args = (
        {"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {}
    )
    engine = create_engine(
        settings.database_url, echo=settings.debug, connect_args=connect_args
    )
    event.listen(engine, "before_cursor_execute", _count_statement)
    return engine


def init_db() -> None:
    settings = get_settings()
    if settings.database_url.startswith("sqlite:///"):
        db_path = Path(settings.database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(get_engine())
    _ensure_last_active_column()
    _ensure_indexes()
    _purge_unnamed_users()


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


# Secondary indexes for the lookups that aren't served by a primary key:
# the roster filter (display_name IS NOT NULL AND one_off = 0), "everything
# this person created" (created_by), and "every order this person is in"
# (target_user_id; owner_id leads the composite PK so it's already covered).
# Declared here rather than as Field(index=True) because create_all skips
# tables that already exist, so existing DBs would never gain them.
_INDEXES = (
    ('ix_user_roster', '"user"', "(one_off, display_name)"),
    ('ix_user_created_by', '"user"', "(created_by)"),
    ('ix_orderitem_target', 'orderitem', "(target_user_id)"),
)


def _ensure_indexes() -> None:
    engine = get_engine()
    with engine.connect() as conn:
        for name, table, cols in _INDEXES:
            conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table} {cols}"
            )
        conn.commit()


def _purge_unnamed_users() -> None:
    """Clean up rows from before unnamed visitors stopped being persisted:
    every cookie-less request used to INSERT a User, so old DBs carry junk
    rows for bots and bounced visits. Unnamed users can't be claimed, named
    lists filter them out, and their cookie-holders (if any) are recreated
    transiently on the next request — deleting them loses nothing."""
    engine = get_engine()
    with engine.connect() as conn:
        conn.exec_driver_sql(
            'DELETE FROM orderitem WHERE owner_id IN '
            '(SELECT id FROM "user" WHERE display_name IS NULL)'
        )
        conn.exec_driver_sql(
            'DELETE FROM saveddrink WHERE user_id IN '
            '(SELECT id FROM "user" WHERE display_name IS NULL)'
        )
        conn.exec_driver_sql('DELETE FROM "user" WHERE display_name IS NULL')
        conn.commit()


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
