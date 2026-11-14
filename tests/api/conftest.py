"""Fixtures for the live API tests: Barnard's Star reference and a temp cutout cache."""
import pytest
from fastapi.testclient import TestClient

from planetx import cutouts
from planetx.api.app import app
from planetx.config import Settings

# Gaia DR3 4472832130942575872 (Barnard's Star), epoch J2016.0.
BARNARD_RA_DEG = 269.44850252
BARNARD_DEC_DEG = 4.73942005
BARNARD_PMRA_COSDEC = -801.551  # mas/yr
BARNARD_PMDEC = 10362.394  # mas/yr
BARNARD_DISTANCE_PC = 1.83


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """TestClient whose cutout cache lives in a temp dir (no FITS left in the repo)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cutouts, "settings", Settings(cache_dir=tmp_path_factory.mktemp("cache")))
        yield TestClient(app)
