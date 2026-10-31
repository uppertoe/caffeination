"""Statistical methods behind the statistics page. Pure functions over plain
values, no database: app.stats_view loads the data and calls these.

Department samples are small (tens of people), so each method is chosen to
stay honest at that size: permutation tests and a bias-corrected Cramér's V
beside the χ² asymptotics, a smoothed model for how unusual a drink is,
resampling by person for the round log, and circular statistics for time
of day. Each
returns its working as well as its answer, because the page shows both.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional


# ---------------------------------------------------------------------------
# A χ² test of independence between two categorical options.
# ---------------------------------------------------------------------------


def chi2_sf(x: float, df: int) -> float:
    """Upper tail of the χ² distribution, P(X ≥ x): the textbook p-value.
    The regularised upper incomplete gamma Q(df/2, x/2), by its series below
    the mean and its continued fraction above (Numerical Recipes, 6.2)."""
    if x <= 0:
        return 1.0
    a, x = df / 2, x / 2
    log_front = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:
        term = total = 1 / a
        ap = a
        for _ in range(1000):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1 - total * math.exp(log_front))
    tiny = 1e-300
    b = x + 1 - a
    c, d = 1 / tiny, 1 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1 / (d if abs(d) > tiny else tiny)
        c = b + an / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return min(1.0, math.exp(log_front) * h)


@dataclass(frozen=True)
class ChiSquare:
    """A χ² test of independence with all of its working, for the page to
    show: the observed and expected tables, the statistic, the textbook
    p-value and the permutation one, and the shuffled statistics behind it."""

    rows: tuple
    cols: tuple
    observed: tuple[tuple[int, ...], ...]
    expected: tuple[tuple[float, ...], ...]
    n: int
    chi2: float
    df: int
    p_textbook: float
    p_permutation: float
    null: tuple[float, ...]  # χ² of every shuffled table
    cramers_v: float

    def contribution(self, i: int, j: int) -> float:
        """(O − E)² / E for one cell; the cells sum to χ²."""
        o, e = self.observed[i][j], self.expected[i][j]
        return (o - e) ** 2 / e

    def residual(self, i: int, j: int) -> float:
        """Pearson residual (O − E) / √E: positive where a combination is
        more common than independence predicts."""
        o, e = self.observed[i][j], self.expected[i][j]
        return (o - e) / math.sqrt(e)

    @property
    def sparse_cells(self) -> int:
        """Cells expected to hold fewer than 5, where the textbook p-value
        stops being trustworthy (Cochran's rule)."""
        return sum(1 for row in self.expected for e in row if e < 5)

    @property
    def as_extreme(self) -> int:
        """Shuffled tables with a χ² at least as large as the real one."""
        return sum(1 for x in self.null if x >= self.chi2 - 1e-9)


def _bias_corrected_v(chi2: float, n: int, r: int, k: int) -> float:
    """Bergsma's bias-corrected Cramér's V. The plain statistic is biased
    upwards in small samples and sparse tables, enough to suggest a strong
    link between options that are in fact unrelated."""
    if n < 2:
        return 0.0
    phi2 = max(0.0, chi2 / n - (k - 1) * (r - 1) / (n - 1))
    r_corr = r - (r - 1) ** 2 / (n - 1)
    k_corr = k - (k - 1) ** 2 / (n - 1)
    denom = min(r_corr - 1, k_corr - 1)
    return math.sqrt(phi2 / denom) if denom > 0 else 0.0


def chi_square(
    pairs: Sequence[tuple[Hashable, Hashable]],
    permutations: int = 499,
    seed: int = 0,
) -> Optional[ChiSquare]:
    """χ² test of independence between two categorical options, with a
    permutation p-value beside the textbook one.

    Shuffling one column keeps both sets of margins fixed, which is the exact
    reference distribution for small, sparse tables where the χ² approximation
    is unreliable. Seeded so the page reads the same on every load. None when
    either option takes only one value, since there is nothing to test.
    """
    n = len(pairs)
    rows = sorted({r for r, _ in pairs}, key=str)
    cols = sorted({c for _, c in pairs}, key=str)
    if n == 0 or len(rows) < 2 or len(cols) < 2:
        return None
    left = [r for r, _ in pairs]
    right = [c for _, c in pairs]
    row_tot = Counter(left)
    col_tot = Counter(right)
    counts = Counter(pairs)
    observed = tuple(tuple(counts[(r, c)] for c in cols) for r in rows)
    expected = tuple(tuple(row_tot[r] * col_tot[c] / n for c in cols) for r in rows)

    # Shuffling keeps both margins, so every expected count is fixed and
    # χ² = Σ O²/E − n: score shuffles by Σ O²/E over integer cell codes
    # rather than rebuilding the table each time.
    k = len(cols)
    row_ix = {r: i for i, r in enumerate(rows)}
    col_ix = {c: j for j, c in enumerate(cols)}
    row_codes = [row_ix[r] * k for r in left]
    right_codes = [col_ix[c] for c in right]
    inv_expected = [1 / e for row in expected for e in row]

    def chi2_of(col_codes: list[int]) -> float:
        cells = [0] * len(inv_expected)
        for a, b in zip(row_codes, col_codes):
            cells[a + b] += 1
        return sum(o * o * w for o, w in zip(cells, inv_expected)) - n

    stat = chi2_of(right_codes)
    rng = random.Random(seed)
    null = []
    for _ in range(permutations):
        rng.shuffle(right_codes)
        null.append(chi2_of(right_codes))
    at_least = sum(1 for x in null if x >= stat - 1e-9)
    df = (len(rows) - 1) * (len(cols) - 1)
    return ChiSquare(
        rows=tuple(rows),
        cols=tuple(cols),
        observed=observed,
        expected=expected,
        n=n,
        chi2=stat,
        df=df,
        p_textbook=chi2_sf(stat, df),
        p_permutation=(at_least + 1) / (permutations + 1),
        null=tuple(null),
        cramers_v=_bias_corrected_v(stat, n, len(rows), len(cols)),
    )


# ---------------------------------------------------------------------------
# Randomisation and the baseline table. Allocating people to two arms at
# random and testing the arms against each other at baseline produces
# p-values spread evenly between 0 and 1: any "significant" difference is
# chance by construction. Carlisle used the same fact to find trials whose
# baseline tables were too good, or too bad, to be true.
# ---------------------------------------------------------------------------


def allocate(n: int, seed: int) -> list[int]:
    """Arm 0 or 1 for each of n people, as near half and half as n allows."""
    arms = [i % 2 for i in range(n)]
    random.Random(seed).shuffle(arms)
    return arms


def _normal_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def rank_sum_p(a: Sequence[float], b: Sequence[float]) -> float:
    """Two-sided Wilcoxon rank-sum (Mann–Whitney) p-value, by the normal
    approximation with a correction for ties."""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return 1.0
    pooled = sorted([(x, 0) for x in a] + [(x, 1) for x in b])
    ranks = [0.0] * len(pooled)
    ties = 0.0
    i = 0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, pooled) if g == 0)
    u = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    var = n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1)))
    if var <= 0:
        return 1.0
    z = (abs(u - n1 * n2 / 2) - 0.5) / math.sqrt(var)  # continuity correction
    return min(1.0, 2 * _normal_sf(max(z, 0.0)))


