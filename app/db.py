from collections.abc import Iterator
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.models import OrderItem

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
    _ensure_columns()
    _rekey_order_items()
    _ensure_indexes()
    _purge_unnamed_users()
    _seed_drink_history()


# One-off in-place migrations: create_all never alters existing tables, so
# DBs created before a column existed need it added. Fresh DBs get every
# column from the models and these are no-ops. Each entry is (table, column,
# column DDL, optional backfill statement). Still no Alembic: a handful of
# guarded ADD COLUMNs doesn't justify it; revisit if these accumulate.
_COLUMNS = (
    ('"user"', "last_active_at", "TIMESTAMP", 'UPDATE "user" SET last_active_at = created_at'),
    ("saveddrink", "shots", "INTEGER", None),
    ("saveddrink", "own_cup", "BOOLEAN NOT NULL DEFAULT 0", None),
    # The round log first shipped without links to people (#21).
    ("round", "owner_id", "VARCHAR", None),
    ("rounddrink", "user_id", "VARCHAR", None),
    ("rounddrink", "own_cup", "BOOLEAN NOT NULL DEFAULT 0", None),
    ("grouporder", "status_message", "VARCHAR", None),
    ("grouporder", "previous_code", "VARCHAR", None),
    # Columns added earlier on the join-by-code branch, for dev databases
    # created part-way through it. No-ops everywhere else.
    ("grouporder", "code", "VARCHAR", None),
    ("grouporder", "joins_close_at", "TIMESTAMP", None),
    ("grouporder", "max_drinks", "INTEGER NOT NULL DEFAULT 8", None),
    ("grouporder", "name", "VARCHAR", None),
    ("grouporder", "location", "VARCHAR", None),
    ("orderitem", "location", "VARCHAR", None),
    ("orderitem", "joined_by", "VARCHAR", None),
    ("orderitem", "base_id", "VARCHAR", None),
    ("orderitem", "temp", "VARCHAR", None),
    ("orderitem", "size", "VARCHAR", None),
    ("orderitem", "milk", "VARCHAR", None),
    ("orderitem", "strength", "VARCHAR", None),
    ("orderitem", "sweetener", "VARCHAR", None),
    ("orderitem", "length", "VARCHAR", None),
    ("orderitem", "shots", "INTEGER", None),
    ("orderitem", "own_cup", "BOOLEAN", None),
    ("orderitem", "notes", "VARCHAR", None),
    ("orderitem", "drink_fixed", "BOOLEAN NOT NULL DEFAULT 0", None),
    ("grouporder", "status_at", "TIMESTAMP", None),
)


def _rekey_order_items() -> None:
    """Move order lines from being keyed by owner to being keyed by order.

    Before the Order table, an open order was just the OrderItems sharing an
    owner_id, and a row targeting the owner was their opt-out marker. Each
    owner with items becomes one open everyday order, started when its first
    item was added; the marker becomes owner_excluded_at; the remaining rows
    are copied across. SQLite can't change a primary key in place, so the old
    table is renamed, the new one created from the model, and the old one
    dropped, all in one transaction. A no-op once orderitem has no owner_id.
    """
    engine = get_engine()
    with engine.begin() as conn:
        cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(orderitem)").fetchall()]
        if "owner_id" not in cols:
            return
        conn.exec_driver_sql(
            "INSERT INTO grouporder (owner_id, kind, status, max_drinks, owner_excluded_at, created_at)"
            " SELECT owner_id, 'everyday', 'open', 8,"
            " MAX(CASE WHEN target_user_id = owner_id THEN added_at END), MIN(added_at)"
            " FROM orderitem GROUP BY owner_id"
        )
        conn.exec_driver_sql("ALTER TABLE orderitem RENAME TO orderitem_by_owner")
        # The old table's index keeps its name through the rename; drop it so
        # _ensure_indexes can create the same name on the new table.
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_orderitem_target")
        OrderItem.__table__.create(conn)
        conn.exec_driver_sql(
            "INSERT INTO orderitem (order_id, target_user_id, added_at, drink_fixed)"
            " SELECT g.id, o.target_user_id, o.added_at, 0 FROM orderitem_by_owner o"
            " JOIN grouporder g ON g.owner_id = o.owner_id"
            " AND g.kind = 'everyday' AND g.status = 'open'"
            " WHERE o.target_user_id != o.owner_id"
        )
        conn.exec_driver_sql("DROP TABLE orderitem_by_owner")


