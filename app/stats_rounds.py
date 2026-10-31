"""The round-log panels: the rounds are a sample of the department's coffee
runs, linked to the people in them, so these report intervals and handle a
person's repeat drinks as one person's. Each panel waits, with a power
calculation, until there are enough rounds to say anything; the views hold
plain numbers and SVG coordinates so the templates only draw. Nothing here
names anyone: only aggregates, and the viewer's own counts."""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.config import get_settings
from app.models import Round, RoundDrink
from app.rounds import DRINK_FIELDS
from app.stats import (
    OddsRatio,
    Roc,
    Welch,
    cluster_bootstrap_diff,
    cusum,
    dispersion,
    fisher_exact,
    fragility_index,
    gini_interval,
    kaplan_meier,
    km_median,
    lorenz,
    mantel_haenszel,
    n_per_group_for_means,
    poisson_pmf,
    roc,
    share_for_half,
    spearman,
    tost,
    welch,
)
from app.stats_panels import DRINK_GROUPS, drink_group, shots_of
from app.stats_view import CHART_W, Usual, as_options, get_drink
from app.users import _as_naive_utc

CHART_H = 160


@dataclass
class LoggedDrink:
    person: str  # user id, or a one-off key for a guest or a detached drink
    options: dict
    shots: float
    guest: bool


@dataclass
class LoggedRound:
    placed: datetime  # local time
    size: int
    drinks: list[LoggedDrink]

    @property
    def afternoon(self) -> bool:
        return self.placed.hour >= 12

    @property
    def minutes(self) -> int:
        return self.placed.hour * 60 + self.placed.minute


def load_rounds(session: Session) -> list[LoggedRound]:
    """Every round with its drinks, oldest first, in one statement."""
    tz = ZoneInfo(get_settings().timezone)
    q = (
        select(Round.id, Round.placed_at, Round.drink_count, RoundDrink.id, RoundDrink.user_id,
               RoundDrink.guest, *(getattr(RoundDrink, f) for f in DRINK_FIELDS if f != "own_cup"))
        .join(RoundDrink, RoundDrink.round_id == Round.id)
        .order_by(Round.placed_at, Round.id)
    )
    rounds: dict[int, LoggedRound] = {}
    for rid, placed, size, did, uid, guest, *drink in session.exec(q):
        if rid not in rounds:
            local = _as_naive_utc(placed).replace(tzinfo=timezone.utc).astimezone(tz)
            rounds[rid] = LoggedRound(local, size, [])
        if get_drink(drink[0]) is None:
            continue
        opts = as_options(*drink)
        rounds[rid].drinks.append(LoggedDrink(uid or f"drink-{did}", opts, shots_of(opts), bool(guest)))
    return list(rounds.values())


def _fmt(x: float, places: int = 1) -> str:
    return f"{x:.{places}f}"


def _ticks(top: float, count: int = 5) -> list[tuple[float, str]]:
    """Round-number ticks from 0 to top, as (percent, label)."""
    step = next(s for s in (0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100) if top / s <= count)
    out, v = [], 0.0
    while v <= top + 1e-9:
        out.append((v / top * 100, f"{v:g}"))
        v += step
    return out


# ---------------------------------------------------------------------------
# Caffeine through the day: before and after midday.
# ---------------------------------------------------------------------------

MIN_EACH_SIDE = 20  # drinks either side of midday


@dataclass
class Dot:
    x: float
    y: float
    tip: str


@dataclass
class Afternoon:
    ready: bool
    before: int  # drinks before midday
    after: int
    needed: int  # drinks either side for 80% power to detect half a shot
    mean_before: Optional[float] = None
    mean_after: Optional[float] = None
    naive: Optional[Welch] = None  # treating every drink as independent
    clustered: Optional[tuple[float, float, float]] = None  # resampling people
    people: int = 0
    crude: Optional[OddsRatio] = None  # odds of a stronger drink after midday
    adjusted: Optional[OddsRatio] = None  # Mantel–Haenszel, within drink groups
    strata: list[tuple[str, int, int, int, int]] = field(default_factory=list)
    posterior_input: Optional[tuple[float, float]] = None  # (difference, SE) for the slider
    roc: Optional[Roc] = None
    roc_path: str = ""
    dots: list[Dot] = field(default_factory=list)  # each round's mean, by time of day
    dot_ticks: list[tuple[float, str]] = field(default_factory=list)
    y_ticks: list[tuple[float, str]] = field(default_factory=list)
    midday_x: float = 0.0
    # Error bars, as percentages of the interval axis: (estimate, low, high).
    axis: list[tuple[float, str]] = field(default_factory=list)
    bar_naive: Optional[tuple[float, float, float]] = None
    bar_clustered: Optional[tuple[float, float, float]] = None