def chi2_textbook_p(values: Sequence[Hashable], arms: Sequence[int]) -> float:
    """The textbook χ² p-value for one categorical variable across the arms."""
    pairs = list(zip(values, arms))
    n = len(pairs)
    rows = sorted({v for v in values}, key=str)
    if n == 0 or len(rows) < 2 or len(set(arms)) < 2:
        return 1.0
    counts = Counter(pairs)
    row_tot = Counter(values)
    arm_tot = Counter(arms)
    stat = sum(
        (counts[(r, a)] - row_tot[r] * arm_tot[a] / n) ** 2 / (row_tot[r] * arm_tot[a] / n)
        for r in rows for a in (0, 1)
    )
    return chi2_sf(stat, len(rows) - 1)


def baseline_p(values: Sequence, kind: str, arms: Sequence[int]) -> float:
    """p-value comparing the two arms on one baseline variable: rank-sum for
    a number, χ² for a category, as a trial's Table 1 would."""
    if kind == "number":
        return rank_sum_p(
            [v for v, a in zip(values, arms) if a == 0],
            [v for v, a in zip(values, arms) if a == 1],
        )
    return chi2_textbook_p(values, arms)


def randomised_baseline_ps(
    variables: Sequence[tuple[Sequence, str]], randomisations: int = 1000, seed: int = 0
) -> list[tuple[float, ...]]:
    """The baseline p-values of every variable, for each of many random
    allocations of the same people."""
    n = len(variables[0][0]) if variables else 0
    out = []
    for r in range(randomisations):
        arms = allocate(n, seed + r)
        out.append(tuple(baseline_p(values, kind, arms) for values, kind in variables))
    return out


