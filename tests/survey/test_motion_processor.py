"""Offline tests for the `motion` survey processor: tiling, de-duplication, idempotence, abort."""
from contextlib import contextmanager

import numpy as np
import pytest
from survey_fakes import field_object, field_run

from planetx import pipeline
from planetx.spherex import archive, cutouts
from planetx.survey import motion, patch_frames, worker
from planetx.survey.candidates import CandidateStore
from planetx.survey.patches import NEP, patch_center, patch_containing, patch_corners, patches_containing
from planetx.survey.queue import PatchQueue

NSIDE = 64
NEP_IPIX = patch_containing(NEP.ra.deg, NEP.dec.deg, NSIDE)


def _sep_deg(ra1, dec1, ra2, dec2):
    ra1, dec1, ra2, dec2 = map(np.radians, (ra1, dec1, ra2, dec2))
    c = np.sin(dec1) * np.sin(dec2) + np.cos(dec1) * np.cos(dec2) * np.cos(ra1 - ra2)
    return np.degrees(np.arccos(np.clip(c, -1, 1)))


def _points_in_patch(ipix, nside, n=4000, seed=1):
    """Random positions inside the patch (drawn around it, kept if inside)."""
    rng = np.random.default_rng(seed)
    ra0, dec0 = patch_center(ipix, nside)
    dec = np.clip(dec0 + rng.uniform(-1, 1, n * 4), -90, 90)
    ra = (ra0 + rng.uniform(-1, 1, n * 4) / max(np.cos(np.radians(dec0)), 0.05)) % 360
    inside = patches_containing(ra, dec, nside) == ipix
    return ra[inside], dec[inside]


# -- tiling -----------------------------------------------------------------

@pytest.mark.parametrize("ipix", [NEP_IPIX, 0, 24000, 49151])  # pole, equator and a polar-cap patch
def test_fields_cover_the_patch(ipix):
    fields = motion.patch_fields(ipix, NSIDE)
    assert len(fields) == 64
    centres_ra, centres_dec = np.array([f.ra for f in fields]), np.array([f.dec for f in fields])
    assert np.all(patches_containing(centres_ra, centres_dec, NSIDE) == ipix)
    ra, dec = _points_in_patch(ipix, NSIDE)
    corners = np.array(patch_corners(ipix, NSIDE))
    ra, dec = np.concatenate([ra, corners[:, 0]]), np.concatenate([dec, corners[:, 1]])
    assert len(ra) > 500
    nearest = motion.nearest_field(ra, dec, fields)
    dist = _sep_deg(ra, dec, centres_ra[nearest], centres_dec[nearest])
    # Every point (corners included) lies well inside the frames of its nearest field.
    assert dist.max() < 0.125 < motion.FIELD_REACH_DEG


def test_fine_patch_is_one_field():
    assert motion.patch_fields(5, 1024) == [motion.Field(*patch_center(5, 1024))]


# -- processing -------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = tmp_path / "survey.sqlite"
    with PatchQueue(path, create=True) as q:
        q.init(NSIDE)
    return path


def _stored(db):
    with CandidateStore(db, readonly=True) as store:
        return store.patch_candidates(NSIDE, NEP_IPIX)


class FakePatch:
    """Stands in for `patch_frames.load`: records the requests and each field run."""

    def __init__(self, monkeypatch, run, load_error=None):
        self.requests, self.runs, self.run, self.load_error = None, [], run, load_error
        monkeypatch.setattr(patch_frames, "load", self.load)

    @contextmanager
    def load(self, requests, should_stop):
        self.requests = list(requests)
        if self.load_error:
            raise self.load_error
        yield self

    def field_run(self, i, should_stop):
        self.runs.append(i)
        ra, dec, _ = self.requests[i]
        return self.run(i, ra, dec)


@pytest.fixture
def every_field_sees(monkeypatch):
    """Every field sees the same objects (as overlapping fields would); records the calls."""
    ra0, dec0 = patch_center(NEP_IPIX, NSIDE)
    objects = [
        field_object(ra0, dec0, classification="unknown_candidate"),  # inside: kept once
        field_object(ra0 + 0.1, dec0 + 0.05, classification="known_star", name="HD 1"),  # inside: kept once
        field_object(ra0, dec0 + 0.03, kind="static", classification="static"),  # not notable
        field_object(ra0, dec0 + 0.04, kind="static", classification="variable_star", is_variable=True),
        field_object(ra0 + 40.0, dec0 - 5.0),  # another patch's object
    ]
    return FakePatch(monkeypatch, lambda i, ra, dec: field_run(objects))


