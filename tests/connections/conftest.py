"""Shared fixtures for the external data-service connection tests.

Barnard's Star is the reference target: its position and huge proper motion
are well known, so every service can be checked against a known answer.
"""
import os

import astropy.units as u
import certifi
import pytest
from astropy.coordinates import SkyCoord
from astroquery import cache_conf

# python.org CPython builds ship without a CA bundle, so astroquery.gaia fails
# TLS verification. Point at certifi's bundle (verification stays on). The app
# needs the same at startup.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

# Gaia DR3 4472832130942575872 (Barnard's Star), epoch J2016.0.
BARNARD_RA_DEG = 269.44850252
BARNARD_DEC_DEG = 4.73942005
BARNARD_PM_TOTAL_MAS_YR = 10393.0  # approx. total proper motion


@pytest.fixture(autouse=True, scope="session")
def _no_astroquery_cache():
    """Connection tests must hit the live service: astroquery otherwise
    replays cached responses (including cached 404s) for up to a week."""
    with cache_conf.set_temp("cache_active", False):
        yield


@pytest.fixture(scope="session")
def barnard() -> SkyCoord:
    return SkyCoord(BARNARD_RA_DEG * u.deg, BARNARD_DEC_DEG * u.deg, frame="icrs")