def _stronger(d: LoggedDrink) -> bool:
    return d.shots >= 2


def build_afternoon(rounds: list[LoggedRound], usuals: list[Usual]) -> Afternoon:
    drinks = [(r, d) for r in rounds for d in r.drinks]
    before = [d.shots for r, d in drinks if not r.afternoon]
    after = [d.shots for r, d in drinks if r.afternoon]
    census_sd = statistics.stdev([shots_of(u.options) for u in usuals]) if len(usuals) > 1 else 0.7
    out = Afternoon(
        ready=len(before) >= MIN_EACH_SIDE and len(after) >= MIN_EACH_SIDE,
        before=len(before), after=len(after),
        needed=n_per_group_for_means(0.5, max(census_sd, 0.1)),
    )
    if not out.ready:
        return out
    out.mean_before, out.mean_after = statistics.fmean(before), statistics.fmean(after)
    out.naive = welch(after, before)
    out.clustered = cluster_bootstrap_diff([(d.person, int(r.afternoon), d.shots) for r, d in drinks])
    out.people = len({d.person for _, d in drinks})

    # Odds of a stronger drink after midday, crude and within drink groups.
    strata = []
    for group in DRINK_GROUPS:
        cell = [r_d for r_d in drinks if drink_group(r_d[1].options) == group]
        a = sum(1 for r, d in cell if r.afternoon and _stronger(d))
        b = sum(1 for r, d in cell if r.afternoon and not _stronger(d))
        c = sum(1 for r, d in cell if not r.afternoon and _stronger(d))
        dd = sum(1 for r, d in cell if not r.afternoon and not _stronger(d))
        strata.append((group, a, b, c, dd))
    out.strata = strata
    mh = mantel_haenszel([s[1:] for s in strata])
    if mh:
        out.crude, out.adjusted = mh

    if out.clustered:
        est, lo, hi = out.clustered
        out.posterior_input = (est, max((hi - lo) / (2 * 1.96), 1e-3))

    out.roc = roc([d.shots for _, d in drinks], [r.afternoon for r, _ in drinks])
    if out.roc:
        out.roc_path = "M" + " L".join(f"{x * 200:.1f},{200 - y * 200:.1f}" for x, y in out.roc.points)

    # Each round's mean shots against its time of day.
    lo_t = min(7 * 60, min(r.minutes for r in rounds))
    hi_t = max(17 * 60, max(r.minutes for r in rounds))
    top = max(3.0, max(statistics.fmean(d.shots for d in r.drinks) for r in rounds if r.drinks))
    for r in rounds:
        if r.drinks:
            m = statistics.fmean(d.shots for d in r.drinks)
            out.dots.append(Dot(
                (r.minutes - lo_t) / (hi_t - lo_t) * CHART_W, CHART_H - m / top * (CHART_H - 6),
                f"A round of {len(r.drinks)}: {_fmt(m)} shots a drink",
            ))
    from app.stats_view import _clock
    out.dot_ticks = [((t - lo_t) / (hi_t - lo_t) * 100, _clock(t)) for t in range(lo_t - lo_t % 120 + 120, hi_t, 120)]
    out.y_ticks = [(100 - p, label) for p, label in _ticks(top, 3)]
    out.midday_x = (12 * 60 - lo_t) / (hi_t - lo_t) * CHART_W

    # The two intervals on one axis.
    lows = [out.naive.low] + ([out.clustered[1]] if out.clustered else [])
    highs = [out.naive.high] + ([out.clustered[2]] if out.clustered else [])
    span = max(0.5, max(abs(v) for v in lows + highs)) * 1.15
    pct = lambda v: (v + span) / (2 * span) * 100
    out.axis = [(p, f"{v:+.1f}" if abs(v) > 1e-9 else "0") for p, v in ((pct(v), v) for v in (-span, -span / 2, 0, span / 2, span))]
    out.bar_naive = (pct(out.naive.difference), pct(out.naive.low), pct(out.naive.high))
    if out.clustered:
        out.bar_clustered = tuple(pct(v) for v in out.clustered)
    return out


