"""Everything the statistics page renders: loads the saved drinks and the
round log, runs app.stats over them, and lays the charts out as plain
numbers (SVG coordinates included) so the templates only draw."""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.config import get_settings
from app.drinks import format_line, normalize
from app.menu import (
    DRINKS,
    MG_PER_SHOT,
    MILK_LABELS,
    MG_PER_SHOT_RANGE,
    SIZE_LABELS,
    STRENGTH_LABELS,
    SWEETENER_LABELS,
    TEMP_LABELS,
    Length,
    Milk,
    MilkPolicy,
    Size,
    Strength,
    Sweetener,
    Temp,
    estimated_caffeine_mg,
    get_drink,
)
from app.models import Round, SavedDrink, User
from app.orders import ACTIVE_WINDOW
from app.stats import UnusualnessModel, association, even_spread_p, half_window, holm_adjust
from app.users import ROSTER_FILTER, _as_naive_utc

MIN_ROUNDS = 5  # round charts appear from this many rounds
MIN_FOR_PAIRING = 12  # drinks needed before a pairing is tested

DRINK_LABELS = {d.id: d.display for d in DRINKS}

# The options a drink is made of, with every value each can take. A drink
# carries None for an option that does not apply to it (milk on a long
# black, size on a piccolo), and such drinks are left out of that option's
# shares and pairings.
DOMAINS: dict[str, tuple] = {
    "base_id": tuple(d.id for d in DRINKS),
    "temp": tuple(t.value for t in Temp),
    "size": tuple(s.value for s in Size),
    "milk": tuple(m.value for m in Milk),
    "strength": tuple(s.value for s in Strength),
    "sweetener": tuple(s.value for s in Sweetener),
    "length": tuple(l.value for l in Length),
    "shots": (1, 2),
}

# SVG geometry shared by the bean charts (viewBox units).
CHART_W = 600
BEAN_W, BEAN_H, BEAN_GAP = 18, 12, 2


def as_options(base_id, temp, size, milk, strength, sweetener, length, shots) -> dict:
    """A drink as its applicable options only."""
    d = get_drink(base_id)
    return {
        "base_id": base_id,
        "temp": temp if d and (d.allows_iced or d.allows_extra_hot) else None,
        "size": size if d and d.sized else None,
        "milk": milk if d and d.milk_policy == MilkPolicy.REQUIRED else None,
        "strength": strength if d and d.allows_strength else None,
        "sweetener": sweetener,
        "length": length if d and d.has_length else None,
        "shots": shots if d and d.shot_choices else None,
    }


def line_for(options: dict) -> str:
    """The order line for a drink's options, without notes or own cup."""
    o = options
    return format_line(
        o["base_id"], o["length"], o["size"], o["temp"] or Temp.HOT.value,
        o["strength"] or Strength.REGULAR.value, o["milk"], o["sweetener"], None,
        o["shots"],
    )


@dataclass
class Usual:
    """One colleague's usual drink."""

    user_id: str
    options: dict
    line: str


def load_usuals(session: Session, who: str) -> list[Usual]:
    """Every roster person's usual drink: active people only, or everyone."""
    q = (
        select(
            User.id,
            User.last_active_at,
            User.created_at,
            SavedDrink.base_id,
            SavedDrink.temp,
            SavedDrink.size,
            SavedDrink.milk,
            SavedDrink.strength,
            SavedDrink.sweetener,
            SavedDrink.length,
            SavedDrink.shots,
        )
        .join(SavedDrink, SavedDrink.user_id == User.id)
        .where(*ROSTER_FILTER)
    )
    cutoff = _as_naive_utc(datetime.now(timezone.utc)) - ACTIVE_WINDOW
    usuals = []
    for uid, last_active, created, *drink in session.exec(q):
        if who == "active" and _as_naive_utc(last_active or created) < cutoff:
            continue
        if get_drink(drink[0]) is None:
            continue  # a drink since removed from the menu
        opts = as_options(*drink)
        usuals.append(Usual(uid, opts, line_for(opts)))
    return usuals


# ---------------------------------------------------------------------------
# How unusual a drink is. The page leads with this.
# ---------------------------------------------------------------------------


@dataclass
class Bean:
    x: float
    y: float
    tip: str
    mine: bool = False


@dataclass
class BeanChart:
    beans: list[Bean]
    height: float


