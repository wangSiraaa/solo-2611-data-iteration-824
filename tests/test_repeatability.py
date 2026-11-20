"""Tests for blind repeatability assessment and retest quotations.

Covers the headline worked example, retransmission de-duplication vs. distinct
determinations with identical values, the negative between-day estimate
clamp, the balanced-design gap contract, immutable reveal re-audit and the
cost/variance quote optimisation.
"""

import pytest
from fractions import Fraction

from titration import (
    ConflictError,
    CurveService,
    PotencyService,
    QuoteParams,
    RepeatabilityService,
    Store,
    ValidationError,
    nested_anova,
    plan_retest,
)
from titration.blind_demo import run_blind_demo
from titration.demo import BASE_SLOPES, BASE_VOLUMES, curve_from_slopes, shift


# -- pure nested ANOVA -----------------------------------------------------
def test_headline_two_day_anova_components():
    stats = nested_anova(
        [[Fraction(0), Fraction(2)], [Fraction(4), Fraction(6)]]
    )
    assert stats.day_count == 2 and stats.per_day_count == 2
    assert stats.df_within == 2 and stats.df_between == 1
    assert stats.ss_within == 4
    assert stats.ss_between == 16
    assert stats.ms_within == 2
    assert stats.ms_between == 16
    assert stats.within_day_variance == 2
    assert stats.between_day_variance == 7
    assert stats.between_day_variance_raw == 7


def test_corrected_review_two_day_anova():
    # (0, 2) and (2, 4): SS_within = 4, SS_between = 4 -> 2 and 1.
    stats = nested_anova(
        [[Fraction(0), Fraction(2)], [Fraction(2), Fraction(4)]]
    )
    assert stats.within_day_variance == 2
    assert stats.between_day_variance == 1


def test_negative_between_day_component_clamped_keeps_raw_mean_squares():
    # Day means 1 and 2, grand mean 3/2: SS_between = 2*((1/2)^2+(1/2)^2) = 1,
    # df_between = 1 -> MS_between = 1.
    # SS_within = 4 over df 2 -> MS_within = 2 > MS_between.
    stats = nested_anova(
        [[Fraction(0), Fraction(2)], [Fraction(1), Fraction(3)]]
    )
    assert stats.ms_within == 2
    assert stats.ms_between == 1
    assert stats.between_day_variance_raw == Fraction(-1, 2)
    assert stats.between_day_variance == 0
    # The within component is the raw mean square itself.
    assert stats.within_day_variance == stats.ms_within


def test_anova_rejects_unbalanced_or_short_designs():
    with pytest.raises(ValidationError):
        nested_anova([[Fraction(1), Fraction(2)]])
    with pytest.raises(ValidationError):
        nested_anova([[Fraction(1)], [Fraction(2)]])
    with pytest.raises(ValidationError):
        nested_anova(
            [[Fraction(1), Fraction(2)], [Fraction(3), Fraction(4), Fraction(5)]]
        )


def test_anova_is_exact_rational():
    # Day means 1 and 3, grand mean 2: SS_between = 2*(1+1) = 4,
    # MS_between = 4; MS_within = 1/2; between component = (4 - 1/2)/2 = 7/4.
    stats = nested_anova(
        [[Fraction(1, 2), Fraction(3, 2)], [Fraction(5, 2), Fraction(7, 2)]]
    )
    assert isinstance(stats.ms_within, Fraction)
    assert stats.ms_within == Fraction(1, 2)
    assert stats.between_day_variance == Fraction(7, 4)


# -- pure quote optimisation -----------------------------------------------
@pytest.fixture
def headline_params_factory():
    def make(cap):
        return QuoteParams.from_values(3, 1, 2, 2, cap)

    return make