def _ensure_columns() -> None:
    engine = get_engine()
    with engine.connect() as conn:
        for table, column, ddl, backfill in _COLUMNS:
            cols = [row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()]
            if column not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                if backfill:
                    conn.exec_driver_sql(backfill)
        conn.commit()


# Secondary indexes for the lookups that aren't served by a primary key:
# the roster filter (display_name IS NOT NULL AND one_off = 0), "everything
# this person created" (created_by), "every order this person is in"
# (target_user_id; order_id leads the composite PK so it's already covered),
# and "this owner's orders". The partial unique index is the rule that a
# buyer has at most one live everyday order.
# Declared here rather than as Field(index=True) because create_all skips
# tables that already exist, so existing DBs would never gain them.
_INDEXES = (
    ("INDEX", 'ix_user_roster', '"user"', "(one_off, display_name)"),
    ("INDEX", 'ix_user_created_by', '"user"', "(created_by)"),
    ("INDEX", 'ix_orderitem_target', 'orderitem', "(target_user_id)"),
    ("INDEX", 'ix_rounddrink_user', 'rounddrink', "(user_id)"),
    ("INDEX", 'ix_grouporder_owner', 'grouporder', "(owner_id, status)"),
    (
        "UNIQUE INDEX",
        'ix_grouporder_one_live_everyday',
        'grouporder',
        "(owner_id) WHERE kind = 'everyday' AND status IN ('open', 'ordered', 'on_the_way')",
    ),
    # A join code identifies one live order. Closed orders release theirs.
    (
        "UNIQUE INDEX",
        'ix_grouporder_live_code',
        'grouporder',
        "(code) WHERE code IS NOT NULL AND status IN ('open', 'ordered', 'on_the_way')",
    ),
)


# Indexes replaced by a differently defined one under a new name.
_DROPPED_INDEXES = ("ix_grouporder_one_everyday",)


def _ensure_indexes() -> None:
    engine = get_engine()
    with engine.connect() as conn:
        for name in _DROPPED_INDEXES:
            conn.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")
        for kind, name, table, cols in _INDEXES:
            conn.exec_driver_sql(
                f"CREATE {kind} IF NOT EXISTS {name} ON {table} {cols}"
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
            'DELETE FROM orderitem WHERE order_id IN (SELECT g.id FROM grouporder g'
            ' JOIN "user" u ON u.id = g.owner_id WHERE u.display_name IS NULL)'
        )
        conn.exec_driver_sql(
            'DELETE FROM grouporder WHERE owner_id IN '
            '(SELECT id FROM "user" WHERE display_name IS NULL)'
        )
        conn.exec_driver_sql(
            'DELETE FROM saveddrink WHERE user_id IN '
            '(SELECT id FROM "user" WHERE display_name IS NULL)'
        )
        conn.exec_driver_sql('DELETE FROM "user" WHERE display_name IS NULL')
        conn.commit()


def _seed_drink_history() -> None:
    """Give every roster drink saved before the history existed a first
    DrinkChange, stamped with when it was last saved, so the history starts
    from the drinks as they stood. Runs once: only while the table is empty."""
    engine = get_engine()
    with engine.connect() as conn:
        if conn.exec_driver_sql("SELECT 1 FROM drinkchange LIMIT 1").first():
            return
        conn.exec_driver_sql(
            "INSERT INTO drinkchange (user_id, changed_at, base_id, temp, size, milk,"
            " strength, sweetener, length, shots, own_cup)"
            " SELECT s.user_id, s.updated_at, s.base_id, s.temp, s.size, s.milk,"
            " s.strength, s.sweetener, s.length, s.shots, s.own_cup"
            ' FROM saveddrink s JOIN "user" u ON u.id = s.user_id WHERE u.one_off = 0'
        )
        conn.commit()


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
