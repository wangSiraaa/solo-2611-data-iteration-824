"""Repeatability variance components and minimum-cost retest quotations.

The variance model is the classic nested (one-way random-effects) ANOVA on
*days*: every accepted day contributes the same number ``n`` of independent
determinations.  All arithmetic is exact (``fractions.Fraction``); no binary
floating point is used.

Given day groups ``g_1, ..., g_d`` of equal size ``n``::

    grand mean      x.. = sum(all values) / (d * n)
    day means       x_i. = mean of group i
    SS_within       = sum over days, values (x - x_i.)^2
    SS_between      = n * sum over days (x_i. - x..)^2
    df_within       = d * (n - 1)
    df_between      = d - 1
    MS_within       = SS_within / df_within
    MS_between      = SS_between / df_between

Variance components::

    sigma2_within    = MS_within
    sigma2_between   = max((MS_between - MS_within) / n, 0)

The raw (possibly negative) estimate and both raw mean squares are always
reported alongside the clamped component so a negative variance estimate is
never hidden.

Retest contract: day errors are independent across days and shared by the
``n`` determinations run that day; every within-day determination adds an
independent error.  For an equal-count plan of ``d`` days with ``n``
determinations each, the mean variance is exactly::

    Var(mean) = sigma2_between / d + sigma2_within / (d * n)

The cheapest plan (day setup cost per day, measurement cost per
determination) meeting a variance cap is selected; ties prefer fewer days,
then fewer total determinations.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Sequence

from .exact import (
    ValidationError,
    fraction_to_json,
    parse_fraction,
)


# -- nested one-way ANOVA -------------------------------------------------
@dataclass(frozen=True)
class AnovaStats:
    day_count: int
    per_day_count: int
    grand_mean: Fraction
    ss_within: Fraction
    ss_between: Fraction
    df_within: int
    df_between: int
    ms_within: Fraction
    ms_between: Fraction
    within_day_variance: Fraction
    between_day_variance_raw: Fraction
    between_day_variance: Fraction
    day_means: tuple[Fraction, ...]

    def to_json(self) -> dict:
        return {
            "day_count": self.day_count,
            "per_day_count": self.per_day_count,
            "grand_mean": fraction_to_json(self.grand_mean),
            "day_means": [fraction_to_json(m) for m in self.day_means],
            "ss_within": fraction_to_json(self.ss_within),
            "ss_between": fraction_to_json(self.ss_between),
            "df_within": self.df_within,
            "df_between": self.df_between,
            "ms_within": fraction_to_json(self.ms_within),
            "ms_between": fraction_to_json(self.ms_between),
            "within_day_variance": fraction_to_json(self.within_day_variance),
            "between_day_variance_raw": fraction_to_json(
                self.between_day_variance_raw
            ),
            "between_day_variance": fraction_to_json(self.between_day_variance),
        }


def nested_anova(groups: Sequence[Sequence[Fraction]]) -> AnovaStats:
    """Exact nested one-way ANOVA for equally sized day groups.

    At least two days, at least two values per day and equal group sizes are
    required; otherwise the degrees of freedom would be undefined.
    """
    if len(groups) < 2:
        raise ValidationError("nested ANOVA needs at least two day groups")
    n = len(groups[0])
    if n < 2:
        raise ValidationError("each day group needs at least two determinations")
    if any(len(g) != n for g in groups):
        raise ValidationError("all day groups must have the same size")

    d = len(groups)
    day_means: list[Fraction] = []
    ss_within = Fraction(0)
    total = Fraction(0)
    for group in groups:
        gsum = sum(group, Fraction(0))
        gmean = gsum / n
        day_means.append(gmean)
        total += gsum
        for value in group:
            diff = value - gmean
            ss_within += diff * diff
    grand_mean = total / (d * n)
    ss_between = n * sum(((m - grand_mean) ** 2 for m in day_means), Fraction(0))

    df_within = d * (n - 1)
    df_between = d - 1
    ms_within = ss_within / df_within
    ms_between = ss_between / df_between

    within_day_variance = ms_within
    between_raw = (ms_between - ms_within) / n
    between_day_variance = between_raw if between_raw > 0 else Fraction(0)

    return AnovaStats(
        day_count=d,
        per_day_count=n,
        grand_mean=grand_mean,
        ss_within=ss_within,
        ss_between=ss_between,
        df_within=df_within,
        df_between=df_between,
        ms_within=ms_within,
        ms_between=ms_between,
        within_day_variance=within_day_variance,
        between_day_variance_raw=between_raw,
        between_day_variance=between_day_variance,
        day_means=tuple(day_means),
    )


# -- retest quotation ------------------------------------------------------
@dataclass(frozen=True)
class QuoteParams:
    """Quotation-side contract parameters.

    All money fields are exact rationals; the unit is whatever the vendor
    quotes in, no conversion is applied.
    """

    day_setup_cost: Fraction
    per_determination_cost: Fraction
    max_days: int
    max_per_day: int
    variance_cap: Fraction

    @classmethod
    def from_values(
        cls,
        day_setup_cost: object,
        per_determination_cost: object,
        max_days: object,
        max_per_day: object,
        variance_cap: object,
    ) -> "QuoteParams":
        setup = parse_fraction(day_setup_cost, field="day_setup_cost")
        per = parse_fraction(
            per_determination_cost, field="per_determination_cost"
        )
        cap = parse_fraction(variance_cap, field="variance_cap")
        for label, value in (
            ("day_setup_cost", setup),
            ("per_determination_cost", per),
            ("variance_cap", cap),
        ):
            if value < 0:
                raise ValidationError(f"{label} must be non-negative")
        for label, value in (("max_days", max_days), ("max_per_day", max_per_day)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError(f"{label} must be an integer")
            if value <= 0:
                raise ValidationError(f"{label} must be a positive integer")
        return cls(setup, per, max_days, max_per_day, cap)

    def to_json(self) -> dict:
        return {
            "day_setup_cost": fraction_to_json(self.day_setup_cost),
            "per_determination_cost": fraction_to_json(
                self.per_determination_cost
            ),
            "max_days": self.max_days,
            "max_per_day": self.max_per_day,
            "variance_cap": fraction_to_json(self.variance_cap),
        }


@dataclass(frozen=True)
class QuotePlan:
    days: int
    per_day: int
    total_determinations: int
    mean_variance: Fraction
    total_cost: Fraction
    feasible: bool

    def to_json(self) -> dict:
        return {
            "days": self.days,
            "per_day": self.per_day,
            "total_determinations": self.total_determinations,
            "mean_variance": fraction_to_json(self.mean_variance),
            "total_cost": fraction_to_json(self.total_cost),
            "feasible": self.feasible,
        }


def mean_variance(
    within_day_variance: Fraction,
    between_day_variance: Fraction,
    days: int,
    per_day: int,
) -> Fraction:
    """Var(mean) under the contract: sigma_b^2/d + sigma_w^2/(d*n)."""
    if days <= 0 or per_day <= 0:
        raise ValidationError("days and per_day must be positive integers")
    return (
        between_day_variance / days
        + within_day_variance / (days * per_day)
    )


def enumerate_plans(
    within_day_variance: Fraction,
    between_day_variance: Fraction,
    params: QuoteParams,
) -> list[QuotePlan]:
    """Every equal-count plan within the vendor's resource limits."""
    plans: list[QuotePlan] = []
    for days in range(1, params.max_days + 1):
        for per_day in range(1, params.max_per_day + 1):
            variance = mean_variance(
                within_day_variance, between_day_variance, days, per_day
            )
            total = days * params.day_setup_cost + (
                days * per_day
            ) * params.per_determination_cost
            plans.append(
                QuotePlan(
                    days=days,
                    per_day=per_day,
                    total_determinations=days * per_day,
                    mean_variance=variance,
                    total_cost=total,
                    feasible=variance <= params.variance_cap,
                )
            )
    return plans


