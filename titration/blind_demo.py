"""End-to-end blind repeatability and retest-quote demonstration.

All inputs travel through the existing public endpoint products and potency
machinery: each blind determination is a real sample whose frozen inputs are
the two public products bound at first receipt, and its potency is computed by
:class:`~titration.potency.PotencyService`.

Scenarios covered
-----------------
* the headline two-day history (0, 2) / (4, 6) -> within 2, between 7 and the
  two contract quotes (cap 5 -> two days once each; cap 8 -> one day twice);
* repeat transmissions of one determination count once while two distinct
  determinations with numerically identical values count twice;
* a control whose raw between-day estimate is negative: the clamped component
  is zero and the raw mean squares / raw estimate are retained;
* a corrected key release (second day re-reviewed to (2, 4)): a new reveal
  version changes the assessment while the old reveal and its product remain
  re-auditable;
* an incomplete control (one day only) producing explicit gaps instead of
  variance components.

The synthetic curves are arbitrary rational data, not derived from any real
drug standard.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from .curve_service import CurveService
from .demo import BASE_SLOPES, BASE_VOLUMES, curve_from_slopes, shift
from .potency import PotencyService
from .repeatability_service import RepeatabilityService
from .store import Store


def _f(value: int | Fraction) -> Fraction:
    return value if isinstance(value, Fraction) else Fraction(value)


def build_blind_scenario(db_path: str) -> dict:
    store = Store(db_path)
    curves = CurveService(store)
    potency = PotencyService(store)
    repeat = RepeatabilityService(store)
    out: dict[str, Any] = {"determinations": [], "transmissions": []}

    base_v, base_e = curve_from_slopes(
        [Fraction(v) for v in BASE_VOLUMES],
        [Fraction(s) for s in BASE_SLOPES],
    )
    method = curves.create_method("ctrl-dual", 0, 40, 10, 2, 2, 2)
    # Blank curve fixed once; every determination pairs it with its own sample
    # curve, so each determination has its own frozen public product identity.
    blank_curve = curves.create_curve(
        "shared-blank", shift(base_v, Fraction(-1)), base_e
    )
    blank_product = curves.publish_endpoints(blank_curve["id"], method["id"])

    def determination(
        uid: str,
        blind_code: str,
        target_effective_volume: int | Fraction,
    ) -> dict:
        """Create one independent determination with an engineered potency.

        m=100, p=1, c=100, f=1 makes potency == effective_volume exactly, so
        the headline integers (0, 2, 4, 6, ...) are exact rationals traced to
        real published endpoints.
        """
        sample_curve = curves.create_curve(
            f"curve-{uid}",
            shift(base_v, _f(target_effective_volume) - 1),
            base_e,
        )
        sample_product = curves.publish_endpoints(
            sample_curve["id"], method["id"]
        )
        sample = potency.create_sample(
            f"sample-{uid}",
            net_mass=100,
            sample_fraction=1,
            concentration=100,
            factor=1,
            blank_curve_id=blank_curve["id"],
            sample_curve_id=sample_curve["id"],
        )
        result = repeat.transmit(
            determination_uid=uid,
            blind_code=blind_code,
            sample_id=sample["id"],
            method_id=method["id"],
        )
        out["determinations"].append(
            {
                "uid": uid,
                "blind_code": blind_code,
                "target": str(_f(target_effective_volume)),
                "potency": str(result["determination"]["potency"]),
                "blank_product_id": blank_product["id"],
                "sample_product_id": sample_product["id"],
                "created": result["created"],
            }
        )
        return result["determination"]

    # -- main control CTRL-STD: day 1 (0, 2), day 2 (4, 6) ----------------
    determination("det-00", "BL-00", 0)
    determination("det-01", "BL-01", 2)
    determination("det-02", "BL-02", 4)
    determination("det-03", "BL-03", 6)

    # A repeat transmission of det-03 must not create a second determination.
    retrans = repeat.transmit(
        determination_uid="det-03",
        blind_code="BL-03",
        sample_id=store.get_determination_by_uid("det-03")["sample_id"],
        method_id=method["id"],
    )
    out["transmissions"].append(
        {
            "uid": "det-03",
            "seq": retrans["transmission"]["seq"],
            "retransmission": True,
            "created": retrans["created"],
            "determination_count_for_uid": len(
                store.list_transmissions("det-03")
            ),
        }
    )

    # A second independent determination with the *same numeric value* (6):
    # different uid, different frozen products, so it counts twice.  It is
    # deliberately left out of both key releases below, so the balanced
    # assessments exclude it and the report lists an explicit unrevealed gap.
    determination("det-04", "BL-04", 6)
    out["same_value_distinct_determinations"] = {
        "uids": ["det-03", "det-04"],
        "potencies": [
            str(store.get_determination_by_uid("det-03")["potency"]),
            str(store.get_determination_by_uid("det-04")["potency"]),
        ],
        "distinct_frozen_products": (
            store.get_determination_by_uid("det-03")["sample_product_id"]
            != store.get_determination_by_uid("det-04")["sample_product_id"]
        ),
        "independent_count": 2,
        "note": "BL-04 is never keyed to CTRL-STD, so it stays an explicit"
        " UNREVEALED_BLIND_CODE gap rather than breaking the balanced design",
    }

    # -- CTRL-FLAT: day 1 (0, 0), day 2 (2, 2) -> MS_within 0 < MS_between?
    # Actually engineered for MS_within > MS_between so the raw between-day
    # component is negative and gets clamped to zero.
    # day means 1 and 2 -> MS_between = 2*(0.5)^2 = 0.5; groups (0,2),(1,3):
    # SS_within = 4, MS_within = 4/2 = 2 > 0.5; raw between = (0.5-2)/2 < 0.
    determination("det-10", "BL-10", 0)
    determination("det-11", "BL-11", 2)
    determination("det-12", "BL-12", 1)
    determination("det-13", "BL-13", 3)

    # -- CTRL-SHORT: a single day, two determinations -> INCOMPLETE gaps.
    determination("det-20", "BL-20", 5)
    determination("det-21", "BL-21", 7)

    # -- reveal v1 --------------------------------------------------------
    reveal_v1 = repeat.create_reveal(
        "training-release",
        [
            {"blind_code": "BL-00", "control_name": "CTRL-STD",
             "measurement_day": 0},
            {"blind_code": "BL-01", "control_name": "CTRL-STD",
             "measurement_day": 0},
            {"blind_code": "BL-02", "control_name": "CTRL-STD",
             "measurement_day": 1},
            {"blind_code": "BL-03", "control_name": "CTRL-STD",
             "measurement_day": 1},
            {"blind_code": "BL-10", "control_name": "CTRL-FLAT",
             "measurement_day": 0},
            {"blind_code": "BL-11", "control_name": "CTRL-FLAT",
             "measurement_day": 0},
            {"blind_code": "BL-12", "control_name": "CTRL-FLAT",
             "measurement_day": 1},
            {"blind_code": "BL-13", "control_name": "CTRL-FLAT",
             "measurement_day": 1},
            {"blind_code": "BL-20", "control_name": "CTRL-SHORT",
             "measurement_day": 0},
            {"blind_code": "BL-21", "control_name": "CTRL-SHORT",
             "measurement_day": 0},
        ],
    )
    out["reveal_v1"] = {"id": reveal_v1["id"], "version": reveal_v1["version"]}

    std_report = repeat.assess(
        reveal_id=reveal_v1["id"],
        control_name="CTRL-STD",
        method_id=method["id"],
    )
    flat_report = repeat.assess(
        reveal_id=reveal_v1["id"],
        control_name="CTRL-FLAT",
        method_id=method["id"],
    )
    short_report = repeat.assess(
        reveal_id=reveal_v1["id"],
        control_name="CTRL-SHORT",
        method_id=method["id"],
    )
    out["std_v1"] = _report_summary(std_report)
    out["flat_v1"] = _report_summary(flat_report)
    out["short_v1"] = _report_summary(short_report)

    # -- quotations on the headline components (within 2, between 7) ------
    quote_cap5 = repeat.quote(
        report_id=std_report["id"],
        day_setup_cost=3,
        per_determination_cost=1,
        max_days=2,
        max_per_day=2,
        variance_cap=5,
    )
    quote_cap8 = repeat.quote(
        report_id=std_report["id"],
        day_setup_cost=3,
        per_determination_cost=1,
        max_days=2,
        max_per_day=2,
        variance_cap=8,
    )
    quote_infeasible = repeat.quote(
        report_id=std_report["id"],
        day_setup_cost=3,
        per_determination_cost=1,
        max_days=2,
        max_per_day=2,
        variance_cap=1,
    )
    out["quotes_v1"] = {
        "cap_5": _quote_summary(quote_cap5),
        "cap_8": _quote_summary(quote_cap8),
        "cap_1_infeasible": _quote_summary(quote_infeasible),
    }

    # -- old reveal re-audit before the correction ------------------------
    replay_v1 = repeat.replay_report(std_report["id"])

    # -- corrected review: the second day is re-measured as (2, 4) -------
    # The original day-1 determinations (4, 6) stay in the warehouse; the
    # review produces two new independent determinations with their own frozen
    # public products, keyed through a new reveal version.
    determination("det-30", "BL-30", 2)
    determination("det-31", "BL-31", 4)
    reveal_v2 = repeat.create_reveal(
        "training-release",
        [
            {"blind_code": "BL-00", "control_name": "CTRL-STD",
             "measurement_day": 0},
            {"blind_code": "BL-01", "control_name": "CTRL-STD",
             "measurement_day": 0},
            {"blind_code": "BL-30", "control_name": "CTRL-STD",
             "measurement_day": 1},
            {"blind_code": "BL-31", "control_name": "CTRL-STD",
             "measurement_day": 1},
            {"blind_code": "BL-10", "control_name": "CTRL-FLAT",
             "measurement_day": 0},
            {"blind_code": "BL-11", "control_name": "CTRL-FLAT",
             "measurement_day": 0},
            {"blind_code": "BL-12", "control_name": "CTRL-FLAT",
             "measurement_day": 1},
            {"blind_code": "BL-13", "control_name": "CTRL-FLAT",
             "measurement_day": 1},
            {"blind_code": "BL-20", "control_name": "CTRL-SHORT",
             "measurement_day": 0},
            {"blind_code": "BL-21", "control_name": "CTRL-SHORT",
             "measurement_day": 0},
        ],
    )
    out["reveal_v2"] = {"id": reveal_v2["id"], "version": reveal_v2["version"]}
    std_report_v2 = repeat.assess(
        reveal_id=reveal_v2["id"],
        control_name="CTRL-STD",
        method_id=method["id"],
    )
    out["std_v2"] = _report_summary(std_report_v2)
    quote_cap5_v2 = repeat.quote(
        report_id=std_report_v2["id"],
        day_setup_cost=3,
        per_determination_cost=1,
        max_days=2,
        max_per_day=2,
        variance_cap=5,
    )
    out["quotes_v2"] = {"cap_5": _quote_summary(quote_cap5_v2)}

    # -- re-audit: the old reveal product is unchanged and reproducible ---
    replay_v1_after = repeat.replay_report(std_report["id"])
    out["old_reveal_reaudit"] = {
        "report_id": std_report["id"],
        "before_correction": replay_v1["anova"],
        "after_correction": replay_v1_after["anova"],
        "still_reproduced": replay_v1_after["reproduced"],
        "stored_status": store.get_repeatability_report(std_report["id"])[
            "status"
        ],
    }

    store.close()
    out["db"] = str(db_path)
    return _jsonable(out)


def _report_summary(report: dict) -> dict:
    details = report["details"]
    return {
        "report_id": report["id"],
        "status": report["status"],
        "within_day_variance": None
        if report["within_day_variance"] is None
        else str(report["within_day_variance"]),
        "between_day_variance": None
        if report["between_day_variance"] is None
        else str(report["between_day_variance"]),
        "anova": details.get("anova"),
        "day_summaries": details["day_summaries"],
        "accepted_determination_count": details[
            "accepted_determination_count"
        ],
        "gaps": details["gaps"],
    }


def _quote_summary(quote: dict) -> dict:
    return {
        "quote_id": quote["id"],
        "status": quote["status"],
        "selected": quote["details"]["selected"],
        "best_achievable_variance": quote["details"][
            "best_achievable_variance"
        ],
        "best_achievable_plan": quote["details"].get("best_achievable_plan"),
    }


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Fraction):
        return str(obj)
    return obj


def run_blind_demo(db_path: str = "var/blind.sqlite") -> dict:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    result = build_blind_scenario(str(path))
    out = Path("results/blind_demo.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
