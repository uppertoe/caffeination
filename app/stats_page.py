"""The statistics page as a whole: loads the saved drinks once and hands
them to each panel's builder in app.stats_view and app.stats_panels."""

from __future__ import annotations

from collections import Counter

from sqlmodel import Session

from app.menu import DRINKS, MG_PER_SHOT, MG_PER_SHOT_RANGE
from app.models import User
from app.stats_rounds import build_round_panels
from app.stats_panels import (
    MIN_MOVED_ON,
    build_agreement,
    build_caffeine_sim,
    build_moved_on,
    build_pairs,
    build_randomisation,
)
from app.stats_view import (
    BEAN_H,
    BEAN_W,
    CHART_W,
    MIN_ROUNDS,
    Usual,
    build_board,
    build_caffeine,
    build_rounds,
    build_unusual,
    chosen_from_form,
    load_usuals,
)


def usuals_for(everyone: list[Usual], who: str) -> list[Usual]:
    return everyone if who == "all" else [u for u in everyone if u.active]


def stats_context(session: Session, viewer: User, who: str, allocation: int = 1) -> dict:
    from app.orders import purge_all_expired_orders

    purge_all_expired_orders(session)
    everyone = load_usuals(session, "all")
    usuals = usuals_for(everyone, who)
    own = next((u for u in usuals if u.user_id == viewer.id), None)
    board = build_board(usuals)
    # The unusual-drink panel starts on the viewer's usual drink, or on the
    # department's most common one for someone who has not saved a drink.
    if own:
        chosen = own.options
    elif usuals:
        # Not board.top_line, which is None when no order is clearly ahead.
        top = Counter(u.line for u in usuals).most_common(1)[0][0]
        chosen = next(u.options for u in usuals if u.line == top)
    else:
        chosen = chosen_from_form({"base_id": DRINKS[0].id})
    return {
        "who": who,
        "has_own": own is not None,
        "board": board,
        "unusual": build_unusual(usuals, chosen, viewer.id) if usuals else None,
        "chosen": chosen,
        "caffeine": build_caffeine(usuals, viewer.id) if usuals else None,
        "caffeine_sim": build_caffeine_sim(usuals),
        "agreement": build_agreement(usuals),
        "pairs": build_pairs(usuals),
        "randomisation": build_randomisation(usuals, allocation),
        "moved_on": build_moved_on(everyone),
        "min_moved_on": MIN_MOVED_ON,
        "shot_range": MG_PER_SHOT_RANGE,
        "rounds": build_rounds(session),
        "round_panels": build_round_panels(session, usuals, viewer.id),
        "min_rounds": MIN_ROUNDS,
        "shot_mg": MG_PER_SHOT,
        "chart_w": CHART_W,
        "bean_w": BEAN_W,
        "bean_h": BEAN_H,
    }
