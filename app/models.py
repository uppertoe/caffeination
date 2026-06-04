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
    notes: Optional[str] = None
    updated_at: datetime = Field(default_factory=utcnow)


class OrderItem(SQLModel, table=True):
    """An open order is just the set of OrderItems for a given owner.

    No separate Order row yet — when we add a notion of closed/till-confirmed
    orders we'll introduce one.
    """

    owner_id: str = Field(primary_key=True, foreign_key="user.id")
    target_user_id: str = Field(primary_key=True, foreign_key="user.id")
    added_at: datetime = Field(default_factory=utcnow)
