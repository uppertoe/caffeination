"""Statistical methods behind the statistics page. Pure functions over plain
values, no database: app.stats_view loads the data and calls these.

Department samples are small (tens of people), so each method is chosen to
stay honest at that size: permutation tests and a bias-corrected Cramér's V
rather than χ² asymptotics, a smoothed model for how unusual a drink is, and
circular statistics for time of day.
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


@dataclass(frozen=True)
class Association:
    n: int
    cramers_v: float
    p_value: float
    # The (row, column) cell furthest above what independence predicts,
    # by Pearson residual, and that residual.
    standout: tuple[Hashable, Hashable]
    standout_residual: float


def _chi2(pairs: Sequence[tuple[Hashable, Hashable]], rows: list, cols: list) -> float:
    n = len(pairs)
    observed = Counter(pairs)
    row_tot = Counter(r for r, _ in pairs)
    col_tot = Counter(c for _, c in pairs)
    chi2 = 0.0
    for r in rows:
        for c in cols:
            expected = row_tot[r] * col_tot[c] / n
            chi2 += (observed[(r, c)] - expected) ** 2 / expected
    return chi2


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


def association(
    pairs: Sequence[tuple[Hashable, Hashable]],
    permutations: int = 499,
    seed: int = 0,
) -> Optional[Association]:
    """Bias-corrected Cramér's V, with a permutation p-value for the null of
    no association.

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
    observed = _chi2(pairs, rows, cols)
    v = _bias_corrected_v(observed, n, len(rows), len(cols))

    # Shuffling keeps both margins, so every expected count is fixed and χ²
    # moves with Σ O²/E alone: compare that over integer cell codes rather
    # than rebuilding the table each time.
    left = [r for r, _ in pairs]
    right = [c for _, c in pairs]
    k = len(cols)
    row_ix = {r: i for i, r in enumerate(rows)}
    col_ix = {c: j for j, c in enumerate(cols)}
    row_codes = [row_ix[r] * k for r in left]
    right_codes = [col_ix[c] for c in right]
    row_tot = Counter(left)
    col_tot = Counter(right)
    inv_expected = [n / (row_tot[r] * col_tot[c]) for r in rows for c in cols]

    def score(col_codes: list[int]) -> float:
        cells = [0] * len(inv_expected)
        for a, b in zip(row_codes, col_codes):
            cells[a + b] += 1
        return sum(o * o * w for o, w in zip(cells, inv_expected))

    threshold = score(right_codes) - 1e-9
    rng = random.Random(seed)
    at_least = 0
    for _ in range(permutations):
        rng.shuffle(right_codes)
        if score(right_codes) >= threshold:
            at_least += 1
    p = (at_least + 1) / (permutations + 1)

    counts = Counter(pairs)
    best, best_resid = (rows[0], cols[0]), -math.inf
    for r in rows:
        for c in cols:
            expected = row_tot[r] * col_tot[c] / n
            resid = (counts[(r, c)] - expected) / math.sqrt(expected)
            if resid > best_resid:
                best, best_resid = (r, c), resid
    return Association(n, v, p, best, best_resid)


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
