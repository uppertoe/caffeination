"""The panels that teach a method on the department's own usual drinks:
options that go together (a χ² test) and randomisation with the baseline
table. Each builds the
answer and its working, laid out as plain numbers, so the templates only
draw. The heavy parts are cached on the data they were computed from."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional

from app.menu import MG_PER_SHOT, SIZE_LABELS, Strength, Sweetener, caffeine_parts
from app.stats import (
    ChiSquare,
    allocate,
    baseline_p,
    chi_square,
    holm_adjust,
    randomised_baseline_ps,
)
from app.stats_view import CHART_W, MIN_FOR_PAIRING, Usual

# ---------------------------------------------------------------------------
# Coarser groupings of the options, so each table is small enough to read
# and its cells are not all near empty.
# ---------------------------------------------------------------------------

PLANT_MILKS = {"oat", "soy", "almond"}
DRINK_GROUPS = {
    "Milk coffee": {"latte", "flat_white", "cappuccino", "magic", "piccolo", "macchiato", "mocha"},
    "Black coffee": {"espresso", "long_black", "filter", "cold_brew"},
    "Chai, matcha or chocolate": {"chai_latte", "dirty_chai", "matcha_latte", "hot_chocolate"},
}
SIZES = tuple(SIZE_LABELS.values())


def milk_kind(o: dict) -> Optional[str]:
    if o["milk"] is None:
        return None
    return "Plant" if o["milk"] in PLANT_MILKS else "Dairy"


def drink_group(o: dict) -> str:
    return next(g for g, ids in DRINK_GROUPS.items() if o["base_id"] in ids)


def strength_group(o: dict) -> Optional[str]:
    if o["shots"] == 2 or o["strength"] == Strength.EXTRA_SHOT.value:
        return "Extra shot or double"
    if o["strength"] in (Strength.WEAK.value, Strength.DECAF.value):
        return "Weak or decaf"
    if o["strength"] is None and o["shots"] is None:
        return None
    return "Regular strength"


def sugar(o: dict) -> str:
    return "No sugar" if o["sweetener"] == Sweetener.NONE.value else "Sugar or honey"


def size(o: dict) -> Optional[str]:
    return SIZE_LABELS.get(o["size"])


def shots_of(o: dict) -> float:
    """Caffeine in espresso shots, counting other caffeine at a shot's mg."""
    n, other = caffeine_parts(
        o["base_id"], o["size"], o["strength"] or Strength.REGULAR.value, o["shots"]
    )
    return n + other / MG_PER_SHOT


# ---------------------------------------------------------------------------
# Options that go together.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    key: str
    tab: str
    row_name: str  # what the rows are, for the table's corner cell
    col_name: str
    pick: Callable[[dict], tuple[Optional[str], Optional[str]]]
    rows: tuple[str, ...]  # display order
    cols: tuple[str, ...]
    sentence: Callable[[str, str], str]  # for the standout cell of a real link
    no_link: str = ""  # the lead when there is no evidence of a link
    premise: str = ""  # the null hypothesis, as the working states it


_STRENGTH_PHRASE = {"Weak or decaf": "weak or decaf", "Extra shot or double": "made with an extra shot or as a double", "Regular strength": "regular strength"}

PAIRS: tuple[Pair, ...] = (
    Pair(
        "milk_size", "Milk and size", "Milk", "Size",
        lambda o: (milk_kind(o), size(o)), ("Dairy", "Plant"), SIZES,
        lambda r, c: f"{r} milk drinks are {c.lower()} more often",
        "The department’s choice of milk shows no clear link with size.",
        "If milk and size were chosen independently",
    ),
    Pair(
        "milk_drink", "Milk and drink", "Milk", "Drink",
        lambda o: (milk_kind(o), drink_group(o)), ("Dairy", "Plant"),
        ("Milk coffee", "Chai, matcha or chocolate"),
        lambda r, c: f"{c} drinks are made with {r.lower()} milk more often",
        "The department’s choice of milk shows no clear link with the kind of drink.",
        "If milk and the kind of drink were chosen independently",
    ),
    Pair(
        "strength_size", "Strength and size", "Strength", "Size",
        lambda o: (strength_group(o), size(o)), ("Weak or decaf", "Regular strength", "Extra shot or double"), SIZES,
        lambda r, c: f"{c} drinks are {_STRENGTH_PHRASE[r]} more often",
        "The department’s choice of strength shows no clear link with size.",
        "If strength and size were chosen independently",
    ),
    Pair(
        "sugar_drink", "Sugar and drink", "Sugar", "Drink",
        lambda o: (sugar(o), drink_group(o)), ("No sugar", "Sugar or honey"), tuple(DRINK_GROUPS),
        lambda r, c: f"{c} drinks come {'without sugar' if r == 'No sugar' else 'with sugar or honey'} more often",
        "The department’s use of sugar shows no clear link with the kind of drink.",
        "If sugar and the kind of drink were chosen independently",
    ),
)


@dataclass
class Cell:
    observed: int
    expected: float
    contribution: float
    residual: float
    tone: str  # "above", "below" or "even"
    strength: float  # 0..1, how far from what independence predicts