def stack_beans(items: list[tuple[int, str, bool]], columns: int, min_rows: int = 4) -> BeanChart:
    """Lay out (column, tooltip, mine) items as bottom-up stacks of beans;
    the viewer's own bean goes on top of its stack, where it is easy to find."""
    heights: Counter = Counter()
    col_w = CHART_W / columns
    placed = []
    for col, tip, mine in sorted(items, key=lambda it: (it[0], it[2])):
        placed.append((col, heights[col], tip, mine))
        heights[col] += 1
    height = max([min_rows, *heights.values()]) * (BEAN_H + BEAN_GAP)
    beans = [
        Bean(
            x=col * col_w + (col_w - BEAN_W) / 2,
            y=height - (row + 1) * (BEAN_H + BEAN_GAP) + BEAN_GAP / 2,
            tip=tip,
            mine=mine,
        )
        for col, row, tip, mine in placed
    ]
    return BeanChart(beans, height)


@dataclass
class Unusual:
    n: int  # usual drinks compared against: everyone's but the viewer's
    line: str
    more_ordinary: int  # of those, how many score as more ordinary
    excludes_viewer: bool
    reason: str  # which option makes this drink unusual, with its count
    chart: BeanChart
    marker_x: float


UNUSUAL_COLUMNS = 30

LENGTH_LABELS = {Length.SHORT.value: "Short", Length.LONG.value: "Long"}