def test_quote_cap5_prefers_two_days_once_each(headline_params_factory):
    result = plan_retest(Fraction(2), Fraction(7), headline_params_factory(5))
    assert result["status"] == "FEASIBLE"
    plan = result["selected"]
    assert plan["days"] == 2
    assert plan["per_day"] == 1
    assert plan["total_determinations"] == 2
    assert plan["mean_variance"] == "9/2"
    assert plan["total_cost"] == "8"


def test_quote_cap8_prefers_one_day_twice(headline_params_factory):
    result = plan_retest(Fraction(2), Fraction(7), headline_params_factory(8))
    assert result["status"] == "FEASIBLE"
    plan = result["selected"]
    assert plan["days"] == 1
    assert plan["per_day"] == 2
    assert plan["total_cost"] == "5"
    assert plan["mean_variance"] == "8"


def test_quote_corrected_history_one_day_once_suffices(headline_params_factory):
    # Corrected components: within 2, between 1.  One day, once -> var 3 <= 5.
    result = plan_retest(Fraction(2), Fraction(1), headline_params_factory(5))
    plan = result["selected"]
    assert plan["days"] == 1
    assert plan["per_day"] == 1
    assert plan["mean_variance"] == "3"
    assert plan["total_cost"] == "4"


def test_quote_infeasible_reports_lowest_achievable_variance(
    headline_params_factory,
):
    result = plan_retest(Fraction(2), Fraction(7), headline_params_factory(1))
    assert result["status"] == "INFEASIBLE"
    assert result["selected"] is None
    # Best resource use is two days, twice each: 7/2 + 2/4 = 4.
    assert result["best_achievable_variance"] == "4"
    assert result["best_achievable_plan"]["days"] == 2
    assert result["best_achievable_plan"]["per_day"] == 2


def test_quote_tie_break_fewer_days_then_fewer_determinations():
    # sigma_w=2, sigma_b=1, cap 2: (1,1) is infeasible (variance 3), while
    # (1,2) (var 2) and (2,1) (var 3/2) are both feasible and both cost 3.
    # The tie must resolve to the plan with fewer days.
    params = QuoteParams.from_values(1, 1, 2, 2, 2)
    result = plan_retest(Fraction(2), Fraction(1), params)
    assert result["selected"]["total_cost"] == "3"
    assert result["selected"]["days"] == 1  # fewer days wins the tie
    assert result["selected"]["per_day"] == 2


def test_quote_second_tie_break_fewer_total_determinations():
    # Zero costs make every plan equally priced; among them (1,1) has the
    # fewest days and, at one day, the fewest total determinations.
    params = QuoteParams.from_values(0, 0, 2, 2, 100)
    result = plan_retest(Fraction(1), Fraction(1), params)
    assert result["selected"]["days"] == 1
    assert result["selected"]["per_day"] == 1
    assert result["selected"]["total_determinations"] == 1


def test_quote_tie_on_days_resolves_by_total_determinations():
    # Cap 1 with sigma_b=sigma_w=1 makes every one-day plan infeasible and
    # (2,1) exactly feasible.  With zero per-determination cost (2,1) and
    # (2,2) tie on price, so fewer total determinations wins.
    params = QuoteParams.from_values(0, 0, 2, 2, 1)
    result = plan_retest(Fraction(1), Fraction(1), params)
    assert result["selected"]["days"] == 2
    assert result["selected"]["per_day"] == 1
    assert result["selected"]["total_determinations"] == 2


def test_quote_enumerates_every_equal_count_plan():
    params = QuoteParams.from_values(3, 1, 2, 2, 5)
    result = plan_retest(Fraction(2), Fraction(7), params)
    assert len(result["considered_plans"]) == 4
    assert {
        (p["days"], p["per_day"]) for p in result["considered_plans"]
    } == {(1, 1), (1, 2), (2, 1), (2, 2)}


