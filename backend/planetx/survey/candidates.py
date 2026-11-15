"""Notable objects the survey found, stored next to the patch queue.

One row per mover, motion candidate or brightness variable, written by the
`motion` processor. Rows are keyed by the patch that produced them: processing
is at-least-once, so `replace_patch` swaps a patch's whole previous output for
the new one in one transaction (re-processing never duplicates rows). Row ids
are therefore not stable across re-processing.

The score (0-1, `score`) orders the list for people: an uncatalogued mover that
SPHEREx measures clearly comes first, catalogued stars and artifacts last.
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
        found_at           REAL    NOT NULL,  -- Unix seconds of the run that stored it
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

    def _values(self) -> tuple[Any, ...]:
        track = json.dumps([vars(p) for p in self.track], allow_nan=False)
        values = {**vars(self), "track": track, "is_variable": int(self.is_variable)}
        for name in ("ra", "dec", "ref_mjd", "pm_ra_cosdec", "pm_dec", "pm_total", "pm_err",
                     "significance", "position_angle_deg", "score", "field_ra", "field_dec"):
            if not math.isfinite(values[name]):
                raise ValueError(f"candidate {name} is not finite: {values[name]!r}")
        return tuple(values[c] for c in _COLUMNS)


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


class CandidateStore:
    """Connection to the candidates table of a survey database. One per thread.

    The database must exist (FileNotFoundError otherwise): it is created by the
    patch queue. A writable store creates the table if it is missing;
    `readonly=True` never writes and lists nothing until the table exists.
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
            if not readonly and not self._has_table():
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _SCHEMA:
                        self._conn.execute(statement)
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                self._conn.execute("COMMIT")
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

    def replace_patch(self, nside: int, ipix: int, candidates: Sequence[Candidate], found_at: float) -> None:
        """Make `candidates` the whole stored output of patch (nside, ipix), atomically.

        Raises sqlite3.IntegrityError if the patch is not in the queue, and
        ValueError for a non-finite value (nothing is written then).
        """
        if self.readonly:
            raise PermissionError("candidate store was opened read-only")
        rows = [(nside, ipix, *c._values(), found_at) for c in candidates]
        placeholders = ", ".join("?" * (len(_COLUMNS) + 3))
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("DELETE FROM candidates WHERE nside = ? AND ipix = ?", (nside, ipix))
            self._conn.executemany(
                f"INSERT INTO candidates (nside, ipix, {', '.join(_COLUMNS)}, found_at) VALUES ({placeholders})",
                rows,
            )
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def page(
        self, *, limit: int, offset: int = 0, classification: str | None = None, sort: str = "score"
    ) -> tuple[int, list[StoredCandidate]]:
        """(total matching, one page of them), ordered by `sort` (see SORTS), ties by id."""
        if sort not in SORTS:
            raise ValueError(f"sort must be one of {', '.join(SORTS)}, got {sort!r}")
        if not self._has_table():
            return 0, []
        where, params = ("WHERE classification = ?", [classification]) if classification else ("", [])
        total = self._conn.execute(f"SELECT COUNT(*) FROM candidates {where}", params).fetchone()[0]
        cursor = self._conn.execute(
            f"SELECT id, nside, ipix, {', '.join(_COLUMNS)}, found_at FROM candidates {where}"
            f" ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return total, [_stored(row) for row in cursor]

    def get(self, id_: int) -> StoredCandidate | None:
        """One stored candidate by row id, or None (ids change when a patch is re-processed)."""
        if not self._has_table():
            return None
        row = self._conn.execute(
            f"SELECT id, nside, ipix, {', '.join(_COLUMNS)}, found_at FROM candidates WHERE id = ?", (id_,)
        ).fetchone()
        return None if row is None else _stored(row)

    def patch_candidates(self, nside: int, ipix: int) -> list[StoredCandidate]:
        """Everything stored for one patch, best score first."""
        if not self._has_table():
            return []
        cursor = self._conn.execute(
            f"SELECT id, nside, ipix, {', '.join(_COLUMNS)}, found_at FROM candidates"
            " WHERE nside = ? AND ipix = ? ORDER BY score DESC, id",
            (nside, ipix),
        )
        return [_stored(row) for row in cursor]


def _stored(row: tuple[Any, ...]) -> StoredCandidate:
    id_, nside, ipix, *values, found_at = row
    fields = dict(zip(_COLUMNS, values, strict=True))
    fields["is_variable"] = bool(fields["is_variable"])
    fields["track"] = tuple(TrackPoint(**p) for p in json.loads(fields["track"]))
    return StoredCandidate(id=id_, nside=nside, ipix=ipix, candidate=Candidate(**fields), found_at=found_at)