def _verb(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _reason(axis: str, value, count: int, total: int) -> str:
    """One sentence naming the drink's least common option and how many of
    the department's usual drinks share it. Counts, never names: the same
    numbers are on the menu board."""
    c = "none" if count == 0 else str(count)
    if axis == "base_id":
        drink = DRINK_LABELS[value].lower()
        return (
            f"Its least common choice is the drink itself: {c} of the {total} "
            f"colleagues {_verb(count, 'has', 'have')} {drink} as their usual drink."
        )
    if axis == "milk":
        phrase, basis = f"{MILK_LABELS[value].lower()} milk", "usual drinks made with milk"
        verb = _verb(count, "uses it", "use it")
    elif axis == "size":
        label = SIZE_LABELS[value].lower()
        phrase, basis = f"the {label} size", "usual drinks that come in a size"
        verb = _verb(count, f"is {label}", f"are {label}")
    elif axis == "strength":
        phrase = {"regular": "regular strength", "weak": "weak strength",
                  "extra_shot": "an extra shot", "decaf": "decaf"}[value]
        basis, verb = "usual drinks with a strength choice", _verb(count, "has it", "have it")
    elif axis == "temp":
        label = TEMP_LABELS[value].lower()
        phrase, basis = f"serving it {label}", "usual drinks that can be served extra hot or iced"
        verb = _verb(count, f"is served {label}", f"are served {label}")
    elif axis == "sweetener":
        phrase = {"none": "no sugar", "one_sugar": "one sugar",
                  "two_sugar": "two sugars", "honey": "honey"}[value]
        basis, verb = "usual drinks", _verb(count, "takes it", "take it")
    elif axis == "length":
        label = LENGTH_LABELS[value].lower()
        phrase, basis = f"a {label} macchiato", "usual macchiatos"
        verb = _verb(count, f"is {label}", f"are {label}")
    else:  # shots
        word = "double" if value == 2 else "single"
        phrase, basis = f"a {word} shot", "usual espressos"
        verb = _verb(count, f"is a {word}", f"are {word}s")
    if total == 0:
        return f"Its least common option is {phrase}: no usual drink in the department has it yet."
    return f"Its least common option is {phrase}: {c} of the {total} {basis} {verb}."


@lru_cache(maxsize=8)
def _model(population: tuple[tuple, ...]) -> tuple[UnusualnessModel, tuple[float, ...]]:
    drinks = [dict(zip(DOMAINS, p)) for p in population]
    model = UnusualnessModel(drinks, DOMAINS)
    return model, tuple(model.surprisal(d) for d in drinks)


def build_unusual(usuals: list[Usual], chosen: dict, viewer_id: str) -> Unusual:
    population = tuple(tuple(u.options[a] for a in DOMAINS) for u in usuals)
    model, scores = _model(population)
    score = model.surprisal(chosen)
    top = max([score, *scores, 1.0]) * 1.08
    col_of = lambda s: min(UNUSUAL_COLUMNS - 1, int(s / top * UNUSUAL_COLUMNS))
    chart = stack_beans(
        [
            (col_of(s), "Your usual drink" if u.user_id == viewer_id else "One colleague’s usual drink",
             u.user_id == viewer_id)
            for u, s in zip(usuals, scores)
        ],
        UNUSUAL_COLUMNS,
    )
    others = [s for u, s in zip(usuals, scores) if u.user_id != viewer_id]
    # The option that contributes most to the score is the one the
    # department chooses least often among drinks that offer it.
    axis = max(
        (a for a in DOMAINS if chosen[a] is not None),
        key=lambda a: -model.option_probability(a, chosen[a]),
    )
    counts = model.counts[axis]
    return Unusual(
        n=len(others),
        line=line_for(chosen),
        more_ordinary=sum(1 for s in others if s < score - 1e-9),
        excludes_viewer=len(others) < len(usuals),
        reason=_reason(axis, chosen[axis], counts[chosen[axis]], sum(counts.values())),
        chart=chart,
        marker_x=(col_of(score) + 0.5) * CHART_W / UNUSUAL_COLUMNS,
    )


def chosen_from_form(form: dict) -> Optional[dict]:
    """The drink picked in the panel, normalised by the same rules as a save."""
    norm = normalize(form.get("base_id", ""), form)
    if norm is None:
        return None
    return as_options(*(norm[a] for a in DOMAINS))


# ---------------------------------------------------------------------------
# What the department drinks: exact shares of each option. The saved drinks
# are the whole department, not a sample, so the shares carry no interval.
# ---------------------------------------------------------------------------


@dataclass
class ShareRow:
    label: str
    count: int
    share: float


@dataclass
class Facet:
    key: str
    tab: str
    n: int
    rows: list[ShareRow]


FACETS = (
    ("base_id", "Drink", DRINK_LABELS, True),
    ("milk", "Milk", MILK_LABELS, True),
    ("size", "Size", SIZE_LABELS, False),
    ("strength", "Strength", STRENGTH_LABELS, True),
    ("temp", "Temperature", TEMP_LABELS, False),
    ("sweetener", "Sweetener", SWEETENER_LABELS, True),
)


@dataclass
class Board:
    n: int
    unique: int  # exact orders that no one else shares
    top_line: Optional[str]  # None unless one exact order is clearly ahead
    top_count: int
    facets: list[Facet]


def build_board(usuals: list[Usual]) -> Board:
    facets = []
    for key, tab, labels, by_count in FACETS:
        values = [u.options[key] for u in usuals if u.options[key] is not None]
        counts = Counter(values)
        n = len(values)
        order = list(labels)
        if by_count:  # nominal options read best most-chosen first
            order.sort(key=lambda v: -counts[v])
        rows = [ShareRow(labels[v], counts[v], counts[v] / n if n else 0.0) for v in order]
        facets.append(Facet(key, tab, n, rows))
    lines = Counter(u.line for u in usuals)
    ranked = lines.most_common(2)
    clear = bool(ranked) and ranked[0][1] > 1 and (len(ranked) == 1 or ranked[0][1] > ranked[1][1])
    return Board(
        n=len(usuals),
        unique=sum(1 for c in lines.values() if c == 1),
        top_line=ranked[0][0] if clear else None,
        top_count=ranked[0][1] if clear else 0,
        facets=facets,
    )


# ---------------------------------------------------------------------------
# Caffeine.
# ---------------------------------------------------------------------------

# Rows of the caffeine chart, by the nearest half espresso shot. The same
# steps word the facts and tooltips, so a drink is never described one way
# in its row and another in its label.
CAFFEINE_ROWS = (
    "Little or none", "About half a shot", "About one shot",
    "About one and a half shots", "About two shots", "Two and a half shots or more",
)


def half_shots(mg: float) -> int:
    """Caffeine as a whole number of half shots, capped at the last row."""
    return min(len(CAFFEINE_ROWS) - 1, int(mg / MG_PER_SHOT * 2 + 0.5))


def describe_mg(mg: float) -> str:
    """'about one shot': shots only. With single café shots ranging fourfold,
    a milligram figure per drink would claim more precision than there is."""
    return CAFFEINE_ROWS[half_shots(mg)].lower()


@dataclass
class CaffeineRow:
    label: str
    beans: list[tuple[str, bool]]  # (tooltip, is the viewer's own)


@dataclass
class Caffeine:
    n: int
    lead: str
    typical: str
    mine: Optional[str]
    decaf: int
    rows: list[CaffeineRow]


def build_caffeine(usuals: list[Usual], viewer_id: str) -> Caffeine:
    mgs = [
        (
            estimated_caffeine_mg(
                u.options["base_id"], u.options["size"],
                u.options["strength"] or Strength.REGULAR.value, u.options["shots"],
            ),
            u.user_id == viewer_id,
        )
        for u in usuals
    ]
    rows = [CaffeineRow(label, []) for label in CAFFEINE_ROWS]
    # The viewer's bean goes last in its row, where it is easy to find.
    for mg, mine in sorted(mgs, key=lambda m: (m[1], m[0])):
        who = "Your usual drink" if mine else "One colleague’s usual drink"
        rows[half_shots(mg)].beans.append((f"{who}: {describe_mg(mg)}", mine))
    biggest = max(range(len(rows)), key=lambda i: len(rows[i].beans))
    amount = "little or no caffeine" if biggest == 0 else CAFFEINE_ROWS[biggest].lower() + " of espresso"
    lead = (
        f"More than half of the department’s usual drinks contain {amount}."
        if 2 * len(rows[biggest].beans) > len(usuals)
        else f"The most common amount in the department’s usual drinks is {amount}."
    )
    mine = [mg for mg, m in mgs if m]
    return Caffeine(
        n=len(usuals),
        lead=lead,
        typical=describe_mg(statistics.median(mg for mg, _ in mgs)),
        mine=describe_mg(mine[0]) if mine else None,
        decaf=sum(1 for u in usuals if u.options["strength"] == Strength.DECAF.value),
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Options that go together: tested by permutation, reported only when the
# evidence says there is a link.
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    sentence: str
    p_value: float  # Holm-adjusted across the four pairs

    @property
    def odds(self) -> str:
        return "1 time in 100" if self.p_value < 0.01 else "1 time in 20"


_STRENGTH_PHRASES = {
    "Regular": "regular strength",
    "Weak": "weak",
    "Extra shot": "made with an extra shot",
    "Decaf": "decaf",
}


def _sugar(u: Usual) -> str:
    return "none" if u.options["sweetener"] == Sweetener.NONE.value else "some"


# Each pairing: a name for the note listing what was tested, how to read the
# two options off a usual drink, and the sentence for the combination that
# stands out when the link is real.
PAIRINGS: tuple[tuple[str, Callable, Callable], ...] = (
    (
        "size and temperature",
        lambda u: (SIZE_LABELS.get(u.options["size"]), TEMP_LABELS.get(u.options["temp"])),
        lambda size, temp: f"{temp} drinks are more often {size.lower()}",
    ),
    (
        "milk and drink",
        lambda u: (MILK_LABELS.get(u.options["milk"]), DRINK_LABELS[u.options["base_id"]]),
        lambda milk, drink: f"{drink} drinkers choose {milk.lower()} milk more often",
    ),
    (
        "strength and size",
        lambda u: (STRENGTH_LABELS.get(u.options["strength"]), SIZE_LABELS.get(u.options["size"])),
        lambda strength, size: f"{size} drinks are more often {_STRENGTH_PHRASES[strength]}",
    ),
    (
        "sweetener and drink",
        lambda u: (_sugar(u), DRINK_LABELS[u.options["base_id"]]),
        lambda sugar, drink: f"{drink} drinkers {'take a sweetener' if sugar == 'some' else 'go without a sweetener'} more often",
    ),
)
PAIRING_NAMES = tuple(name for name, _, _ in PAIRINGS)


@lru_cache(maxsize=32)
def _association(pairs: tuple[tuple[str, str], ...]):
    return association(pairs)


def build_pairings(usuals: list[Usual]) -> list[Finding]:
    """Findings that survive a Holm correction across every pair tested;
    the page shows the panel only when there is at least one."""
    tested = []
    for _, pick, sentence in PAIRINGS:
        pairs = tuple(sorted(p for p in (pick(u) for u in usuals) if None not in p))
        if len(pairs) < MIN_FOR_PAIRING:
            continue
        result = _association(pairs)
        if result is not None:
            tested.append((sentence, result))
    adjusted = holm_adjust([r.p_value for _, r in tested])
    findings = [
        Finding(sentence(*r.standout), p)
        for (sentence, r), p in zip(tested, adjusted)
        if p < 0.05
    ]
    return sorted(findings, key=lambda f: f.p_value)


# ---------------------------------------------------------------------------
# Rounds, from the anonymised log.
# ---------------------------------------------------------------------------

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class Bar:
    x: float
    w: float
    h: float
    label: str
    count: int
    tip: str


@dataclass
class Rounds:
    n: int
    ready: bool
    typical_drinks: Optional[int] = None
    half_window: Optional[str] = None  # "9:30 am and 11 am"
    busiest_day: Optional[str] = None  # None unless one day stands out beyond chance
    time_bars: list[Bar] = field(default_factory=list)
    time_ticks: list[tuple[float, str]] = field(default_factory=list)
    band: Optional[tuple[float, float]] = None
    days: list[tuple[str, int, float]] = field(default_factory=list)  # (day, count, height 0..1)


def _clock(minutes: float) -> str:
    """'9:30 am', '11 am', 'midday', '2:15 pm'."""
    h, m = divmod(int(round(minutes)) % 1440, 60)
    if (h, m) == (12, 0):
        return "midday"
    if (h, m) == (0, 0):
        return "midnight"
    hour = (h - 1) % 12 + 1
    return f"{hour}{f':{m:02d}' if m else ''} {'am' if h < 12 else 'pm'}"


def build_rounds(session: Session) -> Rounds:
    rows = session.exec(select(Round.placed_at, Round.drink_count)).all()
    out = Rounds(n=len(rows), ready=len(rows) >= MIN_ROUNDS)
    if not out.ready:
        return out
    tz = ZoneInfo(get_settings().timezone)
    local = [
        _as_naive_utc(at).replace(tzinfo=timezone.utc).astimezone(tz) for at, _ in rows
    ]
    minutes = [t.hour * 60 + t.minute for t in local]
    out.typical_drinks = int(statistics.median(c for _, c in rows))
    w_start, w_end = half_window(minutes)
    out.half_window = f"{_clock(w_start)} to {_clock(w_end)}"

    # Quarter-hour bars, the resolution rounds are logged at, so the shaded
    # half-window lines up with whole bars. Widened to take in any outliers.
    step = 15
    start = min(6 * 60, min(minutes) // 60 * 60)
    end = max(18 * 60, (max(minutes) // 60 + 1) * 60)
    slots = (end - start) // step
    slot_w = CHART_W / slots
    per_slot = Counter((m - start) // step for m in minutes)
    peak = max(per_slot.values())
    for s in range(slots):
        k = per_slot[s]
        t0 = start + s * step
        out.time_bars.append(Bar(
            x=s * slot_w + 0.75, w=slot_w - 1.5, h=k / peak, label=_clock(t0), count=k,
            tip=f"{_clock(t0)} to {_clock(t0 + step)}: {k} round{'s' if k != 1 else ''}",
        ))
    out.time_ticks = [(s * slot_w, _clock(start + s * step)) for s in range(0, slots + 1, 8)]
    if w_end <= end and w_start >= start:  # a window across midnight is not drawn
        to_x = lambda m: (m - start) / (end - start) * CHART_W
        out.band = (to_x(w_start), to_x(w_end))

    days = Counter(t.weekday() for t in local)
    shown = range(7) if days[5] or days[6] else range(5)
    top = max(days.values())
    out.days = [(WEEKDAYS[d], days[d], days[d] / top) for d in shown]
    if even_spread_p([days[d] for d in shown]) < 0.05:
        out.busiest_day = WEEKDAYS[max(days, key=days.get)]
    return out


# ---------------------------------------------------------------------------
# The page.
# ---------------------------------------------------------------------------


def stats_context(session: Session, viewer: User, who: str) -> dict:
    from app.orders import purge_all_expired_orders

    purge_all_expired_orders(session)
    usuals = load_usuals(session, who)
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
        "findings": build_pairings(usuals),
        "pairing_names": PAIRING_NAMES,
        "shot_range": MG_PER_SHOT_RANGE,
        "rounds": build_rounds(session),
        "min_rounds": MIN_ROUNDS,
        "shot_mg": MG_PER_SHOT,
        "chart_w": CHART_W,
        "bean_w": BEAN_W,
        "bean_h": BEAN_H,
    }
