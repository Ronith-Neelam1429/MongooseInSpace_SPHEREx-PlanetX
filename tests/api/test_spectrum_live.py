"""Live check of the spectrum run against IRSA (network): a plain star agrees with 2MASS.

Downloads 2 exposures per detector (~12 x 4 MB) into a temp cache.
"""
import math

import pytest
from planetx.api import spectrum

# 2MASS J08533892-0714380, a field star near WISE 0855: J 11.417, H 11.151, Ks 11.117.
STAR = (133.912168, -7.243894)
TWOMASS = ((1.235, 1594e3 * 10 ** (-11.417 / 2.5)), (1.662, 1024e3 * 10 ** (-11.151 / 2.5)),
           (2.159, 666.7e3 * 10 ** (-11.117 / 2.5)))  # (um, mJy)


def _twomass_at(lam: float) -> float:
    """2MASS fluxes interpolated as power laws (log-log), mJy."""
    (l1, f1), (l2, f2) = (TWOMASS[0], TWOMASS[1]) if lam < TWOMASS[1][0] else (TWOMASS[1], TWOMASS[2])
    t = math.log(lam / l1) / math.log(l2 / l1)
    return math.exp((1 - t) * math.log(f1) + t * math.log(f2))


@pytest.mark.network
def test_plain_star_matches_2mass_and_shows_no_absorption(cache_dir, monkeypatch):
    monkeypatch.setattr(spectrum, "NEW_PER_DETECTOR", 2)
    job = spectrum.Job(*STAR, 0.0, 0.0, 2000.0)
    result = spectrum.build_spectrum(job, should_stop=lambda: False)
    assert result.counts.ok >= 6 and not result.saturated
    near_ir = [p for p in result.points if 1.1 <= p.wavelength_um <= 2.3]
    assert near_ir, "no exposure in the 2MASS range"
    for p in near_ir:  # measured 2026-11-14: within ~5% of 2MASS
        assert p.flux_mjy == pytest.approx(_twomass_at(p.wavelength_um), rel=0.15)
    assert not any(f.detected for f in result.features)
    assert result.suggestion in ("none", "unknown")
