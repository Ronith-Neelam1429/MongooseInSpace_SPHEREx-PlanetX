"""Re-derive the engine's position-error constants from static stars (dev tool).

How the detect.CENTROID_* / STACK_CENTROID_* and motion.PASS_FLOOR_ARCSEC
constants were fitted (2026-11-14). For each field, the engine runs on cached
cutouts (tools/fetch.py, offline once cached); every unsaturated static track
seen in >= 3 passes contributes its pass positions. The model

    per detection   sigma_d = hypot(a * FWHM / SNR, b)          (arcsec)
    per pass        sigma_p = hypot(1 / sqrt(sum 1/sigma_d^2), f)

(the 2026-11-14 fit also had 54 RW Aur frames from the app cache, not in the dev
cache) is fitted by restricted maximum likelihood (each track, each axis: a weighted
straight line in time) over a grid of (a, b, f). It prints the best values,
chi2/dof per error band and per field. Engine run-time calibration per field
(motion._error_scales) comes on top of these and is switched off here by
reading the detections' own SNRs, not the engine's scaled pass errors.

    uv run python tools/calibrate_errors.py              # single frames
    uv run python tools/calibrate_errors.py --stack      # per-pass stacks
"""
from __future__ import annotations

import itertools
import sys
import warnings
from pathlib import Path

import astropy.units as u
import numpy as np
from astropy import log
from astropy.coordinates import SkyCoord
from astropy.io import fits

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch import TARGETS, fetch_all, find_visits, pick_visits  # noqa: E402

from planetx.engine import find_motion, frame_from_hdul  # noqa: E402
from planetx.engine.motion import DAYS_PER_YEAR, SkyPlane  # noqa: E402

# Cutout centres as in tests/targets.py ENGINE_TARGETS_DEG (fetch's cache keys).
FIELDS = {
    "barnard": ((269.4462678797139, 4.768204454786656), (1, 2, 3, 4, 5, 6), 6),
    "eps-indi-b": ((331.09666878308445, -56.8006491764846), None, 6),
    "r-dra": ((248.16723307211967, 66.75502382621286), None, 30),
    "wise0855": ((133.75976524998046, -7.24218218007099), None, 6),
    "nep": ((270.0, 66.5607), (1, 2, 3), 4),
}


def load(name: str):
    (ra, dec), dets, per_pass = FIELDS[name]
    coord = SkyCoord(ra * u.deg, dec * u.deg)
    dets = dets or TARGETS[name][2]
    frames = []
    for v, path in fetch_all(pick_visits(find_visits(coord, dets), per_pass), coord, 0.1):
        with fits.open(path) as h:
            frames.append(frame_from_hdul(h, v.obs_id))
    return frames


def static_tracks(result) -> list[dict]:
    fwhm = float(np.nanmedian([f.psf_fwhm_arcsec for f in result.frames]))
    out = []
    for o in result.objects:
        if o.is_mover or o.link != "static" or len(o.sources) < 3:
            continue
        if any(d.saturated for s in o.sources for d in s.detections):
            continue
        plane = SkyPlane(o.ra, o.dec)
        x, y = plane.offsets(np.array([s.ra for s in o.sources]), np.array([s.dec for s in o.sources]))
        out.append(dict(
            x=x, y=y, t=np.array([s.mjd for s in o.sources]) / DAYS_PER_YEAR, fwhm=fwhm,
            snrs=[np.array([d.snr for d in s.detections]) for s in o.sources],
        ))
    return out


def reml(v, t, sig) -> tuple[float, float]:
    """(negative restricted log-likelihood, chi2) of a weighted line fit."""
    w = 1 / sig**2
    X = np.column_stack([np.ones_like(t), t - t.mean()])
    XtWX = X.T @ (X * w[:, None])
    r = v - X @ np.linalg.solve(XtWX, X.T @ (w * v))
    chi2 = float((r**2 * w).sum())
    return 0.5 * (np.log(sig**2).sum() + np.linalg.slogdet(XtWX)[1] + chi2), chi2


def pass_sigmas(tr, a, b, f):
    per_pass = [1 / np.sqrt((1 / np.hypot(a * tr["fwhm"] / snr, b) ** 2).sum()) for snr in tr["snrs"]]
    return np.hypot(np.array(per_pass), f)


def evaluate(tracks, a, b, f):
    nll, rows = 0.0, []
    for tr in tracks:
        sig = pass_sigmas(tr, a, b, f)
        c = 0.0
        for v in (tr["x"], tr["y"]):
            n, cc = reml(v, tr["t"], sig)
            nll += n
            c += cc
        rows.append((np.median(sig), c, 2 * (len(sig) - 2)))
    return nll, np.array(rows)


def main() -> None:
    log.setLevel("WARNING")
    warnings.simplefilter("ignore")
    stack = "--stack" in sys.argv
    by_field = {}
    for name in FIELDS:
        by_field[name] = static_tracks(find_motion(load(name), stack=stack))
        print(f"{name}: {len(by_field[name])} static tracks", flush=True)
    tracks = [t for ts in by_field.values() for t in ts]
    grid = itertools.product(np.arange(0.2, 2.01, 0.1), np.arange(0.0, 1.21, 0.1), np.arange(0.0, 0.41, 0.04))
    _, a, b, f = min((evaluate(tracks, a, b, f)[0], a, b, f) for a, b, f in grid)
    print(f"best: a={a:.2f} (x FWHM/SNR)  b={b:.2f}\" per detection  f={f:.2f}\" per pass")
    _, rows = evaluate(tracks, a, b, f)
    print(f"all: chi2/dof {rows[:, 1].sum() / rows[:, 2].sum():.2f}")
    for lo, hi in [(0, 0.15), (0.15, 0.3), (0.3, 0.6), (0.6, 99)]:
        m = (rows[:, 0] >= lo) & (rows[:, 0] < hi)
        if m.any():
            print(f"  pass sigma [{lo}, {hi})\": {m.sum()} tracks, chi2/dof {rows[m, 1].sum() / rows[m, 2].sum():.2f}")
    for name, ts in by_field.items():
        if ts:
            _, rows = evaluate(ts, a, b, f)
            print(f"  {name}: chi2/dof {rows[:, 1].sum() / rows[:, 2].sum():.2f}")


if __name__ == "__main__":
    main()
