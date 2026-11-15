"""The fast clipped median used for local backgrounds equals astropy's, bit for bit."""
import numpy as np
import pytest
from astropy.stats import sigma_clipped_stats

from planetx.engine.detect import _clipped_median


@pytest.mark.parametrize("dtype", [np.float64, np.float32])
def test_clipped_median_matches_astropy(dtype):
    rng = np.random.default_rng(7)
    for trial in range(3000):
        n = int(rng.integers(8, 120))
        vals = rng.normal(0, 1, n)
        if trial % 3 == 0:  # outliers: a neighbour's wing in the annulus
            vals[: max(1, n // 6)] += rng.uniform(5, 50, max(1, n // 6))
        if trial % 5 == 0:  # ties, as quantised data has
            vals = np.round(vals, 1)
        vals = vals.astype(dtype)
        _, expected, _ = sigma_clipped_stats(vals, sigma=2.5, maxiters=5)
        got = _clipped_median(vals, sigma=2.5, maxiters=5)
        assert got.tobytes() == np.asarray(expected, dtype=got.dtype).tobytes(), (trial, vals)


def test_clipped_median_constant_and_iteration_cap():
    assert _clipped_median(np.full(10, 3.0), sigma=2.5, maxiters=5) == 3.0
    # A ladder that keeps clipping: the cap stops it where astropy stops.
    vals = np.concatenate([np.zeros(20), 10.0 ** np.arange(1, 12)])
    for maxiters in (1, 2, 3, 5):
        _, expected, _ = sigma_clipped_stats(vals, sigma=2.5, maxiters=maxiters)
        assert _clipped_median(vals, sigma=2.5, maxiters=maxiters) == expected