def test_quote_params_reject_bad_resources():
    with pytest.raises(ValidationError):
        QuoteParams.from_values(3, 1, 0, 2, 5)
    with pytest.raises(ValidationError):
        QuoteParams.from_values(3, 1, 2, -1, 5)
    with pytest.raises(ValidationError):
        QuoteParams.from_values(-3, 1, 2, 2, 5)
    with pytest.raises(ValidationError):
        QuoteParams.from_values(3, 1, True, 2, 5)


# -- store-backed service --------------------------------------------------
@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "blind.sqlite")
    yield s
    s.close()


@pytest.fixture
def curves(store):
    return CurveService(store)


@pytest.fixture
def potency(store):
    return PotencyService(store)


@pytest.fixture
def repeat(store):
    return RepeatabilityService(store)


def _make_method(curves):
    return curves.create_method("m", 0, 40, 10, 2, 2, 2)


def _blank(curves, method):
    base_v, base_e = curve_from_slopes(
        [Fraction(v) for v in BASE_VOLUMES],
        [Fraction(s) for s in BASE_SLOPES],
    )
    blank = curves.create_curve("blank", shift(base_v, -1), base_e)
    curves.publish_endpoints(blank["id"], method["id"])
    return blank, base_v, base_e


def _determination(curves, potency, repeat, method, blank, base_v, base_e,
                   uid, code, target):
    sample_curve = curves.create_curve(
        f"c-{uid}", shift(base_v, Fraction(target) - 1), base_e
    )
    curves.publish_endpoints(sample_curve["id"], method["id"])
    sample = potency.create_sample(
        f"s-{uid}", 100, 1, 100, 1,
        blank_curve_id=blank["id"], sample_curve_id=sample_curve["id"],
    )
    return repeat.transmit(
        determination_uid=uid,
        blind_code=code,
        sample_id=sample["id"],
        method_id=method["id"],
    )


def _reveal(repeat, name, entries):
    return repeat.create_reveal(name, entries)


def test_retransmission_counts_once_but_distinct_same_value_counts_twice(
    curves, potency, repeat
):
    method = _make_method(curves)
    blank, base_v, base_e = _blank(curves, method)
    first = _determination(
        curves, potency, repeat, method, blank, base_v, base_e,
        "u1", "B1", 6,
    )
    second_same_value = _determination(
        curves, potency, repeat, method, blank, base_v, base_e,
        "u2", "B2", 6,
    )
    assert first["created"] is True
    assert second_same_value["created"] is True
    # Two independent determinations, numerically identical, distinct frozen
    # products.
    assert first["determination"]["potency"] == Fraction(6)
    assert second_same_value["determination"]["potency"] == Fraction(6)
    assert (
        first["determination"]["sample_product_id"]
        != second_same_value["determination"]["sample_product_id"]
    )

    # Re-send u1: only a transmission row is appended, no new determination.
    retrans = repeat.transmit(
        determination_uid="u1",
        blind_code="B1",
        sample_id=first["determination"]["sample_id"],
        method_id=method["id"],
    )
    assert retrans["created"] is False
    assert retrans["transmission"]["retransmission"] is True
    assert retrans["transmission"]["seq"] == 2
    # Still exactly two determinations in the warehouse.
    assert len(repeat._all_determinations()) == 2


def test_retransmission_with_changed_identity_is_conflict(
    curves, potency, repeat
):
    method = _make_method(curves)
    blank, base_v, base_e = _blank(curves, method)
    first = _determination(
        curves, potency, repeat, method, blank, base_v, base_e,
        "u1", "B1", 1,
    )
    other = _determination(
        curves, potency, repeat, method, blank, base_v, base_e,
        "u2", "B2", 2,
    )
    with pytest.raises(ConflictError):
        repeat.transmit(
            determination_uid="u1",
            blind_code="B9",
            sample_id=first["determination"]["sample_id"],
            method_id=method["id"],
        )
    with pytest.raises(ConflictError):
        repeat.transmit(
            determination_uid="u1",
            blind_code="B1",
            sample_id=other["determination"]["sample_id"],
            method_id=method["id"],
        )


