"""Offline tests for the survey candidates table and its score."""
import sqlite3

import pytest
from survey_fakes import candidate

from planetx.survey.candidates import CandidateStore, score
from planetx.survey.queue import PatchQueue

NSIDE = 1


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "survey.sqlite"
    with PatchQueue(path, create=True) as q:
        q.init(NSIDE)
    return path


# -- score ------------------------------------------------------------------

def test_uncatalogued_significant_mover_ranks_highest_and_known_star_low():
    unknown_mover = score("mover", "unknown_candidate", significance=12.0)
    assert unknown_mover == pytest.approx(0.99)
    others = [
        score("mover", None, significance=12.0),
        score("candidate", "unknown_candidate", significance=4.5),
        score("static", "unknown_candidate", significance=0.0, variability_significance=8.0),
        score("mover", "known_asteroid", significance=20.0),
        score("static", "variable_star", significance=0.0, variability_significance=20.0),
        score("mover", "known_star", significance=50.0),
        score("static", "artifact", significance=0.0, variability_significance=9.0),
    ]
    assert all(unknown_mover > s for s in others)
    assert others == sorted(others, reverse=True)
    assert score("mover", "known_star", significance=50.0) < 0.2
    assert all(0.0 <= s <= 1.0 for s in others)


def test_significance_raises_score_within_its_tier_only():
    weak = score("mover", "unknown_candidate", significance=3.0)
    strong = score("mover", "unknown_candidate", significance=100.0)
    assert weak == pytest.approx(0.9) and strong == pytest.approx(0.99)
    # A static variable is ranked by its brightness change, not its (meaningless) motion.
    assert score("static", "unknown_candidate", significance=50.0, variability_significance=3.0) == pytest.approx(0.5)
    assert score("mover", "unknown_candidate", significance=float("nan")) == pytest.approx(0.9)


@pytest.mark.parametrize("kind, classification", [("wobbler", None), ("mover", "planet")])
def test_score_rejects_unknown_labels(kind, classification):
    with pytest.raises(ValueError):
        score(kind, classification, significance=5.0)


# -- store ------------------------------------------------------------------

def test_replace_patch_is_idempotent_and_keyed_by_patch(db):
    with CandidateStore(db) as store:
        store.replace_patch(NSIDE, 3, [candidate(score=0.2), candidate(score=0.9)], found_at=0.0)
        store.replace_patch(NSIDE, 4, [candidate(score=0.5)], found_at=0.0)
        # Re-processing patch 3 replaces its rows; patch 4 is untouched.
        store.replace_patch(NSIDE, 3, [candidate(score=0.3), candidate(score=0.8)], found_at=86400.0)
        total, rows = store.page(limit=10)
    assert total == 3
    assert [(r.ipix, r.candidate.score) for r in rows] == [(3, 0.8), (4, 0.5), (3, 0.3)]
    assert rows[0].found == "1970-01-02" and rows[1].found == "1970-01-01"
    assert rows[0].candidate == candidate(score=0.8)  # track and every field round-trip


def test_page_filters_and_paginates(db):
    with CandidateStore(db) as store:
        store.replace_patch(NSIDE, 0, [candidate(score=s / 10, classification=c) for s, c in
                                       [(9, "unknown_candidate"), (5, None), (3, "known_star"), (1, "known_star")]],
                            found_at=0.0)
        assert store.page(limit=2, offset=1)[0] == 4
        assert [r.candidate.score for r in store.page(limit=2, offset=1)[1]] == [0.5, 0.3]
        total, rows = store.page(limit=10, classification="known_star")
        assert total == 2 and [r.candidate.score for r in rows] == [0.3, 0.1]
        assert store.page(limit=10, offset=10) == (4, [])


def test_failed_replace_keeps_previous_output(db):
    with CandidateStore(db) as store:
        store.replace_patch(NSIDE, 0, [candidate(score=0.4)], found_at=0.0)
        bad = candidate(score=0.5)
        object.__setattr__(bad, "pm_total", float("nan"))
        with pytest.raises(ValueError):
            store.replace_patch(NSIDE, 0, [candidate(score=0.6), bad], found_at=1.0)
        with pytest.raises(sqlite3.IntegrityError):  # not a patch of this queue
            store.replace_patch(64, 123, [candidate()], found_at=1.0)
        assert [r.candidate.score for r in store.patch_candidates(NSIDE, 0)] == [0.4]


def test_constraints_reject_invalid_rows(db):
    with CandidateStore(db) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.replace_patch(NSIDE, 0, [candidate(score=1.5)], found_at=0.0)
        with pytest.raises(sqlite3.IntegrityError):
            store.replace_patch(NSIDE, 0, [candidate(classification="planet")], found_at=0.0)


def test_readonly_store_before_the_table_exists(db):
    with CandidateStore(db, readonly=True) as store:
        assert store.page(limit=5) == (0, [])
        with pytest.raises(PermissionError):
            store.replace_patch(NSIDE, 0, [], found_at=0.0)


def test_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        CandidateStore(tmp_path / "nope.sqlite")
    assert not (tmp_path / "nope.sqlite").exists()
