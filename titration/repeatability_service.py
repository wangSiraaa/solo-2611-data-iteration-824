"""Repeatability service: blind-coded training data, reveals, assessments.

Workflow
--------
1. Every received transmission of a blind-coded sample is appended to
   ``blind_transmissions``.  A retransmission of the same
   ``determination_uid`` only adds a log row: it never creates a second
   independent determination, while two distinct uids always count twice even
   if their frozen inputs and potencies are numerically identical.
2. A *reveal* is an append-only key release mapping blind codes to the true
   stable control sample and the measurement day.  Re-keying creates a new
   version; old reveals remain available for re-audit.
3. ``assess`` accepts, for one control + method, only balanced records: at
   least two days, at least two valid independent determinations per day and
   equal counts across days.  Everything missing, conflicting or unbalanced is
   reported as an explicit gap instead of being silently dropped.
4. Accepted records feed the exact nested ANOVA in :mod:`titration.repeatability`
   and produce an immutable repeatability report; quotations are separate
   immutable products derived from a report.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from .exact import (
    ConflictError,
    NotFoundError,
    ValidationError,
    fraction_to_json,
)
from .potency import PotencyService
from .repeatability import (
    QuoteParams,
    nested_anova,
    plan_retest,
)
from .store import Store


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Fraction):
        return fraction_to_json(obj)
    return obj


class RepeatabilityService:
    def __init__(self, store: Store):
        self.store = store
        self._potency = None

    def _potency_service(self) -> PotencyService:
        # Lazy attribute so constructing the service stays cheap and tests may
        # share one store across services.
        if self._potency is None:
            self._potency = PotencyService(self.store)
        return self._potency

    # -- blind transmission intake ----------------------------------------
    def transmit(
        self,
        *,
        determination_uid: str,
        blind_code: str,
        sample_id: int,
        method_id: int,
        refresh: bool = False,
    ) -> dict:
        """Record one transmission; create the determination on first sight.

        Retransmissions (same ``determination_uid``) must carry the same blind
        code, method and sample, otherwise the transmission is a conflict and
        is refused.  The frozen inputs are the two public endpoint products
        bound at first receipt; ``refresh=False`` keeps later retransmissions
        pinned to them, mirroring audit-bound potency reports.
        """
        if not determination_uid or not str(determination_uid).strip():
            raise ValidationError("determination_uid must be non-empty")
        if not blind_code or not str(blind_code).strip():
            raise ValidationError("blind_code must be non-empty")
        self.store.get_method(method_id)
        self.store.get_sample(sample_id)

        existing = self.store.get_determination_by_uid(determination_uid)
        if existing is not None:
            if existing["blind_code"] != blind_code:
                raise ConflictError(
                    f"retransmission of {determination_uid!r} carries blind"
                    f" code {blind_code!r}; first transmission used"
                    f" {existing['blind_code']!r}"
                )
            if existing["method_id"] != method_id:
                raise ConflictError(
                    f"retransmission of {determination_uid!r} carries method"
                    f" {method_id}; first transmission used"
                    f" {existing['method_id']}"
                )
            if existing["sample_id"] != sample_id:
                raise ConflictError(
                    f"retransmission of {determination_uid!r} carries sample"
                    f" {sample_id}; first transmission used"
                    f" {existing['sample_id']}"
                )
            transmission = self.store.create_transmission(
                blind_code=blind_code,
                determination_uid=determination_uid,
                retransmission=True,
            )
            return {
                "transmission": transmission,
                "determination": self.store.get_determination(existing["id"]),
                "created": False,
            }

        result = self._potency_service().compute_sample(
            sample_id, method_id, refresh=refresh
        )
        sample = self.store.get_sample(sample_id)
        if result["blank_product_id"] is None or result["sample_product_id"] is None:
            raise ValidationError(
                "determination requires two bound public endpoint products"
            )
        determination = self.store.create_determination(
            {
                "determination_uid": determination_uid,
                "blind_code": blind_code,
                "method_id": method_id,
                "sample_id": sample_id,
                "blank_product_id": result["blank_product_id"],
                "sample_product_id": result["sample_product_id"],
                "potency": Fraction(result["potency"])
                if result["potency"] is not None
                else Fraction(0),
                "eval_status": result["status"],
                "failures": result["failures"],
            }
        )
        transmission = self.store.create_transmission(
            blind_code=blind_code,
            determination_uid=determination_uid,
            retransmission=False,
        )
        return {
            "transmission": transmission,
            "determination": determination,
            "sample_name": sample["name"],
            "created": True,
        }

    # -- reveals -----------------------------------------------------------
    def create_reveal(self, name: str, entries: list[dict]) -> dict:
        """Append a new key-release version.

        ``entries`` maps each blind code to ``{"control_name",
        "measurement_day"}``.  Two known determinations sharing one blind code
        make the release intrinsically ambiguous and are refused.
        """
        reveal = self.store.create_reveal(name, entries)
        conflicts = self._blind_code_attribution_conflicts()
        if conflicts:
            raise ConflictError(
                "reveal has blind codes claimed by multiple determinations:"
                f" {sorted(conflicts)}"
            )
        return reveal

    def _blind_code_attribution_conflicts(self) -> dict[str, list[str]]:
        seen: dict[str, set[str]] = {}
        for det in self._all_determinations():
            seen.setdefault(det["blind_code"], set()).add(
                det["determination_uid"]
            )
        return {code: sorted(uids) for code, uids in seen.items() if len(uids) > 1}

    def _all_determinations(self) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT id FROM blind_determinations ORDER BY id"
        ).fetchall()
        return [self.store.get_determination(r["id"]) for r in rows]

    def get_reveal(self, reveal_id: int) -> dict:
        return self.store.get_reveal(reveal_id)

    def find_reveal(self, name: str, version: int | None = None) -> dict:
        reveal = self.store.find_reveal(name, version)
        if reveal is None:
            version_text = "latest" if version is None else f"v{version}"
            raise NotFoundError(f"reveal {name!r} ({version_text}) not found")
        return reveal

    def list_reveals(self) -> list[dict]:
        return self.store.list_reveals()

    # -- assessment --------------------------------------------------------
    def assess(
        self, *, reveal_id: int, control_name: str, method_id: int
    ) -> dict:
        """Assess repeatability for one control + method under one reveal.

        Balanced acceptance: >= 2 distinct days, each with the same number >= 2
        of valid independent determinations for the requested method.  Gaps
        cover unrevealed codes, attribution conflicts, failed evaluations and
        imbalance; an unmet contract yields an INCOMPLETE immutable report
        instead of variance components.
        """
        reveal = self.store.get_reveal(reveal_id)
        method = self.store.get_method(method_id)
        existing = self.store.find_repeatability_report(
            reveal_id, control_name, method_id
        )
        if existing is not None:
            # Immutable product: the same (reveal, control, method) triple
            # always returns the first assessment, never a recomputed copy.
            return existing
        entries = [
            e
            for e in reveal["entries"]
            if e["control_name"] == control_name
        ]
        day_of = {e["blind_code"]: e["measurement_day"] for e in entries}

        gaps: list[dict] = []
        accepted: list[dict] = []
        conflict_codes = set(self._blind_code_attribution_conflicts())

        for det in self.store.list_determinations_for_method(method_id):
            code = det["blind_code"]
            if code not in day_of:
                # Keyed to another control in this reveal, or not keyed at all.
                # Only the latter is a gap for this control's contract.
                if not any(
                    e["blind_code"] == code for e in reveal["entries"]
                ):
                    gaps.append(
                        {
                            "code": "UNREVEALED_BLIND_CODE",
                            "blind_code": code,
                            "determination_uid": det["determination_uid"],
                            "message": f"blind code {code!r} is not keyed to"
                            " any control in this reveal",
                        }
                    )
                continue
            if code in conflict_codes:
                gaps.append(
                    {
                        "code": "ATTRIBUTION_CONFLICT",
                        "blind_code": code,
                        "determination_uids": self._uids_for_blind_code(code),
                        "message": f"blind code {code!r} is claimed by multiple"
                        " independent determinations",
                    }
                )
                continue
            if det["eval_status"] != "OK":
                gaps.append(
                    {
                        "code": "INVALID_DETERMINATION",
                        "blind_code": code,
                        "determination_uid": det["determination_uid"],
                        "failures": det["failures"],
                        "message": "potency evaluation failed; determination"
                        " cannot enter the variance model",
                    }
                )
                continue
            accepted.append({**det, "measurement_day": day_of[code]})

        # Blind codes this reveal assigns to the requested control but for
        # which no determination exists for the requested method.
        known_codes = {
            d["blind_code"]
            for d in self.store.list_determinations_for_method(method_id)
        }
        for code in sorted(day_of):
            if code not in known_codes:
                gaps.append(
                    {
                        "code": "MISSING_DETERMINATION",
                        "blind_code": code,
                        "measurement_day": day_of[code],
                        "message": f"blind code {code!r} is keyed to control"
                        f" {control_name!r} but has no determination for"
                        f" method {method_id}",
                    }
                )

        days = sorted({d["measurement_day"] for d in accepted})
        per_day: dict[int, list[dict]] = {day: [] for day in days}
        for det in accepted:
            per_day[det["measurement_day"]].append(det)

        day_summaries = [
            {
                "measurement_day": day,
                "count": len(per_day[day]),
                "values": [
                    fraction_to_json(d["potency"]) for d in per_day[day]
                ],
                "day_mean": fraction_to_json(
                    sum(
                        (d["potency"] for d in per_day[day]), Fraction(0)
                    )
                    / len(per_day[day])
                ),
            }
            for day in days
        ]

        balanced = (
            len(days) >= 2
            and len(per_day[days[0]]) >= 2
            and all(len(v) == len(per_day[days[0]]) for v in per_day.values())
        ) if days else False

        if not balanced:
            gaps.extend(self._balance_gaps(days, per_day))
            details = {
                "reveal_id": reveal_id,
                "reveal_name": reveal["name"],
                "reveal_version": reveal["version"],
                "control_name": control_name,
                "method_id": method_id,
                "method_version": method["version"],
                "accepted_determination_count": len(accepted),
                "day_summaries": day_summaries,
                "gaps": gaps,
                "anova": None,
            }
            report = self.store.create_repeatability_report(
                reveal_id=reveal_id,
                control_name=control_name,
                method_id=method_id,
                method_version=method["version"],
                status="INCOMPLETE",
                within_day_variance=None,
                between_day_variance=None,
                details=_jsonable(details),
            )
            return report

        groups = [
            [d["potency"] for d in sorted(per_day[day], key=lambda x: x["id"])]
            for day in days
        ]
        stats = nested_anova(groups)
        details = {
            "reveal_id": reveal_id,
            "reveal_name": reveal["name"],
            "reveal_version": reveal["version"],
            "control_name": control_name,
            "method_id": method_id,
            "method_version": method["version"],
            "accepted_determination_count": len(accepted),
            "determinations": [
                self._determination_detail(d) for d in accepted
            ],
            "day_summaries": day_summaries,
            "gaps": gaps,
            "anova": stats.to_json(),
        }
        report = self.store.create_repeatability_report(
            reveal_id=reveal_id,
            control_name=control_name,
            method_id=method_id,
            method_version=method["version"],
            status="COMPLETE",
            within_day_variance=stats.within_day_variance,
            between_day_variance=stats.between_day_variance,
            details=_jsonable(details),
        )
        return report

    def _balance_gaps(self, days, per_day) -> list[dict]:
        gaps: list[dict] = []
        if len(days) < 2:
            gaps.append(
                {
                    "code": "INSUFFICIENT_DAYS",
                    "day_count": len(days),
                    "message": "the contract requires determinations on at"
                    " least two distinct days",
                }
            )
        if days and min(len(v) for v in per_day.values()) < 2:
            gaps.append(
                {
                    "code": "INSUFFICIENT_PER_DAY",
                    "min_count": min(len(v) for v in per_day.values()),
                    "message": "each accepted day needs at least two valid"
                    " independent determinations",
                }
            )
        if days and len({len(v) for v in per_day.values()}) > 1:
            gaps.append(
                {
                    "code": "UNBALANCED_DAY_COUNTS",
                    "counts": {str(day): len(v) for day, v in per_day.items()},
                    "message": "every accepted day must contribute the same"
                    " number of determinations",
                }
            )
        return gaps

    def _uids_for_blind_code(self, blind_code: str) -> list[str]:
        return [
            d["determination_uid"]
            for d in self._all_determinations()
            if d["blind_code"] == blind_code
        ]

    def _determination_detail(self, det: dict) -> dict:
        transmissions = self.store.list_transmissions(
            det["determination_uid"]
        )
        return {
            "determination_id": det["id"],
            "determination_uid": det["determination_uid"],
            "blind_code": det["blind_code"],
            "measurement_day": det["measurement_day"],
            "potency": fraction_to_json(det["potency"]),
            "sample_id": det["sample_id"],
            "blank_product_id": det["blank_product_id"],
            "sample_product_id": det["sample_product_id"],
            "transmission_count": len(transmissions),
            "retransmission_count": sum(
                1 for t in transmissions if t["retransmission"]
            ),
            "frozen_input": {
                "blank_product_id": det["blank_product_id"],
                "sample_product_id": det["sample_product_id"],
            },
        }

    def get_report(self, report_id: int) -> dict:
        return self.store.get_repeatability_report(report_id)

    def list_reports(self, reveal_id: int | None = None) -> list[dict]:
        return self.store.list_repeatability_reports(reveal_id)

    def replay_report(self, report_id: int) -> dict:
        """Recompute an old report from its stored reveal and frozen values.

        Old reveals are never mutated, so a re-audit must reproduce the exact
        stored ANOVA.  This re-derives every component from the determination
        rows recorded in the report and asserts the match.
        """
        report = self.store.get_repeatability_report(report_id)
        if report["status"] != "COMPLETE":
            return {
                "source_report_id": report_id,
                "status": report["status"],
                "reproduced": True,
                "anova": None,
            }
        details = report["details"]
        groups: dict[int, list[Fraction]] = {}
        for det in details["determinations"]:
            groups.setdefault(det["measurement_day"], []).append(
                Fraction(det["potency"])
            )
        ordered = [groups[day] for day in sorted(groups)]
        stats = nested_anova(ordered)
        stored = details["anova"]
        reproduced = (
            stats.to_json() == stored
            and stats.within_day_variance == report["within_day_variance"]
            and stats.between_day_variance == report["between_day_variance"]
        )
        if not reproduced:
            raise ConflictError(
                f"re-audit of report {report_id} disagrees with the stored"
                " immutable product"
            )
        return {
            "source_report_id": report_id,
            "reveal_id": report["reveal_id"],
            "status": report["status"],
            "reproduced": reproduced,
            "anova": stats.to_json(),
        }

    # -- quotations --------------------------------------------------------
    def quote(
        self,
        *,
        report_id: int,
        day_setup_cost: object,
        per_determination_cost: object,
        max_days: int,
        max_per_day: int,
        variance_cap: object,
    ) -> dict:
        """Derive an immutable minimum-cost retest quote from a report."""
        report = self.store.get_repeatability_report(report_id)
        if report["status"] != "COMPLETE":
            raise ValidationError(
                f"report {report_id} is {report['status']}; no variance"
                " components are available for a quote"
            )
        params = QuoteParams.from_values(
            day_setup_cost,
            per_determination_cost,
            max_days,
            max_per_day,
            variance_cap,
        )
        result = plan_retest(
            report["within_day_variance"],
            report["between_day_variance"],
            params,
        )
        result["report_id"] = report_id
        stored = self.store.create_quote(
            report_id=report_id, status=result["status"], details=_jsonable(result)
        )
        return stored

    def get_quote(self, quote_id: int) -> dict:
        return self.store.get_quote(quote_id)

    def list_quotes(self, report_id: int | None = None) -> list[dict]:
        return self.store.list_quotes(report_id)