# ---------------------------------------------------------------------------
# Time to the next coffee: Kaplan–Meier over the gaps between a person's
# rounds, with the gap still open at the end of the log censored.
# ---------------------------------------------------------------------------

MIN_GAPS = 15
KM_DAYS = 42


@dataclass
class NextCoffee:
    ready: bool
    gaps: int  # completed gaps between one person's rounds
    censored: int
    people: int
    median: Optional[float] = None
    median_low: Optional[float] = None
    median_high: Optional[float] = None
    path: str = ""
    band: str = ""
    censor_marks: list[tuple[float, float]] = field(default_factory=list)
    ticks: list[tuple[float, str]] = field(default_factory=list)
    at_risk: list[tuple[float, int]] = field(default_factory=list)
    half_y: float = CHART_H / 2


def build_next_coffee(rounds: list[LoggedRound], now: Optional[datetime] = None) -> NextCoffee:
    times: dict[str, list[datetime]] = defaultdict(list)
    for r in rounds:
        for d in r.drinks:
            if not d.person.startswith("drink-") and not d.guest:
                times[d.person].append(r.placed)
    if now is None and rounds:
        now = datetime.now(timezone.utc).astimezone(rounds[-1].placed.tzinfo)
    durations, events = [], []
    for seen in times.values():
        seen = sorted(set(seen))
        for a, b in zip(seen, seen[1:]):
            durations.append((b - a).total_seconds() / 86400)
            events.append(True)
        durations.append(max((now - seen[-1]).total_seconds() / 86400, 0.01))
        events.append(False)
    out = NextCoffee(sum(events) >= MIN_GAPS, sum(events), len(events) - sum(events), len(times))
    if not out.ready:
        return out
    steps = kaplan_meier(durations, events)
    out.median, out.median_low, out.median_high = km_median(steps)
    span = min(KM_DAYS, max(7.0, max(durations)))
    x = lambda t: min(t, span) / span * CHART_W
    y = lambda s: CHART_H - s * (CHART_H - 4)
    path, upper, lower = [f"M0,{y(1):.1f}"], [], []
    prev = steps[0]
    for st in steps[1:]:
        if st.t > span:
            break
        path.append(f"H{x(st.t):.1f} V{y(st.survival):.1f}")
        upper.append((x(st.t), y(prev.high)))
        upper.append((x(st.t), y(st.high)))
        lower.append((x(st.t), y(prev.low)))
        lower.append((x(st.t), y(st.low)))
        prev = st
    path.append(f"H{CHART_W}")
    out.path = " ".join(path)
    if upper:
        pts = [(0, y(1))] + upper + [(CHART_W, upper[-1][1]), (CHART_W, lower[-1][1])] + lower[::-1] + [(0, y(1))]
        out.band = "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in pts)
    survival_at = lambda t: next((st.survival for st in reversed(steps) if st.t <= t), 1.0)
    out.censor_marks = [
        (x(t), y(survival_at(t))) for t, e in zip(durations, events) if not e and t <= span
    ]
    step = 7 if span > 14 else 2
    out.ticks = [(t / span * 100, f"{t:g}") for t in range(0, int(span) + 1, step)]
    out.at_risk = [(t / span * 100, sum(1 for d in durations if d >= t)) for t in range(0, int(span) + 1, step)]
    out.half_y = y(0.5)
    return out


# ---------------------------------------------------------------------------
# Who comes for coffee: how concentrated the drinks are, whether regulars
# drink stronger coffee, and how the rounds' mix differs from the census.
# ---------------------------------------------------------------------------

