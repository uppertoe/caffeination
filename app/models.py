from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: str = Field(primary_key=True)
    display_name: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class SavedDrink(SQLModel, table=True):
    """One per user. Enum values are stored as strings for portability."""

    user_id: str = Field(primary_key=True, foreign_key="user.id")
    base_id: str
    temp: str = "hot"
    size: Optional[str] = None
    milk: Optional[str] = None
    shots: int = 1
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
