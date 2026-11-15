"""pipeline reports when the catalogue check could not run, so the survey can check those fields again."""
from types import SimpleNamespace

import pytest
from survey_fakes import field_object

from planetx import pipeline
from planetx.classify import catalogs, check_objects


def _gaia(monkeypatch, fetch_gaia):
    """The real catalogue check with a fake Gaia (SIMBAD down: it only adds names; no SkyBoT)."""
    def simbad(*a):
        raise catalogs.CatalogError("SIMBAD request failed: ConnectTimeout")

    monkeypatch.setattr(pipeline, "check_objects", lambda objs, fused, pixel, stacked=False: check_objects(
        objs, fused, pixel, stacked=stacked, fetch_gaia=fetch_gaia, fetch_simbad=simbad,
        fetch_skybot=lambda *a: []))


@pytest.fixture
def engine_result(monkeypatch):
    obj = field_object(270.0, 66.0, kind="static", classification=None)
    monkeypatch.setattr(pipeline, "parse_observatories", lambda: frozenset({"spherex", "2mass"}))
    monkeypatch.setattr(pipeline, "refine_objects",
                        lambda objects, observatories: SimpleNamespace(fused=[obj.fused] * len(objects), notes=[]))
    return SimpleNamespace(objects=[obj.tracked], frames=[SimpleNamespace(pixel_scale_arcsec=6.2)], stacked=False)


def test_gaia_failing_marks_the_check_skipped(engine_result, monkeypatch):
    def down(ra, dec, radius):
        raise catalogs.CatalogError("Gaia request failed: ConnectTimeout")

    _gaia(monkeypatch, down)
    _, known, notes, skipped = pipeline._cross_check(engine_result)
    assert skipped.startswith("Gaia unavailable (Gaia request failed: ConnectTimeout)")
    assert [k.classification for k in known] == [None]
    assert any("Gaia unavailable" in n for n in notes)


def test_gaia_known_down_is_not_asked(engine_result, monkeypatch):
    _gaia(monkeypatch, lambda *a: pytest.fail("Gaia was asked"))
    _, known, notes, skipped = pipeline._cross_check(engine_result, gaia_down="down since the patch began")
    assert skipped == "down since the patch began" and [k.classification for k in known] == [None]
    assert notes == ["Gaia unavailable (down since the patch began): objects were not checked against catalogues"]


def test_gaia_answering_is_a_checked_field(engine_result, monkeypatch):
    _gaia(monkeypatch, lambda *a: [])
    _, known, _, skipped = pipeline._cross_check(engine_result)
    assert skipped is None and known[0].classification is not None


def test_spherex_only_is_not_a_skipped_check(engine_result, monkeypatch):
    monkeypatch.setattr(pipeline, "parse_observatories", lambda: pipeline.SPHEREX_ONLY)
    _gaia(monkeypatch, lambda *a: pytest.fail("Gaia was asked"))
    _, known, _, skipped = pipeline._cross_check(engine_result)
    assert skipped is None and [k.classification for k in known] == [None]
