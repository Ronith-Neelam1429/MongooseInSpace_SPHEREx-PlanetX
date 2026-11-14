"""Fixtures for the live API tests: a TestClient with a temp cutout cache."""
import pytest
from fastapi.testclient import TestClient

from planetx.spherex import cutouts
from planetx.api.app import app
from planetx.config import Settings



@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """TestClient whose cutout cache lives in a temp dir (no FITS left in the repo)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cutouts, "settings", Settings(cache_dir=tmp_path_factory.mktemp("cache")))
        yield TestClient(app)