MIN_FOR_ATTENDANCE = 20  # rounds


@dataclass
class Attendance:
    ready: bool
    rounds: int
    mine: Optional[int]  # the viewer's own rounds, shown only to them
    gini: Optional[float] = None
    gini_low: Optional[float] = None
    gini_high: Optional[float] = None
    half_share: Optional[float] = None  # share of colleagues behind half the drinks
    people: int = 0  # colleagues on the roster in the comparison
    regulars_rho: Optional[float] = None
    regulars_p: Optional[float] = None
    lorenz_path: str = ""
    mix: list[tuple[str, float, float]] = field(default_factory=list)  # (group, census, rounds)


def build_attendance(rounds: list[LoggedRound], usuals: list[Usual], viewer_id: str) -> Attendance:
    counts = Counter(d.person for r in rounds for d in r.drinks)
    out = Attendance(len(rounds) >= MIN_FOR_ATTENDANCE, len(rounds), counts.get(viewer_id) or None)
    if not out.ready or not usuals:
        return out
    per_person = [counts.get(u.user_id, 0) for u in usuals]
    points, out.gini = lorenz(per_person)
    out.gini_low, out.gini_high = gini_interval(per_person)
    out.half_share = share_for_half(per_person)
    out.people = len(usuals)
    out.lorenz_path = "M" + " L".join(f"{px * 200:.1f},{200 - py * 200:.1f}" for px, py in points)
    came = [(counts[u.user_id], shots_of(u.options)) for u in usuals if counts.get(u.user_id)]
    if len(came) >= 8:
        out.regulars_rho, out.regulars_p = spearman([c for c, _ in came], [s for _, s in came])
    census = Counter(drink_group(u.options) for u in usuals)
    logged = Counter(drink_group(d.options) for r in rounds for d in r.drinks)
    n_c, n_l = sum(census.values()), sum(logged.values())
    out.mix = [(g, census[g] / n_c, logged[g] / n_l if n_l else 0.0) for g in DRINK_GROUPS]
    return out


# ---------------------------------------------------------------------------
# Round size: Poisson or not, regression to the mean, a CUSUM chart, and
# whether Monday and Friday rounds are the same size.
# ---------------------------------------------------------------------------

MIN_FOR_SIZE = 20


@dataclass
class RoundSize:
    ready: bool
    rounds: int
    mean: Optional[float] = None
    ratio: Optional[float] = None  # variance to mean
    ratio_p: Optional[float] = None
    bars: list[tuple[float, float, float, int, float]] = field(default_factory=list)  # (x, w, h, k, poisson h)
    size_ticks: list[tuple[float, str]] = field(default_factory=list)
    rtm_r: Optional[float] = None
    big: Optional[int] = None  # threshold for a "big" round
    after_big: Optional[float] = None  # mean size of the round after a big one
    rtm_dots: list[tuple[float, float]] = field(default_factory=list)
    rtm_line: Optional[tuple[float, float, float, float]] = None
    rtm_max: int = 0
    cusum_hi: str = ""
    cusum_lo: str = ""
    cusum_limit_y: float = 0.0
    cusum_signal: Optional[int] = None  # first round number to cross the limit
    mon_fri: Optional[tuple[Welch, float]] = None  # (Welch, TOST p) within one drink
    mon_n: int = 0
    fri_n: int = 0