@dataclass
class HistBar:
    x: float
    w: float
    h: float  # 0..1 of the chart height
    as_extreme: bool


@dataclass
class PairView:
    key: str
    tab: str
    row_name: str
    col_name: str
    n: int
    ready: bool
    test: Optional[ChiSquare] = None
    rows: list[str] = field(default_factory=list)
    cols: list[str] = field(default_factory=list)
    row_totals: list[int] = field(default_factory=list)
    col_totals: list[int] = field(default_factory=list)
    cells: list[list[Cell]] = field(default_factory=list)
    p_holm: Optional[float] = None
    finding: Optional[str] = None
    hist: list[HistBar] = field(default_factory=list)
    curve: str = ""  # SVG path of the χ² density, in the histogram's frame
    marker_x: float = 0.0
    axis_max: float = 0.0
    standout: Optional[tuple[str, str]] = None  # the cell furthest above expected
    tests: int = 1  # pairs in the Holm family
    no_link: str = ""
    premise: str = ""
    ticks: list[tuple[float, str]] = field(default_factory=list)  # (percent, label)


HIST_H = 140
HIST_BINS = 24


def _chi2_pdf(x: float, df: int) -> float:
    k = df / 2
    return math.exp((k - 1) * math.log(x) - x / 2 - k * math.log(2) - math.lgamma(k))


def _histogram(test: ChiSquare) -> tuple[list[HistBar], str, float, float]:
    """The shuffled χ² values as a density histogram, the textbook χ²
    density over it, and where the real statistic falls."""
    null = sorted(test.null)
    q99 = null[int(0.99 * (len(null) - 1))]
    top = max(q99, test.chi2, 3.0 * test.df) * 1.12
    width = top / HIST_BINS
    counts = Counter(min(HIST_BINS - 1, int(x / width)) for x in null)
    densities = [counts[b] / (len(null) * width) for b in range(HIST_BINS)]
    xs = [top * (i + 0.5) / 80 for i in range(80)]
    curve_y = [_chi2_pdf(x, test.df) for x in xs]
    peak = max(max(densities), *curve_y[2:]) or 1.0
    to_x = lambda v: v / top * CHART_W
    bars = [
        HistBar(
            x=to_x(b * width) + 0.75, w=to_x(width) - 1.5, h=min(1.0, d / peak),
            as_extreme=(b + 1) * width > test.chi2,
        )
        for b, d in enumerate(densities)
    ]
    points = [
        f"{to_x(x):.1f},{HIST_H - min(1.0, y / peak) * (HIST_H - 4):.1f}"
        for x, y in zip(xs, curve_y)
    ]
    return bars, "M" + " L".join(points), to_x(min(test.chi2, top)), top


@lru_cache(maxsize=32)
def _pair_test(pairs: tuple[tuple[str, str], ...]) -> Optional[ChiSquare]:
    return chi_square(pairs)


def _pair_view(pair: Pair, kept: tuple[tuple[str, str], ...]) -> PairView:
    """One pair's test laid out for drawing: the table in display order with
    each cell's working, and the permutation histogram."""
    view = PairView(pair.key, pair.tab, pair.row_name, pair.col_name, len(kept), False,
                    no_link=pair.no_link, premise=pair.premise)
    test = _pair_test(kept) if len(kept) >= MIN_FOR_PAIRING else None
    if test is None:
        return view
    view.ready, view.test = True, test
    rows = [r for r in pair.rows if r in test.rows]
    cols = [c for c in pair.cols if c in test.cols]
    ri = [test.rows.index(r) for r in rows]
    ci = [test.cols.index(c) for c in cols]
    view.rows, view.cols = rows, cols
    view.row_totals = [sum(test.observed[i]) for i in ri]
    view.col_totals = [sum(test.observed[i][j] for i in ri) for j in ci]
    for i in ri:
        line = []
        for j in ci:
            res = test.residual(i, j)
            line.append(Cell(
                observed=test.observed[i][j],
                expected=test.expected[i][j],
                contribution=test.contribution(i, j),
                residual=res,
                tone="even" if abs(res) < 0.5 else ("above" if res > 0 else "below"),
                strength=min(1.0, abs(res) / 2.5),
            ))
        view.cells.append(line)
    view.hist, view.curve, view.marker_x, view.axis_max = _histogram(test)
    step = next(st for st in (1, 2, 5, 10, 20, 50, 100) if view.axis_max / st <= 6)
    view.ticks = [
        (t / view.axis_max * 100, str(t)) for t in range(0, int(view.axis_max) + 1, step)
    ]
    view.standout = max(
        ((r, c) for r in rows for c in cols),
        key=lambda rc: test.residual(test.rows.index(rc[0]), test.cols.index(rc[1])),
    )
    return view


def _kept(pairs) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((r, c) for r, c in pairs if r is not None and c is not None))


