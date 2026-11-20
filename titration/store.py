"""SQLite persistence.

All rational numbers are stored as canonical text (``n`` or ``n/d``).
Endpoint products are immutable, append-only public records.  Analytical
reports are append-only snapshots: recomputing creates a new report and never
mutates an earlier one.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Sequence

from .exact import (
    ConflictError,
    NotFoundError,
    ValidationError,
    format_fraction,
    parse_fraction,
)
from .solver import Candidate, SelectionResult

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS curves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    volumes_json TEXT NOT NULL,
    potentials_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS methods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    low_volume TEXT NOT NULL,
    high_volume TEXT NOT NULL,
    threshold TEXT NOT NULL,
    k INTEGER NOT NULL,
    min_gap TEXT NOT NULL,
    equivalence_index INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS curve_versions (
    curve_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    volumes_json TEXT NOT NULL,
    potentials_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (curve_id, version)
);

CREATE TABLE IF NOT EXISTS method_versions (
    method_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    low_volume TEXT NOT NULL,
    high_volume TEXT NOT NULL,
    threshold TEXT NOT NULL,
    k INTEGER NOT NULL,
    min_gap TEXT NOT NULL,
    equivalence_index INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (method_id, version)
);

CREATE TABLE IF NOT EXISTS endpoint_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    curve_id INTEGER,
    curve_name TEXT NOT NULL,
    curve_version INTEGER,
    method_id INTEGER,
    method_name TEXT NOT NULL,
    method_version INTEGER,
    k INTEGER NOT NULL,
    selected_volumes_json TEXT NOT NULL,
    selected_indices_json TEXT NOT NULL,
    objective TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    external_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_product_internal
    ON endpoint_products(curve_id, curve_version, method_id, method_version)
    WHERE source = 'internal';

CREATE UNIQUE INDEX IF NOT EXISTS ux_product_external
    ON endpoint_products(external_ref) WHERE source = 'external';

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    net_mass TEXT NOT NULL,
    sample_fraction TEXT NOT NULL,
    concentration TEXT NOT NULL,
    factor TEXT NOT NULL,
    blank_curve_id INTEGER,
    sample_curve_id INTEGER,
    blank_product_id INTEGER,
    sample_product_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    method_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_samples (
    batch_id INTEGER NOT NULL,
    sample_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (batch_id, sample_id),
    FOREIGN KEY (batch_id) REFERENCES batches(id),
    FOREIGN KEY (sample_id) REFERENCES samples(id)
);

CREATE TABLE IF NOT EXISTS potency_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    method_id INTEGER NOT NULL,
    method_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    average TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Blind-coded repeatability training data.  A determination is one
-- independent measurement; repeat transmissions of the same determination_uid
-- append to blind_transmissions but never create a second determination.
CREATE TABLE IF NOT EXISTS blind_transmissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blind_code TEXT NOT NULL,
    determination_uid TEXT NOT NULL,
    seq INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    retransmission INTEGER NOT NULL,
    UNIQUE (determination_uid, seq)
);

CREATE TABLE IF NOT EXISTS blind_determinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    determination_uid TEXT NOT NULL UNIQUE,
    blind_code TEXT NOT NULL,
    method_id INTEGER NOT NULL,
    sample_id INTEGER NOT NULL,
    blank_product_id INTEGER NOT NULL,
    sample_product_id INTEGER NOT NULL,
    potency TEXT NOT NULL,
    eval_status TEXT NOT NULL,
    failures_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Append-only key releases.  Re-keying a blind code produces a new reveal;
-- old releases stay queryable for historical re-audit.
CREATE TABLE IF NOT EXISTS blind_reveals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS blind_reveal_entries (
    reveal_id INTEGER NOT NULL,
    blind_code TEXT NOT NULL,
    control_name TEXT NOT NULL,
    measurement_day INTEGER NOT NULL,
    PRIMARY KEY (reveal_id, blind_code),
    FOREIGN KEY (reveal_id) REFERENCES blind_reveals(id)
);

-- Immutable repeatability assessment products, one per
-- (reveal, control, method) triple.
CREATE TABLE IF NOT EXISTS repeatability_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reveal_id INTEGER NOT NULL,
    control_name TEXT NOT NULL,
    method_id INTEGER NOT NULL,
    method_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    within_day_variance TEXT,
    between_day_variance TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (reveal_id, control_name, method_id)
);

-- Immutable retest quotations derived from a repeatability report.
CREATE TABLE IF NOT EXISTS retest_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (report_id) REFERENCES repeatability_reports(id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ftext(value: Fraction) -> str:
    return format_fraction(value)


def _flist(values: Iterable[Fraction]) -> list[str]:
    return [_ftext(v) for v in values]


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive, idempotent migrations for databases created earlier."""
        cols = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(endpoint_products)"
            ).fetchall()
        }
        if "input_json" not in cols:
            self.conn.execute(
                "ALTER TABLE endpoint_products ADD COLUMN"
                " input_json TEXT NOT NULL DEFAULT '{}'"
            )

    def close(self) -> None:
        self.conn.close()

    # -- curves -----------------------------------------------------------
    def create_curve(
        self, name: str, volumes: Sequence[Fraction], potentials: Sequence[Fraction]
    ) -> dict:
        if not name or not str(name).strip():
            raise ValidationError("curve name must be non-empty")
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO curves(name, version, volumes_json, potentials_json,"
            " created_at, updated_at) VALUES (?, 1, ?, ?, ?, ?)",
            (
                name,
                json.dumps(_flist(volumes)),
                json.dumps(_flist(potentials)),
                now,
                now,
            ),
        )
        curve_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO curve_versions(curve_id, version, volumes_json,"
            " potentials_json, created_at) VALUES (?, 1, ?, ?, ?)",
            (
                curve_id,
                json.dumps(_flist(volumes)),
                json.dumps(_flist(potentials)),
                now,
            ),
        )
        self.conn.commit()
        return self.get_curve(curve_id)

    def update_curve(
        self,
        curve_id: int,
        volumes: Sequence[Fraction],
        potentials: Sequence[Fraction],
        *,
        expected_version: int,
    ) -> dict:
        row = self.conn.execute(
            "SELECT version FROM curves WHERE id = ?", (curve_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"curve {curve_id} not found")
        if row["version"] != expected_version:
            raise ConflictError(
                f"curve {curve_id} version is {row['version']}, expected "
                f"{expected_version}"
            )
        cur = self.conn.execute(
            "UPDATE curves SET version = version + 1, volumes_json = ?,"
            " potentials_json = ?, updated_at = ? WHERE id = ? AND version = ?",
            (
                json.dumps(_flist(volumes)),
                json.dumps(_flist(potentials)),
                _now(),
                curve_id,
                expected_version,
            ),
        )
        if cur.rowcount != 1:
            raise ConflictError(f"curve {curve_id} was modified concurrently")
        self.conn.execute(
            "INSERT INTO curve_versions(curve_id, version, volumes_json,"
            " potentials_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                curve_id,
                expected_version + 1,
                json.dumps(_flist(volumes)),
                json.dumps(_flist(potentials)),
                _now(),
            ),
        )
        self.conn.commit()
        return self.get_curve(curve_id)

    def get_curve(self, curve_id: int, version: int | None = None) -> dict:
        row = self.conn.execute(
            "SELECT * FROM curves WHERE id = ?", (curve_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"curve {curve_id} not found")
        if version is not None and row["version"] != version:
            historical = self.conn.execute(
                "SELECT volumes_json, potentials_json FROM curve_versions"
                " WHERE curve_id = ? AND version = ?",
                (curve_id, version),
            ).fetchone()
            if historical is None:
                raise NotFoundError(
                    f"curve {curve_id} version {version} not found"
                )
            volumes = [parse_fraction(v) for v in json.loads(historical["volumes_json"])]
            potentials = [
                parse_fraction(v) for v in json.loads(historical["potentials_json"])
            ]
            return {
                "id": row["id"],
                "name": row["name"],
                "version": version,
                "current_version": row["version"],
                "_volumes": volumes,
                "_potentials": potentials,
                "volumes": _flist(volumes),
                "potentials": _flist(potentials),
            }
        volumes = [parse_fraction(v) for v in json.loads(row["volumes_json"])]
        potentials = [parse_fraction(v) for v in json.loads(row["potentials_json"])]
        return {
            "id": row["id"],
            "name": row["name"],
            "version": row["version"],
            "_volumes": volumes,
            "_potentials": potentials,
            "volumes": _flist(volumes),
            "potentials": _flist(potentials),
        }

    def list_curves(self) -> list[dict]:
        rows = self.conn.execute("SELECT id FROM curves ORDER BY id").fetchall()
        return [self.get_curve(r["id"]) for r in rows]

    # -- methods ----------------------------------------------------------
    def create_method(
        self,
        name: str,
        params: Any,
        equivalence_index: int | None = None,
    ) -> dict:
        if not name or not str(name).strip():
            raise ValidationError("method name must be non-empty")
        if equivalence_index is None:
            equivalence_index = getattr(params, "equivalence_index", None)
        if equivalence_index is not None:
            self._validate_j(equivalence_index, params.k)
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO methods(name, version, low_volume, high_volume,"
            " threshold, k, min_gap, equivalence_index, created_at, updated_at)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                _ftext(params.low_volume),
                _ftext(params.high_volume),
                _ftext(params.threshold),
                params.k,
                _ftext(params.min_gap),
                equivalence_index,
                now,
                now,
            ),
        )
        method_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO method_versions(method_id, version, low_volume,"
            " high_volume, threshold, k, min_gap, equivalence_index, created_at)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (
                method_id,
                _ftext(params.low_volume),
                _ftext(params.high_volume),
                _ftext(params.threshold),
                params.k,
                _ftext(params.min_gap),
                equivalence_index,
                now,
            ),
        )
        self.conn.commit()
        return self.get_method(method_id)

    def update_method(
        self,
        method_id: int,
        params: Any,
        equivalence_index: int | None = None,
        *,
        expected_version: int | None = None,
    ) -> dict:
        row = self.conn.execute(
            "SELECT version, equivalence_index FROM methods WHERE id = ?",
            (method_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"method {method_id} not found")
        if expected_version is not None and row["version"] != expected_version:
            raise ConflictError(
                f"method {method_id} version is {row['version']}, expected "
                f"{expected_version}"
            )
        j = row["equivalence_index"] if equivalence_index is None else equivalence_index
        if j is not None:
            self._validate_j(j, params.k)
        self.conn.execute(
            "UPDATE methods SET version = version + 1, low_volume = ?,"
            " high_volume = ?, threshold = ?, k = ?, min_gap = ?,"
            " equivalence_index = ?, updated_at = ? WHERE id = ?",
            (
                _ftext(params.low_volume),
                _ftext(params.high_volume),
                _ftext(params.threshold),
                params.k,
                _ftext(params.min_gap),
                j,
                _now(),
                method_id,
            ),
        )
        new_version = row["version"] + 1
        self.conn.execute(
            "INSERT INTO method_versions(method_id, version, low_volume,"
            " high_volume, threshold, k, min_gap, equivalence_index, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                method_id,
                new_version,
                _ftext(params.low_volume),
                _ftext(params.high_volume),
                _ftext(params.threshold),
                params.k,
                _ftext(params.min_gap),
                j,
                _now(),
            ),
        )
        self.conn.commit()
        return self.get_method(method_id)

    @staticmethod
    def _validate_j(j: Any, k: int) -> None:
        if isinstance(j, bool) or not isinstance(j, int):
            raise ValidationError("equivalence_index must be an integer")
        if j < 1 or j > k:
            raise ValidationError(
                f"equivalence_index j must satisfy 1 <= j <= K (got {j}, K={k})"
            )

    def get_method(self, method_id: int, version: int | None = None) -> dict:
        row = self.conn.execute(
            "SELECT * FROM methods WHERE id = ?", (method_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"method {method_id} not found")
        source = row
        current_version = row["version"]
        if version is not None and version != row["version"]:
            historical = self.conn.execute(
                "SELECT * FROM method_versions WHERE method_id = ? AND version = ?",
                (method_id, version),
            ).fetchone()
            if historical is None:
                raise NotFoundError(
                    f"method {method_id} version {version} not found"
                )
            source = historical
        return {
            "id": row["id"],
            "name": row["name"],
            "version": source["version"],
            "current_version": current_version,
            "low_volume": parse_fraction(source["low_volume"]),
            "high_volume": parse_fraction(source["high_volume"]),
            "threshold": parse_fraction(source["threshold"]),
            "k": source["k"],
            "min_gap": parse_fraction(source["min_gap"]),
            "equivalence_index": source["equivalence_index"],
        }

    # -- endpoint products ------------------------------------------------
    def find_endpoint_product(
        self,
        curve_id: int,
        curve_version: int,
        method_id: int,
        method_version: int,
    ) -> dict | None:
        row = self.conn.execute(
            "SELECT id FROM endpoint_products WHERE curve_id = ? AND"
            " curve_version = ? AND method_id = ? AND method_version = ?"
            " AND source = 'internal'",
            (curve_id, curve_version, method_id, method_version),
        ).fetchone()
        return None if row is None else self.get_endpoint_product(row["id"])

    def create_endpoint_product(
        self,
        *,
        curve_id: int | None,
        curve_name: str,
        curve_version: int | None,
        method_id: int | None,
        method_name: str,
        method_version: int | None,
        k: int,
        selection: SelectionResult,
        explanation: dict,
        candidates: Sequence[Candidate],
        input_snapshot: dict | None = None,
    ) -> dict:
        if curve_id is not None:
            existing = self.find_endpoint_product(
                curve_id, curve_version, method_id, method_version
            )
            if existing is not None:
                if existing["selected_volumes"] != [
                    _ftext(v) for v in selection.volumes
                ]:
                    raise ConflictError(
                        "an endpoint product already exists for this exact"
                        " curve/method version pair but has different endpoints"
                    )
                return existing
        candidates_json = [
            {
                "index": c.index,
                "volume": _ftext(c.volume),
                "strength": _ftext(c.strength),
                "left_slope": _ftext(c.left_slope),
                "right_slope": _ftext(c.right_slope),
            }
            for c in candidates
        ]
        cur = self.conn.execute(
            "INSERT INTO endpoint_products(curve_id, curve_name, curve_version,"
            " method_id, method_name, method_version, k, selected_volumes_json,"
            " selected_indices_json, objective, candidates_json,"
            " explanation_json, input_json, source, external_ref, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'internal', NULL, ?)",
            (
                curve_id,
                curve_name,
                curve_version,
                method_id,
                method_name,
                method_version,
                k,
                json.dumps(_flist(selection.volumes)),
                json.dumps(selection.indices),
                _ftext(selection.objective),
                json.dumps(candidates_json),
                json.dumps(explanation),
                json.dumps(input_snapshot or {}),
                _now(),
            ),
        )
        self.conn.commit()
        return self.get_endpoint_product(cur.lastrowid)

    def import_endpoint_product(self, payload: dict) -> dict:
        """Consume an external endpoint product in the documented JSON shape.

        Required keys: ``curve_name``, ``method_name``, ``k``,
        ``selected_volumes`` (rational strings).  Optional: ``curve_id``,
        ``curve_version``, ``method_id``, ``method_version``, ``objective``,
        ``candidates``, ``selected_indices``, ``explanation``,
        ``external_ref``.
        """
        if not isinstance(payload, dict):
            raise ValidationError("external product must be a JSON object")
        required = ("curve_name", "method_name", "k", "selected_volumes")
        missing = [key for key in required if key not in payload]
        if missing:
            raise ValidationError(
                "external product missing keys: " + ", ".join(missing)
            )
        k = payload["k"]
        if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
            raise ValidationError("external product k must be a positive integer")
        selected = [
            parse_fraction(v, field=f"selected_volumes[{i}]")
            for i, v in enumerate(payload["selected_volumes"])
        ]
        if len(selected) != k:
            raise ValidationError(
                f"external product declares k={k} but lists "
                f"{len(selected)} selected volumes"
            )
        if any(selected[i] <= selected[i - 1] for i in range(1, len(selected))):
            raise ValidationError(
                "external product selected volumes must be strictly increasing"
            )
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            dup = self.conn.execute(
                "SELECT id FROM endpoint_products WHERE source = 'external'"
                " AND external_ref = ?",
                (str(external_ref),),
            ).fetchone()
            if dup is not None:
                return self.get_endpoint_product(dup["id"])
        cur = self.conn.execute(
            "INSERT INTO endpoint_products(curve_id, curve_name, curve_version,"
            " method_id, method_name, method_version, k, selected_volumes_json,"
            " selected_indices_json, objective, candidates_json,"
            " explanation_json, input_json, source, external_ref, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'external', ?, ?)",
            (
                payload.get("curve_id"),
                str(payload["curve_name"]),
                payload.get("curve_version"),
                payload.get("method_id"),
                str(payload["method_name"]),
                payload.get("method_version"),
                k,
                json.dumps(_flist(selected)),
                json.dumps(payload.get("selected_indices", [])),
                _ftext(parse_fraction(payload.get("objective", 0), field="objective")),
                json.dumps(payload.get("candidates", [])),
                json.dumps(payload.get("explanation", {})),
                json.dumps(payload.get("input", {})),
                None if external_ref is None else str(external_ref),
                _now(),
            ),
        )
        self.conn.commit()
        return self.get_endpoint_product(cur.lastrowid)

    def get_endpoint_product(self, product_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM endpoint_products WHERE id = ?", (product_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"endpoint product {product_id} not found")
        return {
            "id": row["id"],
            "curve_id": row["curve_id"],
            "curve_name": row["curve_name"],
            "curve_version": row["curve_version"],
            "method_id": row["method_id"],
            "method_name": row["method_name"],
            "method_version": row["method_version"],
            "k": row["k"],
            "selected_volumes": json.loads(row["selected_volumes_json"]),
            "selected_indices": json.loads(row["selected_indices_json"]),
            "objective": row["objective"],
            "candidates": json.loads(row["candidates_json"]),
            "explanation": json.loads(row["explanation_json"]),
            "input": json.loads(row["input_json"]),
            "source": row["source"],
            "external_ref": row["external_ref"],
            "created_at": row["created_at"],
        }

    def list_endpoint_products(self, **filters: Any) -> list[dict]:
        query = "SELECT id FROM endpoint_products WHERE 1=1"
        args: list[Any] = []
        for key in ("curve_id", "method_id", "source"):
            if key in filters and filters[key] is not None:
                query += f" AND {key} = ?"
                args.append(filters[key])
        query += " ORDER BY id"
        rows = self.conn.execute(query, args).fetchall()
        return [self.get_endpoint_product(r["id"]) for r in rows]

    def bind_sample_products(
        self, sample_id: int, blank_product_id: int | None, sample_product_id: int | None
    ) -> None:
        """Record which public products the sample currently resolves to.

        Used after a refresh computation so a later ``refresh=False`` audit is
        reproducibly bound to the exact inputs that produced its report.
        """
        self.conn.execute(
            "UPDATE samples SET blank_product_id = ?, sample_product_id = ?"
            " WHERE id = ?",
            (blank_product_id, sample_product_id, sample_id),
        )
        self.conn.commit()

    # -- samples and batches ---------------------------------------------
    def create_sample(self, sample: dict) -> dict:
        cur = self.conn.execute(
            "INSERT INTO samples(name, net_mass, sample_fraction, concentration,"
            " factor, blank_curve_id, sample_curve_id, blank_product_id,"
            " sample_product_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sample["name"],
                _ftext(sample["net_mass"]),
                _ftext(sample["sample_fraction"]),
                _ftext(sample["concentration"]),
                _ftext(sample["factor"]),
                sample.get("blank_curve_id"),
                sample.get("sample_curve_id"),
                sample.get("blank_product_id"),
                sample.get("sample_product_id"),
                _now(),
            ),
        )
        self.conn.commit()
        return self.get_sample(cur.lastrowid)

    def get_sample(self, sample_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM samples WHERE id = ?", (sample_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"sample {sample_id} not found")
        return {
            "id": row["id"],
            "name": row["name"],
            "net_mass": parse_fraction(row["net_mass"]),
            "sample_fraction": parse_fraction(row["sample_fraction"]),
            "concentration": parse_fraction(row["concentration"]),
            "factor": parse_fraction(row["factor"]),
            "blank_curve_id": row["blank_curve_id"],
            "sample_curve_id": row["sample_curve_id"],
            "blank_product_id": row["blank_product_id"],
            "sample_product_id": row["sample_product_id"],
            "created_at": row["created_at"],
        }

    def create_batch(self, name: str, method_id: int, sample_ids: Sequence[int]) -> dict:
        if not sample_ids:
            raise ValidationError("a batch needs at least one sample")
        if len(set(sample_ids)) != len(sample_ids):
            raise ValidationError("duplicate sample id in batch")
        if len(sample_ids) > 30:
            raise ValidationError("at most 30 samples per batch")
        for sid in sample_ids:
            self.get_sample(sid)
        self.get_method(method_id)
        cur = self.conn.execute(
            "INSERT INTO batches(name, method_id, created_at) VALUES (?, ?, ?)",
            (name, method_id, _now()),
        )
        batch_id = cur.lastrowid
        for pos, sid in enumerate(sample_ids):
            self.conn.execute(
                "INSERT INTO batch_samples(batch_id, sample_id, position)"
                " VALUES (?, ?, ?)",
                (batch_id, sid, pos),
            )
        self.conn.commit()
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"batch {batch_id} not found")
        samples = self.conn.execute(
            "SELECT sample_id FROM batch_samples WHERE batch_id = ?"
            " ORDER BY position",
            (batch_id,),
        ).fetchall()
        return {
            "id": row["id"],
            "name": row["name"],
            "method_id": row["method_id"],
            "sample_ids": [r["sample_id"] for r in samples],
            "created_at": row["created_at"],
        }

    def create_report(
        self,
        *,
        batch_id: int,
        method_id: int,
        method_version: int,
        status: str,
        average: Fraction | None,
        details: dict,
    ) -> dict:
        cur = self.conn.execute(
            "INSERT INTO potency_reports(batch_id, method_id, method_version,"
            " status, average, details_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                method_id,
                method_version,
                status,
                None if average is None else _ftext(average),
                json.dumps(details),
                _now(),
            ),
        )
        self.conn.commit()
        return self.get_report(cur.lastrowid)

    def get_report(self, report_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM potency_reports WHERE id = ?", (report_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"potency report {report_id} not found")
        return {
            "id": row["id"],
            "batch_id": row["batch_id"],
            "method_id": row["method_id"],
            "method_version": row["method_version"],
            "status": row["status"],
            "average": None
            if row["average"] is None
            else parse_fraction(row["average"]),
            "details": json.loads(row["details_json"]),
            "created_at": row["created_at"],
        }

    def list_reports(self, batch_id: int | None = None) -> list[dict]:
        if batch_id is None:
            rows = self.conn.execute(
                "SELECT id FROM potency_reports ORDER BY id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id FROM potency_reports WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
        return [self.get_report(r["id"]) for r in rows]

    # -- blind transmissions and determinations ---------------------------
    def latest_transmission_seq(self, determination_uid: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(seq) AS m FROM blind_transmissions"
            " WHERE determination_uid = ?",
            (determination_uid,),
        ).fetchone()
        return None if row["m"] is None else row["m"]

    def create_transmission(
        self, *, blind_code: str, determination_uid: str, retransmission: bool
    ) -> dict:
        cur = self.conn.execute(
            "INSERT INTO blind_transmissions(blind_code, determination_uid,"
            " seq, received_at, retransmission) VALUES (?, ?, ?, ?, ?)",
            (
                blind_code,
                determination_uid,
                self._next_transmission_seq(determination_uid),
                _now(),
                1 if retransmission else 0,
            ),
        )
        self.conn.commit()
        return self.get_transmission(cur.lastrowid)

    def _next_transmission_seq(self, determination_uid: str) -> int:
        latest = self.latest_transmission_seq(determination_uid)
        return 1 if latest is None else latest + 1

    def get_transmission(self, transmission_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM blind_transmissions WHERE id = ?", (transmission_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"transmission {transmission_id} not found")
        return {
            "id": row["id"],
            "blind_code": row["blind_code"],
            "determination_uid": row["determination_uid"],
            "seq": row["seq"],
            "received_at": row["received_at"],
            "retransmission": bool(row["retransmission"]),
        }

    def list_transmissions(self, determination_uid: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id FROM blind_transmissions WHERE determination_uid = ?"
            " ORDER BY seq",
            (determination_uid,),
        ).fetchall()
        return [self.get_transmission(r["id"]) for r in rows]

    def create_determination(self, payload: dict) -> dict:
        cur = self.conn.execute(
            "INSERT INTO blind_determinations(determination_uid, blind_code,"
            " method_id, sample_id, blank_product_id, sample_product_id,"
            " potency, eval_status, failures_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload["determination_uid"],
                payload["blind_code"],
                payload["method_id"],
                payload["sample_id"],
                payload["blank_product_id"],
                payload["sample_product_id"],
                _ftext(payload["potency"]),
                payload["eval_status"],
                json.dumps(payload["failures"]),
                _now(),
            ),
        )
        self.conn.commit()
        return self.get_determination(cur.lastrowid)

    def get_determination_by_uid(self, determination_uid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id FROM blind_determinations WHERE determination_uid = ?",
            (determination_uid,),
        ).fetchone()
        return None if row is None else self.get_determination(row["id"])

    def get_determination(self, determination_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM blind_determinations WHERE id = ?",
            (determination_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"determination {determination_id} not found")
        return {
            "id": row["id"],
            "determination_uid": row["determination_uid"],
            "blind_code": row["blind_code"],
            "method_id": row["method_id"],
            "sample_id": row["sample_id"],
            "blank_product_id": row["blank_product_id"],
            "sample_product_id": row["sample_product_id"],
            "potency": parse_fraction(row["potency"]),
            "eval_status": row["eval_status"],
            "failures": json.loads(row["failures_json"]),
            "created_at": row["created_at"],
        }

    def list_determinations_for_method(self, method_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id FROM blind_determinations WHERE method_id = ? ORDER BY id",
            (method_id,),
        ).fetchall()
        return [self.get_determination(r["id"]) for r in rows]

    # -- blind reveals -----------------------------------------------------
    def create_reveal(self, name: str, entries: Sequence[dict]) -> dict:
        if not name or not str(name).strip():
            raise ValidationError("reveal name must be non-empty")
        if not entries:
            raise ValidationError("a reveal needs at least one entry")
        codes = [e["blind_code"] for e in entries]
        if len(set(codes)) != len(codes):
            raise ValidationError("duplicate blind_code in reveal payload")
        for e in entries:
            day = e["measurement_day"]
            if isinstance(day, bool) or not isinstance(day, int) or day < 0:
                raise ValidationError(
                    "measurement_day must be a non-negative integer day index"
                )
        version_row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM blind_reveals WHERE name = ?",
            (name,),
        ).fetchone()
        version = version_row["v"] + 1
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO blind_reveals(name, version, created_at) VALUES (?, ?, ?)",
            (name, version, now),
        )
        reveal_id = cur.lastrowid
        for e in entries:
            self.conn.execute(
                "INSERT INTO blind_reveal_entries(reveal_id, blind_code,"
                " control_name, measurement_day) VALUES (?, ?, ?, ?)",
                (
                    reveal_id,
                    e["blind_code"],
                    e["control_name"],
                    e["measurement_day"],
                ),
            )
        self.conn.commit()
        return self.get_reveal(reveal_id)

    def get_reveal(self, reveal_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM blind_reveals WHERE id = ?", (reveal_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"blind reveal {reveal_id} not found")
        entries = [
            {
                "blind_code": r["blind_code"],
                "control_name": r["control_name"],
                "measurement_day": r["measurement_day"],
            }
            for r in self.conn.execute(
                "SELECT * FROM blind_reveal_entries WHERE reveal_id = ?"
                " ORDER BY measurement_day, blind_code",
                (reveal_id,),
            ).fetchall()
        ]
        return {
            "id": row["id"],
            "name": row["name"],
            "version": row["version"],
            "created_at": row["created_at"],
            "entries": entries,
        }

    def find_reveal(self, name: str, version: int | None = None) -> dict | None:
        if version is None:
            row = self.conn.execute(
                "SELECT id FROM blind_reveals WHERE name = ? ORDER BY version DESC"
                " LIMIT 1",
                (name,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id FROM blind_reveals WHERE name = ? AND version = ?",
                (name, version),
            ).fetchone()
        return None if row is None else self.get_reveal(row["id"])

    def list_reveals(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id FROM blind_reveals ORDER BY name, version"
        ).fetchall()
        return [self.get_reveal(r["id"]) for r in rows]

    # -- repeatability reports and quotes ---------------------------------
    def create_repeatability_report(
        self,
        *,
        reveal_id: int,
        control_name: str,
        method_id: int,
        method_version: int,
        status: str,
        within_day_variance: Fraction | None,
        between_day_variance: Fraction | None,
        details: dict,
    ) -> dict:
        cur = self.conn.execute(
            "INSERT INTO repeatability_reports(reveal_id, control_name,"
            " method_id, method_version, status, within_day_variance,"
            " between_day_variance, details_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reveal_id,
                control_name,
                method_id,
                method_version,
                status,
                None if within_day_variance is None else _ftext(within_day_variance),
                None if between_day_variance is None else _ftext(between_day_variance),
                json.dumps(details),
                _now(),
            ),
        )
        self.conn.commit()
        return self.get_repeatability_report(cur.lastrowid)

    def find_repeatability_report(
        self, reveal_id: int, control_name: str, method_id: int
    ) -> dict | None:
        row = self.conn.execute(
            "SELECT id FROM repeatability_reports WHERE reveal_id = ? AND"
            " control_name = ? AND method_id = ?",
            (reveal_id, control_name, method_id),
        ).fetchone()
        return None if row is None else self.get_repeatability_report(row["id"])

    def get_repeatability_report(self, report_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM repeatability_reports WHERE id = ?", (report_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"repeatability report {report_id} not found")
        return {
            "id": row["id"],
            "reveal_id": row["reveal_id"],
            "control_name": row["control_name"],
            "method_id": row["method_id"],
            "method_version": row["method_version"],
            "status": row["status"],
            "within_day_variance": None
            if row["within_day_variance"] is None
            else parse_fraction(row["within_day_variance"]),
            "between_day_variance": None
            if row["between_day_variance"] is None
            else parse_fraction(row["between_day_variance"]),
            "details": json.loads(row["details_json"]),
            "created_at": row["created_at"],
        }

    def list_repeatability_reports(self, reveal_id: int | None = None) -> list[dict]:
        if reveal_id is None:
            rows = self.conn.execute(
                "SELECT id FROM repeatability_reports ORDER BY id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id FROM repeatability_reports WHERE reveal_id = ? ORDER BY id",
                (reveal_id,),
            ).fetchall()
        return [self.get_repeatability_report(r["id"]) for r in rows]

    def create_quote(self, *, report_id: int, status: str, details: dict) -> dict:
        cur = self.conn.execute(
            "INSERT INTO retest_quotes(report_id, status, details_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (report_id, status, json.dumps(details), _now()),
        )
        self.conn.commit()
        return self.get_quote(cur.lastrowid)

    def get_quote(self, quote_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM retest_quotes WHERE id = ?", (quote_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"retest quote {quote_id} not found")
        return {
            "id": row["id"],
            "report_id": row["report_id"],
            "status": row["status"],
            "details": json.loads(row["details_json"]),
            "created_at": row["created_at"],
        }

    def list_quotes(self, report_id: int | None = None) -> list[dict]:
        if report_id is None:
            rows = self.conn.execute(
                "SELECT id FROM retest_quotes ORDER BY id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id FROM retest_quotes WHERE report_id = ? ORDER BY id",
                (report_id,),
            ).fetchall()
        return [self.get_quote(r["id"]) for r in rows]
