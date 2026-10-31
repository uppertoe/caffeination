"""Statistical methods behind the statistics page. Pure functions over plain
values, no database: app.stats_view loads the data and calls these.

Department samples are small (tens of people), so each method is chosen to
stay honest at that size: permutation tests and a bias-corrected Cramér's V
beside the χ² asymptotics, a smoothed model for how unusual a drink is,
simulation for uncertainty, and circular statistics for time of day. Each
returns its working as well as its answer, because the page shows both.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Association between two categorical options.
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


@dataclass(frozen=True)
class Association:
    n: int
    cramers_v: float
    p_value: float
    # The (row, column) cell furthest above what independence predicts,
    # by Pearson residual, and that residual.
    standout: tuple[Hashable, Hashable]
    standout_residual: float


def association(
    pairs: Sequence[tuple[Hashable, Hashable]],
    permutations: int = 499,
    seed: int = 0,
) -> Optional[Association]:
    """The summary of chi_square that the findings need: bias-corrected
    Cramér's V, the permutation p-value, and the standout cell."""
    test = chi_square(pairs, permutations, seed)
    if test is None:
        return None
    cells = [(i, j) for i in range(len(test.rows)) for j in range(len(test.cols))]
    i, j = max(cells, key=lambda ij: test.residual(*ij))
    return Association(
        test.n, test.cramers_v, test.p_permutation,
        (test.rows[i], test.cols[j]), test.residual(i, j),
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


# ---------------------------------------------------------------------------
# Uncertainty in a caffeine estimate, propagated by simulation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    estimate: float
    low: float  # 2.5th percentile
    high: float  # 97.5th percentile
    samples: tuple[float, ...]


def _percentile(sorted_xs: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks."""
    if not sorted_xs:
        return math.nan
    pos = (len(sorted_xs) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def _interval(samples: list[float], estimate: float) -> Interval:
    ordered = sorted(samples)
    return Interval(estimate, _percentile(ordered, 0.025), _percentile(ordered, 0.975), tuple(samples))


def simulate_caffeine(
    drinks: Sequence[tuple[float, float]],
    shot_mean: float,
    shot_sd: float,
    simulations: int = 2000,
    seed: int = 0,
) -> dict[str, Interval]:
    """Total caffeine of a set of drinks, each given as (espresso shots,
    other mg), with the caffeine in a shot drawn from a gamma distribution
    of the given mean and SD. Two scenarios, in milligrams:

    "separate": each drink comes from a different café, so each draws its
    own shot strength and the errors partly cancel across drinks;
    "one_cafe": the whole round comes from one café, so one draw scales
    every shot and nothing cancels. A real round is the second case.
    """
    shape = (shot_mean / shot_sd) ** 2
    scale = shot_sd ** 2 / shot_mean
    shots = sum(s for s, _ in drinks)
    other = sum(o for _, o in drinks)
    estimate = shots * shot_mean + other
    rng = random.Random(seed)
    separate, one_cafe = [], []
    for _ in range(simulations):
        separate.append(other + sum(s * rng.gammavariate(shape, scale) for s, _ in drinks if s))
        one_cafe.append(other + shots * rng.gammavariate(shape, scale))
    return {"separate": _interval(separate, estimate), "one_cafe": _interval(one_cafe, estimate)}


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


def even_spread_p(counts: Sequence[int], simulations: int = 1999, seed: int = 0) -> float:
    """p-value for counts as uneven as these if every category (weekday)
    were equally likely: a χ² goodness-of-fit test, with the reference
    distribution simulated so it stays exact at a handful of rounds."""
    n, k = sum(counts), len(counts)
    if n == 0 or k < 2:
        return 1.0
    expected = n / k
    stat = lambda cs: sum((c - expected) ** 2 for c in cs)
    observed = stat(counts) - 1e-9
    rng = random.Random(seed)
    at_least = 0
    for _ in range(simulations):
        sim = [0] * k
        for _ in range(n):
            sim[rng.randrange(k)] += 1
        if stat(sim) >= observed:
            at_least += 1
    return (at_least + 1) / (simulations + 1)


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


def _normal_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2))


def power_two_proportions(p1: float, p2: float, n1: int, n2: int) -> float:
    """Power of a two-sided 5% test to detect proportions p1 and p2 in groups
    of n1 and n2, by the usual normal approximation."""
    if n1 <= 0 or n2 <= 0:
        return 0.0
    pbar = (p1 * n1 + p2 * n2) / (n1 + n2)
    null_se = math.sqrt(pbar * (1 - pbar) * (1 / n1 + 1 / n2))
    alt_se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    if alt_se == 0:
        return 1.0 if p1 != p2 else 0.0
    return _normal_cdf((abs(p1 - p2) - Z_975 * null_se) / alt_se)


def n2_for_power(p1: float, p2: float, n1: int, target: float = 0.8, cap: int = 2000) -> Optional[int]:
    """The smallest second group that gives the target power against a first
    group of fixed size n1, or None if no group up to cap is enough."""
    for n2 in range(2, cap + 1):
        if power_two_proportions(p1, p2, n1, n2) >= target:
            return n2
    return None


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


def tost(a: Sequence[float], b: Sequence[float], margin: float) -> Optional[tuple[Welch, float]]:
    """Two one-sided tests for equivalence within ±margin: the larger of the
    two one-sided p-values. Below 0.05 means the groups are shown to be the
    same to within the margin, which a non-significant t-test never shows."""
    w = welch(a, b)
    if w is None or w.se == 0:
        return None
    p_lower = t_sf((w.difference + margin) / w.se, w.df)
    p_upper = t_sf((margin - w.difference) / w.se, w.df)
    return w, max(p_lower, p_upper)


def cluster_bootstrap_diff(
    items: Sequence[tuple[Hashable, int, float]], resamples: int = 2000, seed: int = 0
) -> Optional[tuple[float, float, float]]:
    """Difference in mean value between group 1 and group 0, with a 95%
    percentile interval from resampling whole clusters (people) with
    replacement, so a person's repeat drinks count as one person's evidence.
    Items are (cluster, group, value). Returns (difference, low, high)."""
    clusters: dict[Hashable, list[tuple[int, float]]] = {}
    for c, g, v in items:
        clusters.setdefault(c, []).append((g, v))
    keys = list(clusters)

    def diff(pool: list[tuple[int, float]]) -> Optional[float]:
        g1 = [v for g, v in pool if g == 1]
        g0 = [v for g, v in pool if g == 0]
        if not g1 or not g0:
            return None
        return statistics.fmean(g1) - statistics.fmean(g0)

    est = diff([gv for k in keys for gv in clusters[k]])
    if est is None or len(keys) < 2:
        return None
    rng = random.Random(seed)
    out = []
    for _ in range(resamples):
        pool = [gv for _ in keys for gv in clusters[keys[rng.randrange(len(keys))]]]
        d = diff(pool)
        if d is not None:
            out.append(d)
    out.sort()
    return est, _percentile(out, 0.025), _percentile(out, 0.975)


@dataclass(frozen=True)
class OddsRatio:
    estimate: float
    low: float
    high: float


def _woolf(a: float, b: float, c: float, d: float) -> OddsRatio:
    """Odds ratio ad/bc with Woolf's interval; half is added to every cell
    when one is empty (Haldane–Anscombe)."""
    if 0 in (a, b, c, d):
        a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
    or_ = a * d / (b * c)
    se = math.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    return OddsRatio(or_, or_ * math.exp(-Z_975 * se), or_ * math.exp(Z_975 * se))


def mantel_haenszel(strata: Sequence[tuple[int, int, int, int]]) -> Optional[tuple[OddsRatio, OddsRatio]]:
    """Crude and Mantel–Haenszel odds ratios from 2×2 strata (a, b, c, d):
    a = exposed with the outcome, b = exposed without, c = unexposed with,
    d = unexposed without. The crude ratio pools the strata; the MH ratio
    compares like with like inside each stratum, removing confounding by
    whatever the strata are. MH interval by Robins, Breslow and Greenland."""
    strata = [s for s in strata if sum(s) > 0]
    a, b, c, d = (sum(s[i] for s in strata) for i in range(4))
    if not strata or min(a + b, c + d, a + c, b + d) == 0:
        return None
    crude = _woolf(a, b, c, d)
    num = den = 0.0
    p_r = ps_qr = q_s = 0.0
    for sa, sb, sc, sd in strata:
        n = sa + sb + sc + sd
        r, s = sa * sd / n, sb * sc / n
        p, q = (sa + sd) / n, (sb + sc) / n
        num, den = num + r, den + s
        p_r += p * r
        ps_qr += p * s + q * r
        q_s += q * s
    if num == 0 or den == 0:
        return crude, _woolf(a, b, c, d)
    or_mh = num / den
    var = p_r / (2 * num * num) + ps_qr / (2 * num * den) + q_s / (2 * den * den)
    se = math.sqrt(var)
    return crude, OddsRatio(or_mh, or_mh * math.exp(-Z_975 * se), or_mh * math.exp(Z_975 * se))


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


def gini_interval(values: Sequence[float], resamples: int = 1000, seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(values)
    out = sorted(lorenz([values[rng.randrange(n)] for _ in range(n)])[1] for _ in range(resamples))
    return _percentile(out, 0.025), _percentile(out, 0.975)


def _ranks(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(x: Sequence[float], y: Sequence[float]) -> float:
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0


def spearman(x: Sequence[float], y: Sequence[float], permutations: int = 999, seed: int = 0) -> tuple[float, float]:
    """Spearman's rank correlation with a two-sided permutation p-value."""
    if len(x) < 3:
        return 0.0, 1.0
    rx, ry = _ranks(x), _ranks(y)
    rho = _pearson(rx, ry)
    rng = random.Random(seed)
    shuffled = list(ry)
    at_least = 0
    for _ in range(permutations):
        rng.shuffle(shuffled)
        if abs(_pearson(rx, shuffled)) >= abs(rho) - 1e-12:
            at_least += 1
    return rho, (at_least + 1) / (permutations + 1)


def dispersion(counts: Sequence[int]) -> tuple[float, float]:
    """Variance-to-mean ratio of counts and the p-value against a Poisson
    process, for which the ratio is 1: (n − 1) × ratio follows χ² on n − 1
    degrees of freedom. Above 1, counts clump more than chance predicts."""
    n = len(counts)
    if n < 2:
        return 1.0, 1.0
    mean = statistics.fmean(counts)
    ratio = statistics.variance(counts) / mean if mean else 1.0
    return ratio, chi2_sf((n - 1) * ratio, n - 1)


def poisson_pmf(k: int, lam: float) -> float:
    return math.exp(k * math.log(lam) - lam - math.lgamma(k + 1)) if lam > 0 else float(k == 0)


def cusum(values: Sequence[float], target: float, sd: float, k: float = 0.5, h: float = 4.0) -> list[tuple[float, float, bool]]:
    """Tabular CUSUM: for each value, the upper and lower cumulative sums of
    standardised departures from target beyond an allowance of k SDs, and
    whether either has crossed the decision limit h. The same chart tracks
    a trainee's procedural failures for a learning curve."""
    hi = lo = 0.0
    out = []
    for v in values:
        z = (v - target) / sd if sd else 0.0
        hi = max(0.0, hi + z - k)
        lo = max(0.0, lo - z - k)
        out.append((hi, lo, hi > h or lo > h))
    return out


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher's exact p for the 2×2 table [[a, b], [c, d]]: the sum
    of every table with the same margins that is no more likely."""
    r1, c1, n = a + b, a + c, a + b + c + d
    lo, hi = max(0, r1 + c1 - n), min(r1, c1)
    log_p = lambda x: (
        math.lgamma(r1 + 1) + math.lgamma(n - r1 + 1) + math.lgamma(c1 + 1) + math.lgamma(n - c1 + 1)
        - math.lgamma(n + 1) - math.lgamma(x + 1) - math.lgamma(r1 - x + 1)
        - math.lgamma(c1 - x + 1) - math.lgamma(n - r1 - c1 + x + 1)
    )
    observed = log_p(a)
    return min(1.0, sum(math.exp(log_p(x)) for x in range(lo, hi + 1) if log_p(x) <= observed + 1e-9))


def fragility_index(a: int, b: int, c: int, d: int) -> Optional[int]:
    """For a significant 2×2 result, how many members of the group with fewer
    events would have to switch from no event to event (in the direction
    that weakens the result) before p reaches 0.05. None if p ≥ 0.05."""
    if fisher_exact(a, b, c, d) >= 0.05:
        return None
    flips = 0
    # Move the group with the lower event rate towards the other.
    while fisher_exact(a, b, c, d) < 0.05:
        if a / max(a + b, 1) < c / max(c + d, 1):
            if b == 0:
                return None
            a, b = a + 1, b - 1
        else:
            if d == 0:
                return None
            c, d = c + 1, d - 1
        flips += 1
    return flips


@dataclass(frozen=True)
class Roc:
    points: list[tuple[float, float]]  # (1 − specificity, sensitivity)
    auc: float
    low: float
    high: float


def roc(scores: Sequence[float], positive: Sequence[bool]) -> Optional[Roc]:
    """ROC curve for a score predicting a binary label, with the AUC (the
    chance a random positive scores above a random negative, ties counting
    half) and the Hanley–McNeil 95% interval."""
    pos = [s for s, y in zip(scores, positive) if y]
    neg = [s for s, y in zip(scores, positive) if not y]
    if not pos or not neg:
        return None
    thresholds = sorted(set(scores), reverse=True)
    points = [(0.0, 0.0)]
    for t in thresholds:
        points.append((sum(s >= t for s in neg) / len(neg), sum(s >= t for s in pos) / len(pos)))
    auc = sum((s > n) + 0.5 * (s == n) for s in pos for n in neg) / (len(pos) * len(neg))
    q1, q2 = auc / (2 - auc), 2 * auc * auc / (1 + auc)
    var = (auc * (1 - auc) + (len(pos) - 1) * (q1 - auc ** 2) + (len(neg) - 1) * (q2 - auc ** 2)) / (len(pos) * len(neg))
    se = math.sqrt(max(var, 0.0))
    return Roc(points, auc, max(0.0, auc - Z_975 * se), min(1.0, auc + Z_975 * se))


def normal_posterior(estimate: float, se: float, prior_sd: float) -> tuple[float, float]:
    """Posterior mean and SD for a difference, from a normal likelihood
    (estimate, se) and a sceptical normal prior centred on no difference."""
    w_prior, w_data = 1 / prior_sd ** 2, 1 / se ** 2
    sd = math.sqrt(1 / (w_prior + w_data))
    return estimate * w_data * sd * sd, sd


# ---------------------------------------------------------------------------
# Agreement between two methods of measuring the same thing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlandAltman:
    n: int
    bias: float  # mean of (b − a)
    sd: float
    lower: float  # bias − 1.96 SD: the 95% limits of agreement
    upper: float
    slope: float  # of the difference on the mean: proportional bias
    slope_p: float
    r: float  # Pearson correlation of a and b, for contrast


def bland_altman(a: Sequence[float], b: Sequence[float]) -> Optional[BlandAltman]:
    """Bland and Altman's analysis of b against a: the mean difference, the
    limits within which 95% of differences fall, and whether the difference
    grows with the size of the measurement."""
    n = len(a)
    if n < 3:
        return None
    diffs = [y - x for x, y in zip(a, b)]
    means = [(x + y) / 2 for x, y in zip(a, b)]
    bias, sd = statistics.fmean(diffs), statistics.stdev(diffs)
    mx = statistics.fmean(means)
    sxx = sum((m - mx) ** 2 for m in means)
    slope = sum((m - mx) * (d - bias) for m, d in zip(means, diffs)) / sxx if sxx else 0.0
    resid = [d - bias - slope * (m - mx) for m, d in zip(means, diffs)]
    s2 = sum(e * e for e in resid) / (n - 2) if n > 2 else 0.0
    se = math.sqrt(s2 / sxx) if sxx and s2 else 0.0
    slope_p = 2 * t_sf(abs(slope) / se, n - 2) if se else (0.0 if slope else 1.0)
    return BlandAltman(n, bias, sd, bias - Z_975 * sd, bias + Z_975 * sd, slope, min(1.0, slope_p), _pearson(a, b))


def cohen_kappa(a: Sequence[int], b: Sequence[int], categories: int, weighted: bool = False) -> float:
    """Cohen's kappa for two ratings of the same items on categories 0..k−1:
    agreement beyond what chance would produce, from 0 (chance) to 1
    (perfect). Weighted, a near miss counts as partial agreement, in
    proportion to how near (linear weights)."""
    n = len(a)
    if n == 0:
        return math.nan
    w = lambda i, j: 1 - abs(i - j) / (categories - 1) if weighted else float(i == j)
    pa = Counter(a)
    pb = Counter(b)
    observed = sum(w(i, j) for i, j in zip(a, b)) / n
    expected = sum(w(i, j) * pa[i] * pb[j] for i in range(categories) for j in range(categories)) / (n * n)
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0


def kappa_interval(a: Sequence[int], b: Sequence[int], categories: int, weighted: bool,
                   resamples: int = 1000, seed: int = 0) -> tuple[float, float]:
    """95% bootstrap interval for kappa, resampling the rated items."""
    rng = random.Random(seed)
    n = len(a)
    out = []
    for _ in range(resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        k = cohen_kappa([a[i] for i in idx], [b[i] for i in idx], categories, weighted)
        if not math.isnan(k):
            out.append(k)
    out.sort()
    return _percentile(out, 0.025), _percentile(out, 0.975)