def build_round_size(rounds: list[LoggedRound]) -> RoundSize:
    sizes = [r.size for r in rounds]
    out = RoundSize(len(rounds) >= MIN_FOR_SIZE, len(rounds))
    if not out.ready:
        return out
    out.mean = statistics.fmean(sizes)
    out.ratio, out.ratio_p = dispersion(sizes)
    top_k = max(sizes) + 1
    seen = Counter(sizes)
    expected = [poisson_pmf(k, out.mean) * len(sizes) for k in range(top_k + 1)]
    peak = max(max(seen.values()), max(expected))
    w = CHART_W / (top_k + 1)
    out.bars = [(k * w + 2, w - 4, seen[k] / peak, seen[k], expected[k] / peak) for k in range(top_k + 1)]
    out.size_ticks = [((k + 0.5) / (top_k + 1) * 100, str(k)) for k in range(0, top_k + 1, 2 if top_k > 12 else 1)]

    pairs = list(zip(sizes, sizes[1:]))
    if len(pairs) >= 5 and statistics.pstdev(sizes) > 0:
        xs, ys = [a for a, _ in pairs], [b for _, b in pairs]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxx = sum((a - mx) ** 2 for a in xs)
        slope = sum((a - mx) * (b - my) for a, b in pairs) / sxx if sxx else 0.0
        out.rtm_r = slope * statistics.pstdev(xs) / statistics.pstdev(ys) if statistics.pstdev(ys) else 0.0
        out.big = math.ceil(statistics.quantiles(sizes, n=4)[2])
        after = [b for a, b in pairs if a >= out.big]
        out.after_big = statistics.fmean(after) if after else None
        m = max(sizes) + 1
        out.rtm_max = m
        scale = lambda v: v / m * 200
        out.rtm_dots = [(scale(a), 200 - scale(b)) for a, b in pairs]
        out.rtm_line = (0, 200 - scale(my - slope * mx), 200, 200 - scale(my + slope * (m - mx)))

    sd = statistics.stdev(sizes) if len(sizes) > 1 else 1.0
    chart = cusum(sizes, out.mean, sd)
    limit = 4.0
    top = max(limit * 1.3, max(max(h, l) for h, l, _ in chart))
    x = lambda i: i / max(len(chart) - 1, 1) * CHART_W
    y = lambda v: CHART_H - v / top * (CHART_H - 4)
    out.cusum_hi = "M" + " L".join(f"{x(i):.1f},{y(h):.1f}" for i, (h, _, _) in enumerate(chart))
    out.cusum_lo = "M" + " L".join(f"{x(i):.1f},{y(l):.1f}" for i, (_, l, _) in enumerate(chart))
    out.cusum_limit_y = y(limit)
    out.cusum_signal = next((i + 1 for i, (_, _, s) in enumerate(chart) if s), None)

    mon = [r.size for r in rounds if r.placed.weekday() == 0]
    fri = [r.size for r in rounds if r.placed.weekday() == 4]
    out.mon_n, out.fri_n = len(mon), len(fri)
    if len(mon) >= 5 and len(fri) >= 5:
        out.mon_fri = tost(mon, fri, 1.0)
    return out


# ---------------------------------------------------------------------------
# Guests: do one-off guests order differently? Fisher's exact test, with the
# fragility index when the result is significant.
# ---------------------------------------------------------------------------

MIN_GUESTS = 5


@dataclass
class Guests:
    ready: bool
    guests: int
    table: Optional[tuple[int, int, int, int]] = None  # guest milk coffee, guest other, regular milk coffee, regular other
    p: Optional[float] = None
    fragility: Optional[int] = None


def build_guests(rounds: list[LoggedRound]) -> Guests:
    drinks = [d for r in rounds for d in r.drinks]
    guests = [d for d in drinks if d.guest]
    out = Guests(len(guests) >= MIN_GUESTS, len(guests))
    if not out.ready:
        return out
    milk = lambda d: drink_group(d.options) == "Milk coffee"
    regulars = [d for d in drinks if not d.guest]
    out.table = (
        sum(map(milk, guests)), sum(not milk(d) for d in guests),
        sum(map(milk, regulars)), sum(not milk(d) for d in regulars),
    )
    out.p = fisher_exact(*out.table)
    out.fragility = fragility_index(*out.table)
    return out


@dataclass
class RoundPanels:
    afternoon: Afternoon
    next_coffee: NextCoffee
    attendance: Attendance
    size: RoundSize
    guests: Guests


def build_round_panels(session: Session, usuals: list[Usual], viewer_id: str) -> RoundPanels:
    rounds = load_rounds(session)
    return RoundPanels(
        afternoon=build_afternoon(rounds, usuals),
        next_coffee=build_next_coffee(rounds),
        attendance=build_attendance(rounds, usuals, viewer_id),
        size=build_round_size(rounds),
        guests=build_guests(rounds),
    )