def build_pairs(usuals: list[Usual]) -> list[PairView]:
    """Every pair with its χ² working, Holm-adjusted across the pairs that
    could be tested. A finding is stated only when it survives that. Pairs
    that can be tested come first, strongest evidence first."""
    views = [(pair, _pair_view(pair, _kept(pair.pick(u.options) for u in usuals))) for pair in PAIRS]
    tested = [v for _, v in views if v.ready]
    for v, p in zip(tested, holm_adjust([v.test.p_permutation for v in tested])):
        v.p_holm, v.tests = p, len(tested)
    for pair, v in views:
        if v.ready and v.p_holm < 0.05:
            v.finding = pair.sentence(*v.standout)
    ordered = sorted((v for _, v in views), key=lambda v: (not v.ready, v.p_holm if v.ready else 1.0))
    return ordered


# ---------------------------------------------------------------------------
# Randomisation and the baseline table.
# ---------------------------------------------------------------------------

RANDOMISATIONS = 1000
P_BINS = 20

# (label, how to read it off a drink, "number" or "category", level order)
BASELINE = (
    ("Caffeine, in espresso shots", shots_of, "number", ()),
    ("Drink", drink_group, "category", tuple(DRINK_GROUPS)),
    ("Milk", lambda o: milk_kind(o) or "No milk", "category", ("Dairy", "Plant", "No milk")),
    ("Size", lambda o: size(o) or "No size", "category", SIZES + ("No size",)),
    ("Sugar", sugar, "category", ("No sugar", "Sugar or honey")),
)


@dataclass
class BaselineRow:
    label: str
    arms: tuple[str, str]  # "12 (52%)" or "1 (1 to 1.5)"
    level: bool  # an indented level under a categorical variable


@dataclass
class BaselineVariable:
    label: str
    summary: str  # how the arms are summarised, for the first column
    rows: list[BaselineRow]
    p: float


@dataclass
class Randomisation:
    n: int
    arm_sizes: tuple[int, int]
    allocation: int  # which allocation is shown; the button asks for the next
    variables: list[BaselineVariable]
    any_below: bool  # this table has a p below 0.05
    p_bars: list[tuple[float, float, int]]  # (x, height 0..1, count) per 0.05 bin
    expected_x: float  # height 0..1 of the even-spread line
    share_below: float  # of all baseline p-values, the share below 0.05
    tables_with_any: float  # share of tables with at least one below 0.05
    tables_with_any_holm: float  # the same after a Holm correction per table


def _fmt_shots(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _number_cell(values: list[float]) -> str:
    if not values:
        return "None"
    q = statistics.quantiles(values, n=4, method="inclusive") if len(values) > 1 else [values[0]] * 3
    return f"{_fmt_shots(statistics.median(values))} ({_fmt_shots(q[0])} to {_fmt_shots(q[2])})"


@lru_cache(maxsize=8)
def _p_values(data: tuple[tuple, ...]) -> tuple[tuple[float, ...], ...]:
    variables = [(column, kind) for column, (_, _, kind, _) in zip(data, BASELINE)]
    return tuple(randomised_baseline_ps(variables, RANDOMISATIONS, seed=10_000))


def build_randomisation(usuals: list[Usual], allocation: int) -> Optional[Randomisation]:
    if len(usuals) < MIN_FOR_PAIRING:
        return None
    columns = [[read(u.options) for u in usuals] for _, read, _, _ in BASELINE]
    arms = allocate(len(usuals), allocation)
    sizes = (arms.count(0), arms.count(1))
    variables = []
    for (label, _, kind, levels), values in zip(BASELINE, columns):
        p = baseline_p(values, kind, arms)
        if kind == "number":
            cells = tuple(_number_cell([v for v, a in zip(values, arms) if a == arm]) for arm in (0, 1))
            variables.append(BaselineVariable(label, "Median (interquartile range)", [BaselineRow(label, cells, False)], p))
            continue
        rows = []
        for level in levels:
            cells = []
            for arm in (0, 1):
                k = sum(1 for v, a in zip(values, arms) if a == arm and v == level)
                cells.append(f"{k} ({round(100 * k / sizes[arm])}%)")
            if any(v == level for v in values):
                rows.append(BaselineRow(level, tuple(cells), True))
        variables.append(BaselineVariable(label, "Number (%)", rows, p))

    all_ps = _p_values(tuple(tuple(c) for c in columns))
    flat = [p for row in all_ps for p in row]
    bins = Counter(min(P_BINS - 1, int(p * P_BINS)) for p in flat)
    peak = max(max(bins.values()), len(flat) / P_BINS)
    return Randomisation(
        n=len(usuals),
        arm_sizes=sizes,
        allocation=allocation,
        variables=variables,
        any_below=any(v.p < 0.05 for v in variables),
        p_bars=[(b * CHART_W / P_BINS, bins[b] / peak, bins[b]) for b in range(P_BINS)],
        expected_x=(len(flat) / P_BINS) / peak,
        share_below=sum(1 for p in flat if p < 0.05) / len(flat),
        tables_with_any=sum(1 for row in all_ps if min(row) < 0.05) / len(all_ps),
        tables_with_any_holm=sum(1 for row in all_ps if min(holm_adjust(row)) < 0.05) / len(all_ps),
    )


