"""The panels that teach a method on the department's own data: options that
go together (a χ² test), randomisation and the baseline table, uncertainty
in the caffeine estimate, and colleagues who have moved on. Each builds the
answer and its working, laid out as plain numbers, so the templates only
draw. The heavy parts are cached on the data they were computed from."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional

from app.menu import (
    MG_PER_SHOT,
    MG_PER_SHOT_SD,
    SIZE_LABELS,
    Strength,
    Sweetener,
    ausnut_caffeine_mg,
    caffeine_parts,
    estimated_caffeine_mg,
)
from app.stats import (
    BlandAltman,
    ChiSquare,
    Interval,
    allocate,
    baseline_p,
    bland_altman,
    chi_square,
    cohen_kappa,
    holm_adjust,
    n2_for_power,
    randomised_baseline_ps,
    simulate_caffeine,
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
        return "Stronger"
    if o["strength"] in (Strength.WEAK.value, Strength.DECAF.value):
        return "Milder"
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


_STRENGTH_PHRASE = {"Milder": "weak or decaf", "Stronger": "made stronger", "Regular strength": "regular strength"}

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
        lambda o: (strength_group(o), size(o)), ("Milder", "Regular strength", "Stronger"), SIZES,
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
        v.p_holm = p
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


# ---------------------------------------------------------------------------
# Uncertainty in the caffeine estimate.
# ---------------------------------------------------------------------------



@dataclass
class Scenario:
    key: str
    interval: Interval
    low_x: float  # percent along the axis
    high_x: float
    estimate_x: float
    strip: list[tuple[float, float]]  # (percent along the axis, height 0..1)


@dataclass
class CaffeineSim:
    drinks: int
    shots: float
    estimate_g: float
    scenarios: list[Scenario]
    ticks: list[tuple[float, str]]  # (percent, grams)
    shot_sd: int


@lru_cache(maxsize=8)
def _simulate(parts: tuple[tuple[float, float], ...]) -> dict[str, Interval]:
    return simulate_caffeine(parts, MG_PER_SHOT, MG_PER_SHOT_SD)


def build_caffeine_sim(usuals: list[Usual]) -> Optional[CaffeineSim]:
    if not usuals:
        return None
    parts = tuple(sorted(
        caffeine_parts(
            u.options["base_id"], u.options["size"],
            u.options["strength"] or Strength.REGULAR.value, u.options["shots"],
        )
        for u in usuals
    ))
    sims = _simulate(parts)
    lo = min(min(s.samples) for s in sims.values())
    hi = max(max(s.samples) for s in sims.values())
    step = 2000 if hi - lo > 8000 else 1000 if hi - lo > 4000 else 500
    lo, hi = math.floor(lo / step) * step, math.ceil(hi / step) * step
    pct = lambda mg: (mg - lo) / (hi - lo) * 100
    scenarios = []
    for key in ("one_cafe", "separate"):
        s = sims[key]
        bins = Counter(min(59, int((x - lo) / (hi - lo) * 60)) for x in s.samples)
        top = max(bins.values())
        scenarios.append(Scenario(
            key, s, pct(s.low), pct(s.high), pct(s.estimate),
            [(b / 60 * 100, bins[b] / top) for b in range(60) if bins[b]],
        ))
    ticks = [(pct(mg), f"{mg / 1000:g} g") for mg in range(lo, hi + 1, step)]
    return CaffeineSim(
        drinks=len(usuals),
        shots=sum(n for n, _ in parts),
        estimate_g=sims["one_cafe"].estimate / 1000,
        scenarios=scenarios,
        ticks=ticks,
        shot_sd=MG_PER_SHOT_SD,
    )


# ---------------------------------------------------------------------------
# Colleagues who have moved on: survivorship.
# ---------------------------------------------------------------------------

MIN_MOVED_ON = 10

MOVED_ON = Pair(
    "inactive", "Inactive", "Milk", "Colleagues",
    lambda o: (None, None), ("Dairy", "Plant"), ("Active", "Inactive"),
    lambda r, c: f"{c} colleagues chose {r.lower()} milk more often",
    "Inactive colleagues chose plant milk about as often as active colleagues.",
    "If inactive colleagues’ milk were no different from active colleagues’",
)


@dataclass
class MovedOn:
    active: int
    inactive: int
    ready: bool
    plant_active: Optional[float]  # share of milk drinks with plant milk
    plant_inactive: Optional[float]
    needed: Optional[int]  # colleagues who have moved on needed for 80% power
    pair: Optional[PairView] = None


def build_moved_on(everyone: list[Usual]) -> MovedOn:
    active = [u for u in everyone if u.active]
    inactive = [u for u in everyone if not u.active]

    def plant_share(group: list[Usual]) -> Optional[float]:
        kinds = [milk_kind(u.options) for u in group if milk_kind(u.options)]
        return sum(1 for k in kinds if k == "Plant") / len(kinds) if kinds else None

    pa = plant_share(active)
    needed = None
    milk_active = sum(1 for u in active if milk_kind(u.options))
    if pa is not None and milk_active:
        # The difference worth detecting: 20 percentage points either way.
        target = pa + 0.2 if pa <= 0.5 else pa - 0.2
        needed = n2_for_power(pa, target, milk_active)
    out = MovedOn(len(active), len(inactive), len(inactive) >= MIN_MOVED_ON, pa, plant_share(inactive), needed)
    if out.ready:
        out.pair = _pair_view(MOVED_ON, _kept(
            (milk_kind(u.options), "Active" if u.active else "Inactive") for u in everyone
        ))
        if out.pair.ready:
            out.pair.p_holm = out.pair.test.p_permutation  # a single test
            if out.pair.p_holm < 0.05:
                out.pair.finding = MOVED_ON.sentence(*out.pair.standout)
    return out


# ---------------------------------------------------------------------------
# Two caffeine references: Bland–Altman agreement and kappa.
# ---------------------------------------------------------------------------


@dataclass
class Agreement:
    n: int  # usual drinks with an entry in both references
    left_out: int
    ba: Optional[BlandAltman] = None
    ratio: Optional[float] = None  # AUSNUT's total over ours
    # The ratio analysis Bland and Altman recommend when the difference grows
    # with the size: the geometric mean of AUSNUT ÷ Desbrow, and the range
    # holding 95% of the drinks' ratios. Drinks with no caffeine in either
    # reference are left out of it.
    gm_ratio: Optional[float] = None
    ratio_low: Optional[float] = None
    ratio_high: Optional[float] = None
    kinds: int = 0  # distinct kinds of usual drink compared
    kappa: Optional[float] = None
    wkappa: Optional[float] = None
    # Bland–Altman plot: (x, y, radius, tip) in a 600 × 200 frame, and the
    # y of the bias and limit lines, and the proportional-bias line.
    points: list[tuple[float, float, float, str]] = field(default_factory=list)
    bias_y: float = 0.0
    lower_y: float = 0.0
    upper_y: float = 0.0
    zero_y: float = 0.0
    trend: Optional[tuple[float, float, float, float]] = None
    x_ticks: list[tuple[float, str]] = field(default_factory=list)
    y_ticks: list[tuple[float, str]] = field(default_factory=list)
    scatter: list[tuple[float, float, float]] = field(default_factory=list)  # 200 × 200
    bands: list[str] = field(default_factory=list)
    table: list[list[int]] = field(default_factory=list)  # ours (rows) by AUSNUT (cols)


BA_W, BA_H = 600, 200


def build_agreement(usuals: list[Usual]) -> Optional[Agreement]:
    if not usuals:
        return None
    pairs = []
    for u in usuals:
        o = u.options
        args = (o["base_id"], o["size"], o["strength"] or Strength.REGULAR.value, o["shots"])
        theirs = ausnut_caffeine_mg(*args)
        if theirs is not None:
            pairs.append((estimated_caffeine_mg(*args), theirs))
    out = Agreement(len(pairs), len(usuals) - len(pairs))
    if len(pairs) < MIN_FOR_PAIRING:
        return out
    ours = [a for a, _ in pairs]
    theirs = [b for _, b in pairs]
    out.ba = ba = bland_altman(ours, theirs)
    out.ratio = sum(theirs) / sum(ours) if sum(ours) else None

    # Bland–Altman plot, identical points drawn as one larger dot.
    counts = Counter(pairs)
    x_max = max((a + b) / 2 for a, b in pairs) * 1.1
    y_lo = min(0.0, ba.lower, min(b - a for a, b in pairs)) - 20
    y_hi = max(ba.upper, max(b - a for a, b in pairs)) + 20
    px = lambda m: m / x_max * BA_W
    py = lambda d: BA_H - (d - y_lo) / (y_hi - y_lo) * BA_H
    out.points = [
        (px((a + b) / 2), py(b - a), 3 + 2.2 * math.sqrt(k),
         f"{k} usual {'drink' if k == 1 else 'drinks'}: {a:.0f} mg against {b:.0f} mg")
        for (a, b), k in sorted(counts.items())
    ]
    out.bias_y, out.lower_y, out.upper_y, out.zero_y = py(ba.bias), py(ba.lower), py(ba.upper), py(0)
    mean_of_means = statistics.fmean((a + b) / 2 for a, b in pairs)
    line = lambda m: ba.bias + ba.slope * (m - mean_of_means)
    out.trend = (0, py(line(0)), BA_W, py(line(x_max)))
    step = 100 if x_max > 300 else 50
    out.x_ticks = [(m / x_max * 100, f"{m}") for m in range(0, int(x_max) + 1, step)]
    y_step = 50 if y_hi - y_lo > 150 else 25
    first = math.ceil(y_lo / y_step) * y_step
    out.y_ticks = [((py(v)) / BA_H * 100, f"{v:+d}" if v else "0") for v in range(int(first), int(y_hi) + 1, y_step)]

    top = max(max(ours), max(theirs)) * 1.05
    out.scatter = [(a / top * 200, 200 - b / top * 200, 2.5 + 1.8 * math.sqrt(k)) for (a, b), k in counts.items()]

    # Both references placed in the same half-shot bands.
    from app.stats_view import CAFFEINE_ROWS, half_shots

    ra = [half_shots(a) for a in ours]
    rb = [half_shots(b) for b in theirs]
    k = len(CAFFEINE_ROWS)
    out.bands = list(CAFFEINE_ROWS)
    out.table = [[sum(1 for x, y in zip(ra, rb) if x == i and y == j) for j in range(k)] for i in range(k)]
    out.kappa = cohen_kappa(ra, rb, k)
    out.wkappa = cohen_kappa(ra, rb, k, weighted=True)
    logs = [math.log(b / a) for a, b in pairs if a > 0 and b > 0]
    if len(logs) >= 2:
        m, sd = statistics.fmean(logs), statistics.stdev(logs)
        out.gm_ratio = math.exp(m)
        out.ratio_low, out.ratio_high = math.exp(m - 1.96 * sd), math.exp(m + 1.96 * sd)
    out.kinds = len(counts)
    return out