def plan_retest(
    within_day_variance: Fraction,
    between_day_variance: Fraction,
    params: QuoteParams,
) -> dict:
    """Choose the cheapest feasible equal-count plan.

    Tie-break on equal total cost: fewer days first, then fewer total
    determinations.  If no plan meets the cap, the lowest achievable variance
    within the available resources is reported instead of a fake quote.
    """
    within = parse_fraction(
        within_day_variance, field="within_day_variance"
    ) if not isinstance(within_day_variance, Fraction) else within_day_variance
    between = parse_fraction(
        between_day_variance, field="between_day_variance"
    ) if not isinstance(between_day_variance, Fraction) else between_day_variance
    if within < 0 or between < 0:
        raise ValidationError("variance components must be non-negative")

    plans = enumerate_plans(within, between, params)
    feasible = [p for p in plans if p.feasible]
    if feasible:
        best = min(
            feasible,
            key=lambda p: (p.total_cost, p.days, p.total_determinations),
        )
        return {
            "status": "FEASIBLE",
            "selected": best.to_json(),
            "best_achievable_variance": fraction_to_json(best.mean_variance),
            "considered_plans": [p.to_json() for p in plans],
            "params": params.to_json(),
        }

    best_variance_plan = min(plans, key=lambda p: (p.mean_variance, p.total_cost))
    return {
        "status": "INFEASIBLE",
        "selected": None,
        "best_achievable_variance": fraction_to_json(
            best_variance_plan.mean_variance
        ),
        "best_achievable_plan": best_variance_plan.to_json(),
        "considered_plans": [p.to_json() for p in plans],
        "params": params.to_json(),
    }
