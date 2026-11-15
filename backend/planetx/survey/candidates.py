"""Notable objects the survey found, stored next to the patch queue.

One row per mover, motion candidate or brightness variable, written by the
`motion` processor. Rows are keyed by the patch that produced them: processing
is at-least-once, so `replace_patch` swaps a patch's whole previous output for
the new one in one transaction (re-processing never duplicates rows). Row ids
are therefore not stable across re-processing.

The score (0-1, `score`) orders the list for people: an uncatalogued mover that
SPHEREx measures clearly comes first, catalogued stars and artifacts last.

Each row also carries its motion verdict (planetx.classify.reliability: is the
motion real?). The verdict columns came after the first survey runs: a writable
store adds them to an older table, and a row stored without a verdict gets one
from its other columns when read (`StoredCandidate.motion_verdict`), also inside
SQL (the `planetx_verdict` function), so filtering on the verdict covers it too.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from planetx.classify.reliability import (
    CAUSES,
    CERTAINTIES,
    DECIDERS,
    VERDICTS,
    MotionVerdict,
    stored_verdict,
    stored_verdict_sql,
)
from planetx.classify.rules import CLASSIFICATIONS

KINDS = ("mover", "candidate", "static")
# List orders: best score first, or newest run first (then best score).
SORTS = {"score": "score DESC, id", "date": "found_at DESC, score DESC, id"}
BUSY_TIMEOUT_S = 30.0

# Score = tier + up to TIER_BONUS for significance, so a class never overtakes the
# tier above it. The tier is set by the catalogue classification and the motion
# class (classification None: the catalogue check could not run).
_TIERS: dict[tuple[str | None, str], float] = {
    ("unknown_candidate", "mover"): 0.9,  # significant motion no catalogue explains
    (None, "mover"): 0.7,  # significant motion, catalogues not consulted
    ("unknown_candidate", "candidate"): 0.6,  # 3-5 sigma motion, unexplained
    ("unknown_candidate", "static"): 0.5,  # brightness change of an uncatalogued variable
    (None, "candidate"): 0.4,
    (None, "static"): 0.3,
}
_CLASS_TIERS: dict[str, float] = {
    "known_asteroid": 0.3,
    "variable_star": 0.2,
    "known_star": 0.1,
    "artifact": 0.0,
    "static": 0.0,
}
TIER_BONUS = 0.09
# Significance (sigma) at which the bonus starts and where it is full.
BONUS_FROM_SIGMA = 3.0
BONUS_FULL_SIGMA = 10.0

# Motion verdict columns (planetx.classify.reliability.MotionVerdict), NULL in rows stored
# without one. Older tables get them with ALTER TABLE, so each is nullable.
_VERDICT_DEFINITIONS = (
    ("verdict", f"TEXT CHECK (verdict IS NULL OR verdict IN {VERDICTS!r})"),
    ("verdict_certainty", f"TEXT CHECK (verdict_certainty IS NULL OR verdict_certainty IN {CERTAINTIES!r})"),
    ("verdict_cause", f"TEXT CHECK (verdict_cause IS NULL OR verdict_cause IN {CAUSES!r})"),
    ("verdict_reason", "TEXT"),
    ("verdict_decided_by", f"TEXT CHECK (verdict_decided_by IS NULL OR verdict_decided_by IN {DECIDERS!r})"),
)
VERDICT_COLUMNS = tuple(name for name, _ in _VERDICT_DEFINITIONS)
# Every row's verdict in SQL: the stored one, else judged from the row's columns.
_JUDGED_SQL = "planetx_verdict(kind, classification, archive_verdict, significance, track, is_variable)"
_VERDICT_SQL = f"COALESCE(verdict, {_JUDGED_SQL})"
_VERDICT_COLUMNS_SQL = "".join(f"\n        {name} {definition}," for name, definition in _VERDICT_DEFINITIONS)

_SCHEMA = (
    f"""
    CREATE TABLE IF NOT EXISTS candidates (
        id                 INTEGER PRIMARY KEY,
        nside              INTEGER NOT NULL,
        ipix               INTEGER NOT NULL,
        ra                 REAL    NOT NULL,  -- deg, ICRS, at ref_mjd
        dec                REAL    NOT NULL,
        ref_mjd            REAL    NOT NULL,
        kind               TEXT    NOT NULL CHECK (kind IN {KINDS!r}),
        classification     TEXT    CHECK (classification IS NULL OR classification IN {CLASSIFICATIONS!r}),
        name               TEXT,
        is_variable        INTEGER NOT NULL CHECK (is_variable IN (0, 1)),
        pm_ra_cosdec       REAL    NOT NULL,  -- arcsec/yr, SPHEREx fit (as the website's MotionObject)
        pm_dec             REAL    NOT NULL,
        pm_total           REAL    NOT NULL,
        pm_err             REAL    NOT NULL,
        significance       REAL    NOT NULL,  -- pm_total / pm_err
        position_angle_deg REAL    NOT NULL,  -- east of north
        archive_verdict    TEXT,              -- archival refit verdict (planetx.observatories.Verdict)
        track              TEXT    NOT NULL,  -- JSON [{{ra, dec, mjd, sigma_arcsec, n_frames}}], one per pass
        score              REAL    NOT NULL CHECK (score >= 0 AND score <= 1),
        field_ra           REAL    NOT NULL,  -- centre of the field the object was measured in
        field_dec          REAL    NOT NULL,
        found_at           REAL    NOT NULL,  -- Unix seconds of the run that stored it{_VERDICT_COLUMNS_SQL}
        FOREIGN KEY (nside, ipix) REFERENCES patches (nside, ipix)
    )
    """,
    "CREATE INDEX IF NOT EXISTS candidates_patch ON candidates (nside, ipix)",
    "CREATE INDEX IF NOT EXISTS candidates_score ON candidates (score DESC, id)",
    "CREATE INDEX IF NOT EXISTS candidates_class_score ON candidates (classification, score DESC, id)",
)

_COLUMNS = (
    "ra", "dec", "ref_mjd", "kind", "classification", "name", "is_variable", "pm_ra_cosdec", "pm_dec",
    "pm_total", "pm_err", "significance", "position_angle_deg", "archive_verdict", "track", "score",
    "field_ra", "field_dec",
)
_ALL_COLUMNS = _COLUMNS + VERDICT_COLUMNS


def is_notable(kind: str, is_variable: bool) -> bool:
    """Whether an object belongs in the candidates table: it moves (or may) or it varies."""
    return kind in ("mover", "candidate") or is_variable


def score(
    kind: str, classification: str | None, *, significance: float, variability_significance: float | None = None
) -> float:
    """How interesting a notable object is, 0-1 (see the tiers above).

    `significance` is the motion's (pm_total / pm_err); a static variable is
    ranked by `variability_significance` instead. Unknown kinds or
    classifications raise ValueError.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if classification is not None and classification not in CLASSIFICATIONS:
        raise ValueError(f"unknown classification {classification!r}")
    tier = _TIERS.get((classification, kind))
    if tier is None:
        tier = _CLASS_TIERS[classification]  # every non-None classification not in _TIERS is here
    sigma = significance if kind != "static" else (variability_significance or 0.0)
    if not math.isfinite(sigma):
        sigma = 0.0
    frac = min(1.0, max(0.0, (sigma - BONUS_FROM_SIGMA) / (BONUS_FULL_SIGMA - BONUS_FROM_SIGMA)))
    return round(tier + TIER_BONUS * frac, 4)


@dataclass(frozen=True)
class TrackPoint:
    ra: float
    dec: float
    mjd: float
    sigma_arcsec: float
    n_frames: int


@dataclass(frozen=True)
class Candidate:
    """One notable object, as stored (without its patch key, id and timestamp)."""

    ra: float
    dec: float
    ref_mjd: float
    kind: str
    classification: str | None
    name: str | None
    is_variable: bool
    pm_ra_cosdec: float
    pm_dec: float
    pm_total: float
    pm_err: float
    significance: float
    position_angle_deg: float
    archive_verdict: str | None
    track: tuple[TrackPoint, ...]
    score: float
    field_ra: float
    field_dec: float
    # Is the motion real? From the full pipeline object (reliability.object_verdict). None
    # stores no verdict; reading the row then judges it from its other columns.
    verdict: MotionVerdict | None = None

    def _values(self) -> tuple[Any, ...]:
        track = json.dumps([vars(p) for p in self.track], allow_nan=False)
        values = {**vars(self), "track": track, "is_variable": int(self.is_variable)}
        for name in ("ra", "dec", "ref_mjd", "pm_ra_cosdec", "pm_dec", "pm_total", "pm_err",
                     "significance", "position_angle_deg", "score", "field_ra", "field_dec"):
            if not math.isfinite(values[name]):
                raise ValueError(f"candidate {name} is not finite: {values[name]!r}")
        v = self.verdict
        verdict = (v.verdict, v.certainty, v.cause, v.reason, v.decided_by) if v else (None,) * len(VERDICT_COLUMNS)
        return (*(values[c] for c in _COLUMNS), *verdict)


@dataclass(frozen=True)
class StoredCandidate:
    id: int
    nside: int
    ipix: int
    candidate: Candidate
    found_at: float

    @property
    def found(self) -> str:
        """UTC date (YYYY-MM-DD) of the run that stored it."""
        return datetime.fromtimestamp(self.found_at, UTC).date().isoformat()

    @property
    def motion_verdict(self) -> MotionVerdict:
        """The stored verdict; for a row stored without one, a verdict judged from its other columns."""
        c = self.candidate
        if c.verdict is not None:
            return c.verdict
        return stored_verdict(c.kind, c.classification, c.archive_verdict, c.significance, len(c.track), c.is_variable)


class CandidateStore:
    """Connection to the candidates table of a survey database. One per thread.

    The database must exist (FileNotFoundError otherwise): it is created by the
    patch queue. A writable store creates the table if it is missing, and adds the
    verdict columns to a table made before them; `readonly=True` never writes, lists
    nothing until the table exists, and reads missing verdict columns as NULL.
    """

    def __init__(self, path: Path | str, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        if not self.path.is_file():
            raise FileNotFoundError(f"survey database not found: {self.path} (run `init` first)")
        uri = f"{self.path.resolve().as_uri()}?mode={'ro' if readonly else 'rw'}"
        self._conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.create_function("planetx_verdict", 6, stored_verdict_sql, deterministic=True)
            if not readonly and self._needs_schema():
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    if not self._has_table():
                        for statement in _SCHEMA:
                            self._conn.execute(statement)
                    self._migrate()
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                self._conn.execute("COMMIT")
            present = self._columns()
            self._select = ", ".join(c if c in present else f"NULL AS {c}" for c in _ALL_COLUMNS)
            self._verdict_sql = _VERDICT_SQL if "verdict" in present else _JUDGED_SQL
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _has_table(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'candidates'"
        ).fetchone()
        return row is not None

    def _columns(self) -> set[str]:
        return {row[1] for row in self._conn.execute("PRAGMA table_info(candidates)")}

    def _needs_schema(self) -> bool:
        """Whether the table or any verdict column is missing (checked before taking the write lock)."""
        return not self._has_table() or not set(VERDICT_COLUMNS) <= self._columns()

    def _migrate(self) -> None:
        """Add the verdict columns a table made before them lacks (inside the caller's transaction).

        Nullable columns only, which SQLite adds without rewriting the table; a survey
        worker still running older code keeps inserting rows (they get no verdict).
        """
        present = self._columns()
        for name, definition in _VERDICT_DEFINITIONS:
            if name not in present:
                self._conn.execute(f"ALTER TABLE candidates ADD COLUMN {name} {definition}")

    def replace_patch(self, nside: int, ipix: int, candidates: Sequence[Candidate], found_at: float) -> None:
        """Make `candidates` the whole stored output of patch (nside, ipix), atomically.

        Raises sqlite3.IntegrityError if the patch is not in the queue, and
        ValueError for a non-finite value (nothing is written then).
        """
        if self.readonly:
            raise PermissionError("candidate store was opened read-only")
        rows = [(nside, ipix, *c._values(), found_at) for c in candidates]
        placeholders = ", ".join("?" * (len(_ALL_COLUMNS) + 3))
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("DELETE FROM candidates WHERE nside = ? AND ipix = ?", (nside, ipix))
            self._conn.executemany(
                f"INSERT INTO candidates (nside, ipix, {', '.join(_ALL_COLUMNS)}, found_at) VALUES ({placeholders})",
                rows,
            )
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def page(
        self, *, limit: int, offset: int = 0, classification: str | None = None, sort: str = "score",
        hide_fake: bool = False,
    ) -> tuple[int, list[StoredCandidate]]:
        """(total matching, one page of them), ordered by `sort` (see SORTS), ties by id.

        `hide_fake` leaves out rows whose motion verdict (stored, or judged from the
        row's columns) is fake_motion.
        """
        if sort not in SORTS:
            raise ValueError(f"sort must be one of {', '.join(SORTS)}, got {sort!r}")
        if not self._has_table():
            return 0, []
        conditions: list[str] = []
        params: list[Any] = []
        if classification:
            conditions.append("classification = ?")
            params.append(classification)
        if hide_fake:
            conditions.append(f"{self._verdict_sql} != 'fake_motion'")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        total = self._conn.execute(f"SELECT COUNT(*) FROM candidates {where}", params).fetchone()[0]
        cursor = self._conn.execute(
            f"SELECT id, nside, ipix, {self._select}, found_at FROM candidates {where}"
            f" ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return total, [_stored(row) for row in cursor]

    def get(self, id_: int) -> StoredCandidate | None:
        """One stored candidate by row id, or None (ids change when a patch is re-processed)."""
        if not self._has_table():
            return None
        row = self._conn.execute(
            f"SELECT id, nside, ipix, {self._select}, found_at FROM candidates WHERE id = ?", (id_,)
        ).fetchone()
        return None if row is None else _stored(row)

    def patch_candidates(self, nside: int, ipix: int) -> list[StoredCandidate]:
        """Everything stored for one patch, best score first."""
        if not self._has_table():
            return []
        cursor = self._conn.execute(
            f"SELECT id, nside, ipix, {self._select}, found_at FROM candidates"
            " WHERE nside = ? AND ipix = ? ORDER BY score DESC, id",
            (nside, ipix),
        )
        return [_stored(row) for row in cursor]


def _stored(row: tuple[Any, ...]) -> StoredCandidate:
    id_, nside, ipix, *values, found_at = row
    fields: dict[str, Any] = dict(zip(_COLUMNS, values[: len(_COLUMNS)], strict=True))
    fields["is_variable"] = bool(fields["is_variable"])
    fields["track"] = tuple(TrackPoint(**p) for p in json.loads(fields["track"]))
    verdict, certainty, cause, reason, decided_by = values[len(_COLUMNS):]
    if verdict is not None:
        fields["verdict"] = MotionVerdict(
            verdict=verdict, certainty=certainty, cause=cause, reason=reason or "", decided_by=decided_by,
        )
    return StoredCandidate(id=id_, nside=nside, ipix=ipix, candidate=Candidate(**fields), found_at=found_at)