def _percentile(sorted_xs: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks."""
    if not sorted_xs:
        return math.nan
    pos = (len(sorted_xs) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


# ---------------------------------------------------------------------------
# How unusual a drink is.
# ---------------------------------------------------------------------------


class UnusualnessModel:
    """Probability of a whole drink under a smoothed independence model.

    Each option (base, milk, size, ...) is scored by its share among the
    department's drinks for which that option applies, with a Jeffreys allowance
    of half a drink for every value, so an option nobody has chosen yet is
    rare rather than impossible. A drink's probability is the product over
    its options; its surprisal is −log₂ of that. Treating options as
    independent is a simplification, but the full joint table is far too
    sparse to estimate from one department.
    """

    PRIOR = 0.5

    def __init__(self, drinks: Sequence[dict], domains: dict[str, Sequence[str]]):
        self.domains = domains
        self.counts: dict[str, Counter] = {axis: Counter() for axis in domains}
        for drink in drinks:
            for axis in domains:
                value = drink.get(axis)
                if value is not None:
                    self.counts[axis][value] += 1

    def option_probability(self, axis: str, value) -> float:
        """Smoothed share of one option among drinks for which it applies."""
        counts = self.counts[axis]
        total = sum(counts.values())
        return (counts[value] + self.PRIOR) / (total + self.PRIOR * len(self.domains[axis]))

    def probability(self, drink: dict) -> float:
        p = 1.0
        for axis in self.domains:
            value = drink.get(axis)
            if value is not None:  # None: the option does not apply to this drink
                p *= self.option_probability(axis, value)
        return p

    def surprisal(self, drink: dict) -> float:
        return -math.log2(self.probability(drink))


# ---------------------------------------------------------------------------
# Several tests at once.
# ---------------------------------------------------------------------------


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    """Holm–Bonferroni adjusted p-values, in the order given. Controls the
    chance of any false finding across the whole set, so testing four pairs
    of options does not quadruple the odds of announcing a spurious link."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[i]))
        adjusted[i] = running
    return adjusted


@lru_cache(maxsize=32)
def _even_spread_p(counts: tuple[int, ...], simulations: int, seed: int) -> float:
    n, k = sum(counts), len(counts)
    expected = n / k
    stat = lambda cs: sum((c - expected) ** 2 for c in cs)
    observed = stat(counts) - 1e-9
    if expected >= 5:
        # Cochran's rule holds, so the textbook χ² distribution is accurate
        # and the simulation, which grows with every round, is not needed.
        return chi2_sf(stat(counts) / expected, k - 1)
    rng = random.Random(seed)
    at_least = 0
    for _ in range(simulations):
        sim = [0] * k
        for _ in range(n):
            sim[rng.randrange(k)] += 1
        if stat(sim) >= observed:
            at_least += 1
    return (at_least + 1) / (simulations + 1)


def even_spread_p(counts: Sequence[int], simulations: int = 1999, seed: int = 0) -> float:
    """p-value for counts as uneven as these if every category (weekday)
    were equally likely: a χ² goodness-of-fit test. While any category
    expects fewer than 5, the reference distribution is simulated so the
    test stays exact at a handful of rounds; beyond that, the textbook χ²
    distribution is accurate and far cheaper."""
    if sum(counts) == 0 or len(counts) < 2:
        return 1.0
    return _even_spread_p(tuple(counts), simulations, seed)


# ---------------------------------------------------------------------------
# Time of day, which is circular: 23:45 and 00:15 are half an hour apart.
# ---------------------------------------------------------------------------


def half_window(minutes: Sequence[int], step: int = 15) -> Optional[tuple[int, int]]:
    """The shortest stretch of the clock holding at least half the times,
    as (start, end) in minutes after midnight; end may pass 1440 when the
    stretch crosses midnight. Each time stands for the step that follows
    it, as rounds are logged floored to the quarter hour.

    Unlike a mean and standard deviation, this stays honest when the times
    bunch in two places (a morning run and an afternoon one): it reports
    where half of them actually fall."""
    n = len(minutes)
    if n == 0:
        return None
    ordered = sorted(m % 1440 for m in minutes)
    wrapped = ordered + [m + 1440 for m in ordered]
    k = (n + 1) // 2  # at least half
    best = None
    for i in range(n):
        start, end = wrapped[i], wrapped[i + k - 1] + step
        if best is None or end - start < best[1] - best[0]:
            best = (start, end)
    return best


# ---------------------------------------------------------------------------
# Power, for the panels still waiting on data.
# ---------------------------------------------------------------------------

Z_975 = 1.959964  # two-sided 5%
Z_80 = 0.841621  # 80% power


def n_per_group_for_means(difference: float, sd: float) -> int:
    """People (or rounds) needed in each of two groups to detect a difference
    in means with 80% power at a two-sided 5% level."""
    return math.ceil(2 * ((Z_975 + Z_80) * sd / difference) ** 2)


# ---------------------------------------------------------------------------
# Methods for the round log. The rounds are a sample of the department's
# coffee runs, so these report intervals as well as estimates.
# ---------------------------------------------------------------------------


def wilson(k: int, n: int) -> tuple[float, float]:
    """Wilson 95% interval for a proportion k/n; sensible at small n and at
    proportions near 0 or 1, unlike the textbook ±1.96 SE."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    z2 = Z_975 ** 2
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = Z_975 * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def rayleigh(minutes: Sequence[float]) -> tuple[float, float, float]:
    """Mean time of day (minutes after midnight), mean resultant length R
    (0 for times spread round the clock, 1 for all at one moment), and the
    Rayleigh test p-value against times spread evenly round the clock
    (Zar's approximation)."""
    n = len(minutes)
    if n == 0:
        return math.nan, 0.0, 1.0
    angles = [2 * math.pi * m / 1440 for m in minutes]
    c = sum(math.cos(a) for a in angles)
    s = sum(math.sin(a) for a in angles)
    rn = math.hypot(c, s)
    mean = (math.degrees(math.atan2(s, c)) % 360) / 360 * 1440
    p = math.exp(math.sqrt(1 + 4 * n + 4 * (n * n - rn * rn)) - (1 + 2 * n))
    return mean, rn / n, min(1.0, max(0.0, p))


# The t distribution, via the regularised incomplete beta function.


def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        if abs(d * c - 1) < 1e-14:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1 - front * _betacf(b, a, 1 - x) / b


def t_sf(t: float, df: float) -> float:
    """Upper tail of Student's t, P(T ≥ t)."""
    tail = 0.5 * _betainc(df / 2, 0.5, df / (df + t * t))
    return tail if t >= 0 else 1 - tail


def t_quantile(q: float, df: float) -> float:
    """The t value with P(T ≤ t) = q, by bisection."""
    lo, hi = -50.0, 50.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 1 - t_sf(mid, df) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


@dataclass(frozen=True)
class Welch:
    difference: float  # mean(a) − mean(b)
    se: float
    df: float
    p: float  # two-sided, for no difference
    low: float
    high: float


def welch(a: Sequence[float], b: Sequence[float]) -> Optional[Welch]:
    """Welch's t-test and 95% interval for a difference in means, without
    assuming the two groups share a variance."""
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = statistics.variance(a) / len(a), statistics.variance(b) / len(b)
    se = math.sqrt(va + vb)
    diff = statistics.fmean(a) - statistics.fmean(b)
    if se == 0:
        return Welch(diff, 0.0, math.inf, 1.0 if diff == 0 else 0.0, diff, diff)
    df = (va + vb) ** 2 / (va * va / (len(a) - 1) + vb * vb / (len(b) - 1))
    tq = t_quantile(0.975, df)
    return Welch(diff, se, df, min(1.0, 2 * t_sf(abs(diff) / se, df)), diff - tq * se, diff + tq * se)


@lru_cache(maxsize=8)
def _cluster_bootstrap_diff(
    sums: tuple[tuple[float, int, float, int], ...], resamples: int, seed: int
) -> Optional[tuple[float, float, float]]:
    def diff(rows) -> Optional[float]:
        s1 = n1 = s0 = n0 = 0.0
        for a, b, c, d in rows:
            s1 += a
            n1 += b
            s0 += c
            n0 += d
        if not n1 or not n0:
            return None
        return s1 / n1 - s0 / n0

    est = diff(sums)
    if est is None or len(sums) < 2:
        return None
    rng = random.Random(seed)
    k = len(sums)
    out = []
    for _ in range(resamples):
        d = diff([sums[rng.randrange(k)] for _ in range(k)])
        if d is not None:
            out.append(d)
    out.sort()
    return est, _percentile(out, 0.025), _percentile(out, 0.975)


def cluster_bootstrap_diff(
    items: Sequence[tuple[Hashable, int, float]], resamples: int = 2000, seed: int = 0
) -> Optional[tuple[float, float, float]]:
    """Difference in mean value between group 1 and group 0, with a 95%
    percentile interval from resampling whole clusters (people) with
    replacement, so a person's repeat drinks count as one person's evidence.
    Items are (cluster, group, value). Returns (difference, low, high).

    Each cluster is reduced first to its sums and counts in each group, so a
    resample adds up clusters rather than every drink, and the result is
    cached on those sums."""
    totals: dict[Hashable, list[float]] = {}
    for c, g, v in items:
        t = totals.setdefault(c, [0.0, 0, 0.0, 0])
        if g == 1:
            t[0] += v
            t[1] += 1
        else:
            t[2] += v
            t[3] += 1
    sums = tuple(sorted(tuple(t) for t in totals.values()))
    return _cluster_bootstrap_diff(sums, resamples, seed)


@dataclass(frozen=True)
class KMStep:
    t: float
    survival: float
    low: float
    high: float
    at_risk: int


def kaplan_meier(durations: Sequence[float], events: Sequence[bool]) -> list[KMStep]:
    """Kaplan–Meier estimate with a 95% band from Greenwood's variance on the
    log(−log) scale, which keeps the band inside 0 and 1. An event is False
    for a censored time: someone whose next coffee has not happened yet."""
    data = sorted(zip(durations, events))
    n = len(data)
    steps = [KMStep(0.0, 1.0, 1.0, 1.0, n)]
    s, greenwood = 1.0, 0.0
    i = 0
    at_risk = n
    while i < n:
        t = data[i][0]
        deaths = removed = 0
        while i < n and data[i][0] == t:
            deaths += data[i][1]
            removed += 1
            i += 1
        if deaths:
            s *= 1 - deaths / at_risk
            if at_risk > deaths:
                greenwood += deaths / (at_risk * (at_risk - deaths))
            if 0 < s < 1:
                se = math.sqrt(greenwood) / abs(math.log(s))
                lo = s ** math.exp(Z_975 * se)
                hi = s ** math.exp(-Z_975 * se)
            else:
                lo = hi = s
            steps.append(KMStep(t, s, lo, hi, at_risk))
        at_risk -= removed
    return steps


def km_median(steps: Sequence[KMStep]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Median survival with the 95% interval read off the band: the first
    time each of the estimate, the upper band and the lower band reaches
    one half. None where it never does."""
    first = lambda key: next((st.t for st in steps if key(st) <= 0.5), None)
    return first(lambda st: st.survival), first(lambda st: st.low), first(lambda st: st.high)


def lorenz(values: Sequence[float]) -> tuple[list[tuple[float, float]], float]:
    """Lorenz curve points (share of people, share of the total), people in
    ascending order, and the Gini coefficient: 0 when everyone contributes
    equally, approaching 1 when one person contributes everything."""
    xs = sorted(values)
    n, total = len(xs), sum(xs)
    if n == 0 or total == 0:
        return [(0.0, 0.0), (1.0, 1.0)], 0.0
    points, running = [(0.0, 0.0)], 0.0
    for i, x in enumerate(xs, 1):
        running += x
        points.append((i / n, running / total))
    area = sum((x1 - x0) * (y0 + y1) / 2 for (x0, y0), (x1, y1) in zip(points, points[1:]))
    return points, 1 - 2 * area


def share_for_half(values: Sequence[float]) -> float:
    """The smallest share of people who account for half the total."""
    xs = sorted(values, reverse=True)
    total = sum(xs)
    running = 0.0
    for i, x in enumerate(xs, 1):
        running += x
        if running >= total / 2:
            return i / len(xs)
    return 1.0


def cluster_bootstrap(
    clusters: Sequence[Sequence], stat, resamples: int = 500, seed: int = 0
) -> Optional[tuple[float, float]]:
    """95% percentile interval for any statistic of pooled items, resampling
    whole clusters (people, or rounds) with replacement. stat takes the
    pooled items and may return None for a resample it cannot score."""
    if len(clusters) < 2:
        return None
    rng = random.Random(seed)
    k = len(clusters)
    out = []
    for _ in range(resamples):
        pool = [item for _ in range(k) for item in clusters[rng.randrange(k)]]
        v = stat(pool)
        if v is not None and math.isfinite(v):
            out.append(v)
    if len(out) < resamples // 2:
        return None
    out.sort()
    return _percentile(out, 0.025), _percentile(out, 0.975)


def poisson_binomial(rates: Sequence[float]) -> list[float]:
    """Distribution of the number of successes among independent trials with
    their own probabilities: the size of a round if each colleague joined it
    independently, at their own rate."""
    dist = [1.0]
    for p in rates:
        nxt = [0.0] * (len(dist) + 1)
        for k, q in enumerate(dist):
            nxt[k] += q * (1 - p)
            nxt[k + 1] += q * p
        dist = nxt
    return dist


def variance_test(values: Sequence[float], expected_var: float) -> float:
    """Two-sided p-value for a sample variance against an expected one,
    using (n − 1)s²/σ² on n − 1 degrees of freedom."""
    n = len(values)
    if n < 2 or expected_var <= 0:
        return 1.0
    stat = (n - 1) * statistics.variance(values) / expected_var
    upper = chi2_sf(stat, n - 1)
    return min(1.0, 2 * min(upper, 1 - upper))