def test_patch_processed_once_per_field_without_duplicates(db, every_field_sees):
    result = worker.run(worker.PROCESSORS["motion"], db, max_patches=1)
    assert result.done == 1
    fields = motion.patch_fields(NEP_IPIX, NSIDE)
    # One load for the whole patch, with each field's raw cutout request; then one run per field.
    assert every_field_sees.requests == [cutouts.cutout_request(f.ra, f.dec, motion.FIELD_SIZE_DEG) for f in fields]
    assert every_field_sees.runs == list(range(64))
    stored = _stored(db)
    assert [(s.candidate.classification, s.candidate.name) for s in stored] == [
        ("unknown_candidate", None), ("variable_star", None), ("known_star", "HD 1")
    ]
    assert stored[0].candidate.score > 0.9 and stored[-1].candidate.score < 0.2
    assert len(stored[0].candidate.track) == 3
    with PatchQueue(db) as q:
        summary = q.patch_row(NSIDE, NEP_IPIX)["summary"]
    assert summary["fields"] == 64 and summary["fields_analysed"] == 64
    assert summary["candidates"] == 3 and summary["objects"] == 4
    assert summary["by_classification"] == {"unknown_candidate": 1, "variable_star": 1, "known_star": 1}


def test_reprocessing_replaces_the_patch_output(db, every_field_sees):
    patch = worker.Patch(NSIDE, NEP_IPIX, 0, 0)
    fields = motion.patch_fields(NEP_IPIX, NSIDE)
    for _ in range(2):
        found, _summary = motion.analyse_patch(patch, fields, lambda: False)
        motion.store(db, patch, found)
    assert len(_stored(db)) == 3


def test_fields_without_data_are_skipped(db, monkeypatch):
    def run(i, ra, dec):
        if i % 2 == 0:
            raise pipeline.FieldFailed("SPHEREx has too few exposures")
        return field_run([field_object(ra, dec)])

    FakePatch(monkeypatch, run)
    patch = worker.Patch(NSIDE, NEP_IPIX, 0, 0)
    found, summary = motion.analyse_patch(patch, motion.patch_fields(NEP_IPIX, NSIDE), lambda: False)
    assert summary["fields_without_data"] == 32 and summary["fields_analysed"] == 32
    assert len(found) == 32


def test_abort_between_fields_keeps_the_previous_output(db, every_field_sees):
    worker.run(worker.PROCESSORS["motion"], db, max_patches=1)
    before = [s.candidate for s in _stored(db)]
    assert before
    every_field_sees.runs.clear()
    stop_after = 3
    ctx = worker.PatchContext(worker.Patch(NSIDE, NEP_IPIX, 0, 0), db, *(worker.threading.Event() for _ in "ab"))
    # should_stop() turns true after three fields: the processor gives up before storing anything.
    object.__setattr__(ctx, "should_stop", lambda: len(every_field_sees.runs) >= stop_after)
    with pytest.raises(worker.PatchAbandoned):
        motion.process_patch(ctx)
    assert len(every_field_sees.runs) == stop_after
    assert [s.candidate for s in _stored(db)] == before


@pytest.mark.parametrize("where", ["field", "load"])
def test_stop_inside_the_pipeline_abandons_the_patch(db, monkeypatch, where):
    def stopped(*a):
        raise pipeline.Stopped()

    FakePatch(monkeypatch, stopped, load_error=pipeline.Stopped() if where == "load" else None)
    result = worker.run(worker.PROCESSORS["motion"], db, max_patches=1)
    assert result.released == 1 and result.done == 0 and result.failed == 0
    with PatchQueue(db) as q:
        assert q.patch_row(NSIDE, NEP_IPIX)["status"] == "pending"
    assert _stored(db) == []


@pytest.mark.parametrize("where", ["field", "load"])
def test_irsa_outage_fails_the_patch_for_a_retry(db, monkeypatch, where):
    outage = archive.UpstreamError("IRSA 503", status=503)

    def down(*a):
        raise outage

    FakePatch(monkeypatch, down, load_error=outage if where == "load" else None)
    result = worker.run(worker.PROCESSORS["motion"], db, max_patches=1)
    assert result.failed == 1
    with PatchQueue(db) as q:
        row = q.patch_row(NSIDE, NEP_IPIX)
    assert row["status"] == "pending" and row["attempts"] == 1 and "UpstreamError" in row["last_error"]
