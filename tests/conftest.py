"""Shared test fixtures: a temp cutout cache, and no real SPHEREx archive calls in offline tests.

Tests marked ``network`` (the live ones) are left alone so they can reach IRSA.
Helpers shared across folders live next to this file (``synthetic.py``,
``targets.py``) and are imported by bare name, e.g. ``from synthetic import make_mef``.
"""
import pytest

from planetx.config import Settings
from planetx.spherex import archive, cutouts


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the cutout cache at a temp dir."""
    monkeypatch.setattr(cutouts, "settings", Settings(cache_dir=tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _offline_archive(request, monkeypatch):
    """Offline tests: no retry sleeps and no real HTTP (tests install their own fake session)."""
    if request.node.get_closest_marker("network"):
        yield
        return
    monkeypatch.setattr(archive, "RETRY_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(archive, "_session", None)
    archive._sia_cache.clear()
    yield
    archive._sia_cache.clear()
