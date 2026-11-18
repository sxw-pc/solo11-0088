"""Exact finite-population statistics for quality sampling evidence.

Sampling is without replacement, so the tail of the observed qualified count is
hypergeometric.  Candidate population counts whose upper tail is at most alpha
are rejected; the smallest surviving count is the one-sided lower bound.  All
arithmetic uses ``Fraction``, so there is no floating-point drift and no
external statistics service.
"""

from __future__ import annotations

from fractions import Fraction
from math import comb
from typing import Any

DEFAULT_ALPHA = Fraction(1, 10)
ONE_SIDED_CONFIDENCE = Fraction(9, 10)


def hypergeometric_tail(
    population: int, successes: int, sample_size: int, observed: int
) -> Fraction:
    """P(X >= observed) for sampling ``sample_size`` from a finite population."""
    if not (0 <= successes <= population and 0 <= sample_size <= population):
        raise ValueError("hypergeometric parameters are out of range")
    denominator = comb(population, sample_size)
    if denominator == 0:
        return Fraction(0, 1)
    upper = min(sample_size, successes)
    numerator = sum(
        comb(successes, drawn) * comb(population - successes, sample_size - drawn)
        for drawn in range(max(0, observed), upper + 1)
        if 0 <= sample_size - drawn <= population - successes
    )
    return Fraction(numerator, denominator)


def population_lower_bound(
    population: int, sample_size: int, observed: int, *, alpha: Fraction = DEFAULT_ALPHA
) -> int:
    """Smallest population count not rejected at one-sided ``alpha``."""
    if not (0 <= observed <= sample_size <= population):
        raise ValueError("sample count is out of range")
    if sample_size == 0:
        return 0
    for candidate in range(observed, population + 1):
        if hypergeometric_tail(population, candidate, sample_size, observed) > alpha:
            return candidate
    return population


def fraction_dict(value: Fraction) -> dict[str, Any]:
    return {
        "numerator": value.numerator,
        "denominator": value.denominator,
        "text": f"{value.numerator}/{value.denominator}",
    }


def population_bounds(
    population: int, sample_size: int, observed: int, *, alpha: Fraction = DEFAULT_ALPHA
) -> list[dict[str, Any]]:
    """Per-candidate tail probabilities used by the certificate."""
    lower = population_lower_bound(population, sample_size, observed, alpha=alpha)
    rows = []
    for candidate in range(population + 1):
        tail = hypergeometric_tail(population, candidate, sample_size, observed)
        rows.append({
            "population_successes": candidate,
            "tail_probability": fraction_dict(tail),
            "rejected_at_alpha": tail <= alpha,
            "lower_bound": candidate == lower,
        })
    return rows