def test_assessment_headline_and_immutable_product(
    curves, potency, repeat
):
    method = _make_method(curves)
    blank, base_v, base_e = _blank(curves, method)
    for uid, code, target in [
        ("d0", "B0", 0), ("d1", "B1", 2),
        ("d2", "B2", 4), ("d3", "B3", 6),
    ]:
        _determination(
            curves, potency, repeat, method, blank, base_v, base_e,
            uid, code, target,
        )
    reveal = _reveal(
        repeat, "rel",
        [
            {"blind_code": "B0", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B1", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B2", "control_name": "CTRL", "measurement_day": 1},
            {"blind_code": "B3", "control_name": "CTRL", "measurement_day": 1},
        ],
    )
    report = repeat.assess(
        reveal_id=reveal["id"], control_name="CTRL", method_id=method["id"]
    )
    assert report["status"] == "COMPLETE"
    assert report["within_day_variance"] == 2
    assert report["between_day_variance"] == 7
    details = report["details"]
    assert details["anova"]["ms_within"] == "2"
    assert details["anova"]["ms_between"] == "16"
    assert [d["measurement_day"] for d in details["determinations"]] == [
        0, 0, 1, 1
    ]
    # Frozen inputs are retained per determination.
    for d in details["determinations"]:
        assert d["frozen_input"]["blank_product_id"] is not None
        assert d["frozen_input"]["sample_product_id"] is not None

    # Re-assessing the same (reveal, control, method) returns the same
    # immutable product rather than duplicating it.
    again = repeat.assess(
        reveal_id=reveal["id"], control_name="CTRL", method_id=method["id"]
    )
    assert again["id"] == report["id"]


