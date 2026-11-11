from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: str = Field(primary_key=True)
    display_name: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    # Who created this person on someone else's behalf (None = self-onboarded).
    # The creator may edit a roster person for a window after creation, and may
    # always edit/remove their own one-off entries. See app.users.can_edit_person.
    created_by: Optional[str] = Field(default=None, foreign_key="user.id")
    # One-off ("guest") entries live only in their creator's order: excluded
    # from the roster + name search, duplicate names allowed, deleted on removal.
    one_off: bool = False
    # Last sign of life: visiting the app (throttled), saving a name/drink,
    # building an order, or being added to someone else's order. Drives the
    # roster's active/inactive split so finished rotations sink out of the way.
    # Optional because pre-existing rows are backfilled by init_db's ALTER.
    last_active_at: Optional[datetime] = Field(default_factory=utcnow)


class SavedDrink(SQLModel, table=True):
    """One per user. Enum values are stored as strings for portability."""

    user_id: str = Field(primary_key=True, foreign_key="user.id")
    base_id: str
    temp: str = "hot"
    size: Optional[str] = None
    milk: Optional[str] = None
    strength: str = "regular"
    sweetener: str = "none"
    length: Optional[str] = None  # macchiato-only "short" | "long"
    shots: Optional[int] = None  # only for drinks with shot choices (espresso)
    own_cup: bool = False
    notes: Optional[str] = None
    updated_at: datetime = Field(default_factory=utcnow)


# Orders that are still under way: open (taking drinks), or placed at the
# café and not yet back. A buyer has at most one live everyday order.
LIVE_STATUSES = ("open", "ordered", "on_the_way")


class Order(SQLModel, table=True):
    """A group order: one buyer, the people in it (OrderItem), and where it
    stands. A buyer has at most one live everyday order (enforced by a
    partial unique index in app.db) and any number of advance orders.

    The row outlives its lines: closing an order deletes the OrderItems but
    keeps the Order with its closing status, so that people who joined can
    still be told how it ended. A Ready order keeps its lines too, so a
    joiner's page can still show their drink. Closed rows, and any lines
    they kept, are pruned after a day.
    """

    __tablename__ = "grouporder"  # "order" is an SQL keyword

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: str = Field(foreign_key="user.id")
    kind: str = "everyday"  # everyday | advance
    # open | ordered | on_the_way | ready | cancelled | expired
    status: str = "open"
    # Set while the owner has taken themselves out of their own order
    # ("buying for others, not me"). The owner is otherwise included
    # implicitly, with no OrderItem of their own. A timestamp rather than a
    # flag because it counts towards the order's age, as the opt-out marker
    # row it replaces did.
    owner_excluded_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)
    closed_at: Optional[datetime] = None

    # Sharing (app.sharing). An order is shared once it has a code; the code
    # is unique among live orders (a partial unique index in app.db) and
    # released when the order closes. Joins are accepted until
    # joins_close_at and while the order has fewer than max_drinks drinks.
    code: Optional[str] = None
    # The code before the buyer last replaced it, so that devices which
    # joined with the old code keep their status page (app.joining).
    previous_code: Optional[str] = None
    joins_close_at: Optional[datetime] = None
    max_drinks: int = 8
    name: Optional[str] = None  # e.g. "Tuesday education meeting"
    location: Optional[str] = None  # one delivery location for every drink

    # The buyer's latest update to everyone who joined (app.sharing.send_update):
    # its optional message and when it was sent. The status itself is above.
    status_message: Optional[str] = None
    status_at: Optional[datetime] = None


class OrderItem(SQLModel, table=True):
    """One person in an order (never the owner, who is included implicitly).

    The line uses the person's usual drink unless base_id is set, in which
    case the drink options here are a one-off for this order only. Same
    option fields as SavedDrink.
    """

    order_id: int = Field(primary_key=True, foreign_key="grouporder.id")
    target_user_id: str = Field(primary_key=True, foreign_key="user.id")
    added_at: datetime = Field(default_factory=utcnow)
    # Where to bring the drink; None means it is collected from the buyer.
    location: Optional[str] = None
    # Who added this line by code (app.joining): a user id, or a guest
    # device's joiner id. None for lines the buyer added from the roster.
    joined_by: Optional[str] = None

    # A one-off drink for this order (None: the person's usual drink).
    base_id: Optional[str] = None
    temp: Optional[str] = None
    size: Optional[str] = None
    milk: Optional[str] = None
    strength: Optional[str] = None
    sweetener: Optional[str] = None
    length: Optional[str] = None
    shots: Optional[int] = None
    own_cup: Optional[bool] = None
    notes: Optional[str] = None
    # Set when the buyer sent Ordered and this line's usual drink was copied
    # into the fields above, so a later change to the usual can't change
    # what was ordered. Such a line is still shown as the person's usual.
    drink_fixed: bool = False


class PlaceName(SQLModel, table=True):
    """A delivery location someone typed, for suggesting it next time.

    Deliberately holds no link to any person or order: only the name, how
    often it has been used and when it was last used.
    """

    key: str = Field(primary_key=True)  # the name, case-folded
    name: str  # as most recently typed
    uses: int = 0
    last_used_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# The round log and drink history behind the statistics page. A round is
# recorded when an order closes (cleared, or expired after ORDER_TTL), stamped
# with the time its last person was added. Rows link to people so that
# repeat drinks can be treated as one person's, but the statistics page only
# ever shows aggregates and the viewer's own figures: it never names anyone.
# Deleting yourself detaches your rounds and deletes your drink history.
# ---------------------------------------------------------------------------


class Round(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    placed_at: datetime  # UTC
    drink_count: int
    owner_id: Optional[str] = None  # who placed the order


class RoundDrink(SQLModel, table=True):
    """One drink in a round, as it was ordered. Same option fields as
    SavedDrink; notes are reduced to whether there were any."""

    id: Optional[int] = Field(default=None, primary_key=True)
    round_id: int = Field(foreign_key="round.id", index=True)
    user_id: Optional[str] = None  # None for guests and detached rows
    base_id: str
    temp: str
    size: Optional[str] = None
    milk: Optional[str] = None
    strength: str
    sweetener: str
    length: Optional[str] = None
    shots: Optional[int] = None
    own_cup: bool = False
    has_notes: bool = False
    guest: bool = False


class DrinkChange(SQLModel, table=True):
    """A usual drink as it stood after each save that changed it, for one
    roster person. The first row per person is their first saved drink (or,
    for drinks saved before the history existed, the drink as it stood then)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    changed_at: datetime  # UTC
    changed_by: Optional[str] = None  # None when the person saved it themselves
    base_id: str
    temp: str
    size: Optional[str] = None
    milk: Optional[str] = None
    strength: str
    sweetener: str
    length: Optional[str] = None
    shots: Optional[int] = None
    own_cup: bool = False
