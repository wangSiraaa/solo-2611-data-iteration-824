"""Exact-rational titration equivalence and potency service.

The package implements a deliberately finite, discrete signal model:

    L_i = (e_i - e_{i-1}) / (v_i - v_{i-1})
    R_i = (e_{i+1} - e_i) / (v_{i+1} - v_i)
    s_i = abs(R_i - L_i)

Only internal, measured points are candidates.  No continuous-curve
inflection is inferred.
"""

from .curve_service import CurveService, MethodParams, compute_candidates
from .exact import (
    ValidationError,
    NotFoundError,
    ConflictError,
    parse_fraction,
    format_fraction,
)
from .potency import PotencyService
from .repeatability import (
    AnovaStats,
    QuoteParams,
    QuotePlan,
    enumerate_plans,
    mean_variance,
    nested_anova,
    plan_retest,
)
from .repeatability_service import RepeatabilityService
from .solver import solve_selection, brute_force_selection
from .store import Store

__all__ = [
    "CurveService",
    "MethodParams",
    "compute_candidates",
    "PotencyService",
    "RepeatabilityService",
    "Store",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
    "parse_fraction",
    "format_fraction",
    "solve_selection",
    "brute_force_selection",
    "AnovaStats",
    "QuoteParams",
    "QuotePlan",
    "nested_anova",
    "plan_retest",
    "enumerate_plans",
    "mean_variance",
]
