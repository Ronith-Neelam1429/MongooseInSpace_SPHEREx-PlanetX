"""The motion verdict in the survey candidates table: storage, migration of older tables, the hide-fake filter."""
import dataclasses
import sqlite3

import pytest
from survey_fakes import candidate, field_object

from planetx.classify.reliability import MotionVerdict
from planetx.survey import motion
from planetx.survey.candidates import VERDICT_COLUMNS, CandidateStore
from planetx.survey.queue import PatchQueue

NSIDE = 1
FAKE = MotionVerdict("fake_motion", "Sits on a slow Gaia star.", "catalogue", cause="saturated_halo")
MOVING = MotionVerdict("moving", "SPHEREx alone.", "spherex", certainty="candidate")

# The candidates table as the first survey runs made it (before the verdict columns).
OLD_TABLE = """
CREATE TABLE candidates (
    id INTEGER PRIMARY KEY, nside INTEGER NOT NULL, ipix INTEGER NOT NULL, ra REAL NOT NULL, dec REAL NOT NULL,
    ref_mjd REAL NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('mover', 'candidate', 'static')),
    classification TEXT, name TEXT, is_variable INTEGER NOT NULL CHECK (is_variable IN (0, 1)),
    pm_ra_cosdec REAL NOT NULL, pm_dec REAL NOT NULL, pm_total REAL NOT NULL, pm_err REAL NOT NULL,
    significance REAL NOT NULL, position_angle_deg REAL NOT NULL, archive_verdict TEXT, track TEXT NOT NULL,
    score REAL NOT NULL CHECK (score >= 0 AND score <= 1), field_ra REAL NOT NULL, field_dec REAL NOT NULL,
    found_at REAL NOT NULL, FOREIGN KEY (nside, ipix) REFERENCES patches (nside, ipix)
)
"""
OLD_ROW = (
    "INSERT INTO candidates (nside, ipix, ra, dec, ref_mjd, kind, classification, name, is_variable, pm_ra_cosdec,"
    " pm_dec, pm_total, pm_err, significance, position_angle_deg, archive_verdict, track, score, field_ra, field_dec,"
    " found_at) VALUES (1, 0, 10, 20, 61080, 'mover', NULL, NULL, 0, 0.1, 0.2, 0.22, 0.1, 2.2, 26, ?,"
    " '[{\"ra\": 10, \"dec\": 20, \"mjd\": 60900, \"sigma_arcsec\": 0.1, \"n_frames\": 6}]', 0.7, 10, 20, 0)"
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "survey.sqlite"
    with PatchQueue(path, create=True) as q:
        q.init(NSIDE)
    return path


@pytest.fixture
def old_db(db):
    """A survey database whose candidates table predates the verdict columns, with two rows."""
    with sqlite3.connect(db) as conn:
        conn.execute(OLD_TABLE)
        conn.execute(OLD_ROW, ("confirmed",))
        conn.execute(OLD_ROW, ("refuted",))
    return db


def columns(path):
    with sqlite3.connect(path) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}


def test_a_stored_verdict_round_trips(db):
    with CandidateStore(db) as store:
        store.replace_patch(NSIDE, 0, [dataclasses.replace(candidate(), verdict=FAKE), candidate(score=0.4)], 0.0)
        rows = store.patch_candidates(NSIDE, 0)
    assert rows[0].candidate.verdict == FAKE and rows[0].motion_verdict == FAKE
    # No verdict stored: judged from the columns (a catalogued star moving the same way).
    assert rows[1].candidate.verdict is None
    assert (rows[1].motion_verdict.verdict, rows[1].motion_verdict.decided_by) == ("moving", "catalogue")


def test_the_table_refuses_unknown_verdicts(db):
    with CandidateStore(db) as store:
        bad = dataclasses.replace(candidate(), verdict=dataclasses.replace(MOVING))
        object.__setattr__(bad.verdict, "verdict", "teleported")
        with pytest.raises(sqlite3.IntegrityError):
            store.replace_patch(NSIDE, 0, [bad], 0.0)


def test_hide_fake_counts_stored_and_judged_verdicts(db):
    refuted = dataclasses.replace(candidate(score=0.3), archive_verdict="refuted")  # judged fake from its columns
    with CandidateStore(db) as store:
        store.replace_patch(NSIDE, 0, [
            dataclasses.replace(candidate(score=0.9), verdict=FAKE),
            dataclasses.replace(candidate(score=0.6), verdict=MOVING),
            refuted,
            candidate(score=0.1),
        ], 0.0)
        assert store.page(limit=10)[0] == 4
        total, rows = store.page(limit=10, hide_fake=True)
        assert total == 2 and [r.candidate.score for r in rows] == [0.6, 0.1]
        assert store.page(limit=10, hide_fake=True, classification="known_star")[0] == 2
        assert store.page(limit=1, offset=1, hide_fake=True)[1][0].candidate.score == 0.1


def test_a_writable_store_adds_the_verdict_columns_and_keeps_the_rows(old_db):
    assert not set(VERDICT_COLUMNS) & columns(old_db)
    with CandidateStore(old_db) as store:
        total, rows = store.page(limit=10)
        assert total == 2 and all(r.candidate.verdict is None for r in rows)
        assert sorted(r.motion_verdict.verdict for r in rows) == ["fake_motion", "moving"]
        store.replace_patch(NSIDE, 1, [dataclasses.replace(candidate(), verdict=FAKE)], 0.0)
        assert store.page(limit=10, hide_fake=True)[0] == 1
    assert set(VERDICT_COLUMNS) <= columns(old_db)
    with CandidateStore(old_db) as again:  # migrating twice is a no-op
        assert again.page(limit=10)[0] == 3


def test_older_code_can_still_insert_after_the_migration(old_db):
    CandidateStore(old_db).close()
    with sqlite3.connect(old_db) as conn:  # a survey worker still running the old INSERT
        conn.execute(OLD_ROW, ("unconfirmed",))
    with CandidateStore(old_db, readonly=True) as store:
        rows = store.page(limit=10)[1]
    assert len(rows) == 3 and all(r.candidate.verdict is None for r in rows)


def test_a_readonly_store_reads_an_unmigrated_table(old_db):
    with CandidateStore(old_db, readonly=True) as store:
        total, rows = store.page(limit=10)
        assert total == 2 and all(r.candidate.verdict is None for r in rows)
        hidden = store.page(limit=10, hide_fake=True)
        assert hidden[0] == 1 and hidden[1][0].candidate.archive_verdict == "confirmed"
        again = store.get(rows[0].id)
        assert again is not None and again.candidate == rows[0].candidate
    assert not set(VERDICT_COLUMNS) & columns(old_db)  # read-only never migrates


def test_the_survey_processor_stores_the_full_verdict():
    found = field_object(10.0, 20.0)
    c = motion._candidate(found, motion.Field(10.0, 20.0))
    assert c.verdict is not None and (c.verdict.verdict, c.verdict.certainty) == ("moving", "candidate")
