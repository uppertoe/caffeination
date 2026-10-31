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
