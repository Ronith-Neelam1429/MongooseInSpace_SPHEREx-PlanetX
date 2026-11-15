import pytest

from planetx.classify import check


@pytest.fixture(autouse=True)
def _fresh_horizons_backoff():
    """A Horizons failure in one test must not make the next one skip Horizons."""
    check.horizons_backoff.reset()
    yield
    check.horizons_backoff.reset()