def test_incomplete_design_reports_gaps_without_components(
    curves, potency, repeat
):
    method = _make_method(curves)
    blank, base_v, base_e = _blank(curves, method)
    _determination(
        curves, potency, repeat, method, blank, base_v, base_e,
        "d0", "B0", 5,
    )
    _determination(
        curves, potency, repeat, method, blank, base_v, base_e,
        "d1", "B1", 7,
    )
    reveal = _reveal(
        repeat, "rel",
        [
            {"blind_code": "B0", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B1", "control_name": "CTRL", "measurement_day": 0},
        ],
    )
    report = repeat.assess(
        reveal_id=reveal["id"], control_name="CTRL", method_id=method["id"]
    )
    assert report["status"] == "INCOMPLETE"
    assert report["within_day_variance"] is None
    assert report["between_day_variance"] is None
    gap_codes = {g["code"] for g in report["details"]["gaps"]}
    assert "INSUFFICIENT_DAYS" in gap_codes
    with pytest.raises(ValidationError):
        repeat.quote(
            report_id=report["id"],
            day_setup_cost=3, per_determination_cost=1,
            max_days=2, max_per_day=2, variance_cap=5,
        )


def test_unbalanced_design_lists_gap(curves, potency, repeat):
    method = _make_method(curves)
    blank, base_v, base_e = _blank(curves, method)
    for uid, code, target, day in [
        ("d0", "B0", 0, 0), ("d1", "B1", 2, 0),
        ("d2", "B2", 4, 1),
    ]:
        _determination(
            curves, potency, repeat, method, blank, base_v, base_e,
            uid, code, target,
        )
    reveal = _reveal(
        repeat, "rel",
        [
            {"blind_code": "B0", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B1", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B2", "control_name": "CTRL", "measurement_day": 1},
        ],
    )
    report = repeat.assess(
        reveal_id=reveal["id"], control_name="CTRL", method_id=method["id"]
    )
    assert report["status"] == "INCOMPLETE"
    gap_codes = {g["code"] for g in report["details"]["gaps"]}
    assert "UNBALANCED_DAY_COUNTS" in gap_codes


def test_missing_determination_gap_for_keyed_but_absent_code(
    curves, potency, repeat
):
    method = _make_method(curves)
    blank, base_v, base_e = _blank(curves, method)
    _determination(
        curves, potency, repeat, method, blank, base_v, base_e,
        "d0", "B0", 0,
    )
    _determination(
        curves, potency, repeat, method, blank, base_v, base_e,
        "d1", "B1", 2,
    )
    reveal = _reveal(
        repeat, "rel",
        [
            {"blind_code": "B0", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B1", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B2", "control_name": "CTRL", "measurement_day": 1},
            {"blind_code": "B3", "control_name": "CTRL", "measurement_day": 1},
        ],
    )
    report = repeat.assess(
        reveal_id=reveal["id"], control_name="CTRL", method_id=method["id"]
    )
    missing = [g for g in report["details"]["gaps"]
               if g["code"] == "MISSING_DETERMINATION"]
    assert {g["blind_code"] for g in missing} == {"B2", "B3"}


def test_attribution_conflict_refuses_reveal(curves, potency, repeat):
    method = _make_method(curves)
    blank, base_v, base_e = _blank(curves, method)
    _determination(
        curves, potency, repeat, method, blank, base_v, base_e,
        "u1", "DUP", 1,
    )
    _determination(
        curves, potency, repeat, method, blank, base_v, base_e,
        "u2", "DUP", 2,
    )
    with pytest.raises(ConflictError):
        _reveal(
            repeat, "rel",
            [{"blind_code": "DUP", "control_name": "CTRL",
              "measurement_day": 0}],
        )


def test_corrected_reveal_changes_components_but_old_report_reaudits(
    curves, potency, repeat
):
    method = _make_method(curves)
    blank, base_v, base_e = _blank(curves, method)
    for uid, code, target in [
        ("d0", "B0", 0), ("d1", "B1", 2),
        ("d2", "B2", 4), ("d3", "B3", 6),
    ]:
        _determination(
            curves, potency, repeat, method, blank, base_v, base_e,
            uid, code, target,
        )
    v1 = _reveal(
        repeat, "rel",
        [
            {"blind_code": "B0", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B1", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B2", "control_name": "CTRL", "measurement_day": 1},
            {"blind_code": "B3", "control_name": "CTRL", "measurement_day": 1},
        ],
    )
    old_report = repeat.assess(
        reveal_id=v1["id"], control_name="CTRL", method_id=method["id"]
    )
    assert old_report["between_day_variance"] == 7

    # New reviewed determinations (2, 4) and a new reveal version.
    for uid, code, target in [("d4", "B4", 2), ("d5", "B5", 4)]:
        _determination(
            curves, potency, repeat, method, blank, base_v, base_e,
            uid, code, target,
        )
    v2 = _reveal(
        repeat, "rel",
        [
            {"blind_code": "B0", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B1", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B4", "control_name": "CTRL", "measurement_day": 1},
            {"blind_code": "B5", "control_name": "CTRL", "measurement_day": 1},
        ],
    )
    assert v2["version"] == 2
    new_report = repeat.assess(
        reveal_id=v2["id"], control_name="CTRL", method_id=method["id"]
    )
    assert new_report["id"] != old_report["id"]
    assert new_report["within_day_variance"] == 2
    assert new_report["between_day_variance"] == 1

    # The old immutable product still replays exactly from its old reveal.
    replay = repeat.replay_report(old_report["id"])
    assert replay["reproduced"] is True
    assert replay["anova"]["between_day_variance"] == "7"
    stored = repeat.get_report(old_report["id"])
    assert stored["between_day_variance"] == 7


def test_quote_persistence_and_resource_limit_plan(
    curves, potency, repeat
):
    method = _make_method(curves)
    blank, base_v, base_e = _blank(curves, method)
    for uid, code, target in [
        ("d0", "B0", 0), ("d1", "B1", 2),
        ("d2", "B2", 4), ("d3", "B3", 6),
    ]:
        _determination(
            curves, potency, repeat, method, blank, base_v, base_e,
            uid, code, target,
        )
    reveal = _reveal(
        repeat, "rel",
        [
            {"blind_code": "B0", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B1", "control_name": "CTRL", "measurement_day": 0},
            {"blind_code": "B2", "control_name": "CTRL", "measurement_day": 1},
            {"blind_code": "B3", "control_name": "CTRL", "measurement_day": 1},
        ],
    )
    report = repeat.assess(
        reveal_id=reveal["id"], control_name="CTRL", method_id=method["id"]
    )
    quote = repeat.quote(
        report_id=report["id"],
        day_setup_cost=3, per_determination_cost=1,
        max_days=2, max_per_day=2, variance_cap=5,
    )
    assert quote["status"] == "FEASIBLE"
    assert quote["details"]["selected"]["days"] == 2
    # Stored and retrievable as an immutable product.
    assert repeat.get_quote(quote["id"])["id"] == quote["id"]
    assert repeat.list_quotes(report["id"])[0]["id"] == quote["id"]


# -- end-to-end demo fixture -----------------------------------------------
def test_blind_demo_end_to_end(tmp_path):
    result = run_blind_demo(str(tmp_path / "blind.sqlite"))
    # Headline history and components.
    assert result["std_v1"]["within_day_variance"] == "2"
    assert result["std_v1"]["between_day_variance"] == "7"
    # Quotes exactly match the contract examples.
    assert result["quotes_v1"]["cap_5"]["selected"]["days"] == 2
    assert result["quotes_v1"]["cap_5"]["selected"]["per_day"] == 1
    assert result["quotes_v1"]["cap_5"]["selected"]["total_cost"] == "8"
    assert result["quotes_v1"]["cap_8"]["selected"]["days"] == 1
    assert result["quotes_v1"]["cap_8"]["selected"]["per_day"] == 2
    assert result["quotes_v1"]["cap_8"]["selected"]["total_cost"] == "5"
    # Infeasible quote reports the lowest achievable variance.
    assert result["quotes_v1"]["cap_1_infeasible"]["status"] == "INFEASIBLE"
    assert (
        result["quotes_v1"]["cap_1_infeasible"]["best_achievable_variance"]
        == "4"
    )
    # Retransmission de-duplication and same-value distinct determinations.
    assert result["transmissions"][0]["retransmission"] is True
    assert result["transmissions"][0]["created"] is False
    assert (
        result["same_value_distinct_determinations"]["independent_count"] == 2
    )
    assert (
        result["same_value_distinct_determinations"][
            "distinct_frozen_products"
        ]
        is True
    )
    # MS_within > MS_between: clamp to zero, raw values retained.
    assert result["flat_v1"]["within_day_variance"] == "2"
    assert result["flat_v1"]["between_day_variance"] == "0"
    assert result["flat_v1"]["anova"]["between_day_variance_raw"] == "-1/2"
    assert result["flat_v1"]["anova"]["ms_within"] == "2"
    assert result["flat_v1"]["anova"]["ms_between"] == "1"
    # Corrected review: (0,2)/(2,4) -> within 2, between 1; cap 5 now allows
    # one day with a single determination.
    assert result["std_v2"]["within_day_variance"] == "2"
    assert result["std_v2"]["between_day_variance"] == "1"
    selected = result["quotes_v2"]["cap_5"]["selected"]
    assert selected["days"] == 1 and selected["per_day"] == 1
    assert selected["total_cost"] == "4"
    # Old reveal product remains reproducible after the correction.
    assert result["old_reveal_reaudit"]["still_reproduced"] is True
    assert result["old_reveal_reaudit"]["before_correction"] == (
        result["old_reveal_reaudit"]["after_correction"]
    )
    # The incomplete control reports gaps rather than components.
    assert result["short_v1"]["status"] == "INCOMPLETE"
    assert result["short_v1"]["anova"] is None
