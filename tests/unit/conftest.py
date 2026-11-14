"""Offline test fixtures: temp cutout cache and a network-free IRSA module."""
import pytest

from planetx import cutouts, irsa
from planetx.config import Settings


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the cutout cache at a temp dir."""
    monkeypatch.setattr(cutouts, "settings", Settings(cache_dir=tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _offline_irsa(monkeypatch):
    """No retry sleeps and no real HTTP: tests install their own fake session."""
    monkeypatch.setattr(irsa, "RETRY_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(irsa, "_session", None)
    irsa._sia_cache.clear()
    yield
    irsa._sia_cache.clear()
