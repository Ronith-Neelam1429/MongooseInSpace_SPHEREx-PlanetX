"""Measure what the motion engine can find, by planting fake movers in real SPHEREx frames.

Uses the cutouts cached by tools/fetch.py (offline). For each field, batches of fake
point sources (proper motion x brightness x direction, with the field's real PSF and
dates, see `planetx.analysis.injection`) are added to the real frames, the engine runs
on single frames and on per-pass stacks, and each injection is matched to the objects
that come back. Static injections (proper motion 0) give the rate of false movers.
Writes results/injection_recovery.json (contract: docs/RESULTS.md).

    uv run python tools/injection_recovery.py --cache-dir /path/to/.cache/dev          # full run
    uv run python tools/injection_recovery.py --cache-dir ... --quick --fields nep       # smoke test

The dev cutout cache has no EPSF table (trimmed); the PSF library is built from full
cutouts under --epsf-from (the app cache, default next to --cache-dir).
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import pickle
import sys
import time
import warnings
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
PM_GRID = (0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 12.0, 16.0)  # arcsec/yr
STATIC_EXTRA = 3  # static injections get this many times the reps: false movers are rare, so count more of them
MAG_GRID = (12.0, 14.0, 15.0, 16.0, 17.0, 18.0)  # bin centres, AB; injected uniformly +-0.5
MODES = ("single", "stack")
# name -> (ra, dec) of the cutout centre; frames within TOL deg of it belong to the field.
FIELDS = {
    "nep": (270.0, 66.56),
    "r-dra": (248.1675, 66.7555),
    "eps-indi-b": (331.10, -56.80),
    "barnard": (269.45, 4.77),
}
TOL_DEG = 0.004
BATCH = 4  # injections per engine run, spread over the field
MAX_PM_SEARCHED = 15.0  # find_motion's default max_pm_arcsec_yr: the engine cannot link faster


# --- field and PSF caches ---------------------------------------------------------------


def load_field_frames(cache_dir: Path, name: str):
    """Frames of one field (pickled after the first, slow, load) with detector pixel of the cutout centre."""
    from astropy.io import fits

    from planetx.analysis.injection import detector_xy
    from planetx.engine import frame_from_hdul

    pkl = cache_dir / f"injection_field_{name}.pkl"
    if pkl.exists():  # our own cache, written below; never a downloaded file
        return pickle.loads(pkl.read_bytes())
    ra0, dec0 = FIELDS[name]
    frames = []
    for path in sorted(glob.glob(str(cache_dir / "*.fits"))):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with fits.open(path) as hdul:
                f = frame_from_hdul(hdul, obs_id=Path(path).stem)
                ny, nx = f.data.shape
                cra, cdec = f.wcs.all_pix2world((nx - 1) / 2, (ny - 1) / 2, 0)
                if abs(cra - ra0) * math.cos(math.radians(dec0)) > TOL_DEG or abs(cdec - dec0) > TOL_DEG:
                    continue
                f.meta["detector_xy"] = detector_xy(hdul["IMAGE"].header, (nx - 1) / 2, (ny - 1) / 2)
        frames.append(f)
    if not frames:
        raise SystemExit(f"no cached frames for field {name} in {cache_dir}")
    tmp = pkl.with_suffix(".tmp")
    tmp.write_bytes(pickle.dumps(frames))
    tmp.replace(pkl)
    return frames


def epsf_library(cache_dir: Path, sources: list[Path]):
    from astropy.io import fits

    from planetx.analysis.injection import EpsfLibrary

    path = cache_dir / "injection_epsf_library.npz"
    if path.exists():
        return EpsfLibrary.load(path)
    lib = EpsfLibrary({})
    for root in sources:
        for p in glob.glob(str(root / "**" / "*.fits"), recursive=True):
            with fits.open(p) as hdul:
                lib.add_hdul(hdul)
            if len(lib.detectors) == 6:
                break
        if len(lib.detectors) == 6:
            break
    if len(lib.detectors) < 6:
        print(f"warning: EPSF tables only for detectors {lib.detectors}; the rest use a Gaussian", file=sys.stderr)
    lib.save(path)
    return lib


# --- the work ---------------------------------------------------------------------------

_state: dict = {}


def _field_state(cache_dir: str, name: str):
    from planetx.analysis.injection import SiteFinder

    if name not in _state:
        frames = load_field_frames(Path(cache_dir), name)
        lib = epsf_library(Path(cache_dir), [])
        _state[name] = (frames, lib, SiteFinder(frames))
    return _state[name]


def _psf_for(lib):
    from planetx.analysis.injection import Psf

    def psf_for(frame, x, y):
        if frame.detector in lib.tables:
            return lib.psf_at(frame.detector, *frame.meta["detector_xy"])
        return Psf.gaussian(frame.psf_fwhm_arcsec, frame.pixel_scale_arcsec)

    return psf_for


def object_verdicts(objects):
    """The motion verdict (planetx.classify.reliability) of each engine object, SPHEREx alone.

    No archive or catalogue is consulted: a planted source has no counterpart in any of them, so
    this is also what the full pipeline would have to go on.
    """
    from planetx.classify.check import Known
    from planetx.classify.reliability import bright_star_distances, object_verdict, split_partners
    from planetx.observatories import refine_objects

    fused = refine_objects(objects, observatories="spherex").fused
    bright, partners = bright_star_distances(objects), split_partners(objects)
    out = []
    for o, fm, b, p in zip(objects, fused, bright, partners):
        known = Known(kind="mover" if o.is_mover else "static")
        found = type("Found", (), {"tracked": o, "fused": fm, "known": known})()
        out.append(object_verdict(found, bright_star_arcsec=b, split_partner=p))
    return out


def run_batch(task: dict) -> list[dict]:
    """Plant one batch, run the engine in each mode, score each injection."""
    from planetx.analysis.injection import inject, match_injections
    from planetx.engine import find_motion

    name = task["field"]
    frames, lib, finder = _field_state(task["cache_dir"], name)
    rng = np.random.default_rng(np.random.SeedSequence(task["seed"]))
    injections, specs = [], []
    for pm, mag_centre in task["cells"]:
        pa = float(rng.uniform(0, 360))
        mag = float(mag_centre + rng.uniform(-0.5, 0.5))
        inj = finder.sample(rng, pm * math.sin(math.radians(pa)), pm * math.cos(math.radians(pa)), mag, taken=injections)
        if inj is None:
            specs.append({"pm": pm, "mag_centre": mag_centre, "pa": pa, "mag": mag, "placed": False})
            continue
        injections.append(inj)
        specs.append({"pm": pm, "mag_centre": mag_centre, "pa": pa, "mag": mag, "placed": True})
    planted = inject(frames, injections, _psf_for(lib))
    rows = []
    for mode in task["modes"]:
        t0 = time.time()
        error, verdicts = None, None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = find_motion(planted, stack=(mode == "stack"))
            matches = match_injections(result.objects, injections)
            verdicts = object_verdicts(result.objects) if task.get("verdicts") else None
        except Exception as exc:  # a failed run counts as a miss, and is reported
            error, matches = f"{type(exc).__name__}: {exc}", [None] * len(injections)
        k = 0
        for spec in specs:
            row = {"field": name, "mode": mode, "batch": task["seed"], **spec, "error": error, "seconds": time.time() - t0}
            if spec["placed"]:
                inj, m = injections[k], matches[k]
                k += 1
                row["recovered"] = bool(m and m.recovered)
                row["flagged_mover"] = bool(m and m.flagged_mover)
                if m and m.recovered:
                    pull = m.pull(inj)
                    row.update(
                        sep_arcsec=m.separation_arcsec, pm_meas_e=m.obj.pm_ra_cosdec, pm_meas_n=m.obj.pm_dec,
                        pm_err=m.obj.pm_err, n_sources=len(m.obj.sources), pm_true_e=inj.pm_ra_cosdec,
                        pm_true_n=inj.pm_dec, pull_e=pull[0] if pull else None, pull_n=pull[1] if pull else None,
                        significance=m.obj.pm_significance, link=m.obj.link,
                    )
                    if verdicts is not None:
                        v = verdicts[result.objects.index(m.obj)]
                        row.update(verdict=v.verdict, verdict_cause=v.cause, verdict_certainty=v.certainty)
            rows.append(row)
    return rows


# --- summaries --------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval for k of n (defined, and non-degenerate, at k = 0 and k = n)."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def crossing(xs, fs, level: float):
    """Smallest x where the curve fs(xs), read left to right, first reaches `level`, by linear
    interpolation in x; None if it never does. Curves are non-decreasing in spirit; noise is not smoothed."""
    for i, (x, f) in enumerate(zip(xs, fs)):
        if f >= level:
            if i == 0:
                return float(x)
            x0, f0 = xs[i - 1], fs[i - 1]
            return float(x0 + (x - x0) * (level - f0) / (f - f0)) if f > f0 else float(x)
    return None


def grid_rows(rows: list[dict], by_field: bool = False) -> list[dict]:
    cells: dict = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        if not r["placed"]:
            continue
        key = (r["mode"], r["field"] if by_field else "all", r["pm"], r["mag_centre"])
        c = cells[key]
        c[0] += 1
        c[1] += r["recovered"]
        c[2] += r["flagged_mover"]
    out = []
    for (mode, field, pm, mc), (n, rec, flag) in sorted(cells.items()):
        lo, hi = wilson(flag, n)
        out.append({
            "mode": mode, "field": field, "pm_arcsec_yr": pm, "mag_bin": f"{mc - 0.5:g}-{mc + 0.5:g}", "mag_ab": mc,
            "n_injected": n, "n_recovered": rec, "n_flagged_mover": flag, "recovered_fraction": rec / n,
            "flagged_fraction": flag / n, "flagged_ci95": [lo, hi],
        })
    return out


def summarize(grid: list[dict], ref_mag: float, ref_pm: tuple[float, float], field: str = "all") -> dict:
    """pm_50 / pm_90: slowest speed at which 50% / 90% of fake movers brighter than `ref_mag` are flagged as
    movers; mag_50: faintest magnitude at which half of those with speed in `ref_pm` (min, max) are."""
    out = {}
    for mode in MODES:
        g = [c for c in grid if c["mode"] == mode and c["field"] == field]
        pms = sorted({c["pm_arcsec_yr"] for c in g if c["pm_arcsec_yr"] > 0})
        fr = []
        for pm in pms:
            cs = [c for c in g if c["pm_arcsec_yr"] == pm and c["mag_ab"] <= ref_mag]
            n = sum(c["n_injected"] for c in cs)
            fr.append(sum(c["n_flagged_mover"] for c in cs) / n if n else 0.0)
        mags = sorted({c["mag_ab"] for c in g})
        mf = []
        for mc in mags:
            cs = [c for c in g if c["mag_ab"] == mc and ref_pm[0] <= c["pm_arcsec_yr"] <= ref_pm[1]]
            n = sum(c["n_injected"] for c in cs)
            mf.append(sum(c["n_flagged_mover"] for c in cs) / n if n else 0.0)
        # mag_50: walk from bright to faint and stop at the first drop below 0.5
        mag_50 = None
        for i, (m, f) in enumerate(zip(mags, mf)):
            if f < 0.5:
                if i == 0:
                    mag_50 = float(m)
                else:
                    m0, f0 = mags[i - 1], mf[i - 1]
                    mag_50 = float(m0 + (m - m0) * (f0 - 0.5) / (f0 - f)) if f0 > f else float(m0)
                break
        else:
            mag_50 = float(mags[-1]) if mags else None
        out[mode] = {
            "pm_50": crossing(pms, fr, 0.5), "pm_90": crossing(pms, fr, 0.9), "mag_50": mag_50,
            "ref_mag_brighter_than": ref_mag, "ref_pm_range": list(ref_pm),
            "completeness_vs_pm_at_ref_mag": dict(zip(map(str, pms), fr)),
            "completeness_vs_mag_at_ref_pm": dict(zip(map(str, mags), mf)),
        }
    return out


def false_positive_summary(rows: list[dict]) -> dict:
    out = {}
    for mode in MODES:
        st = [r for r in rows if r["mode"] == mode and r["placed"] and r["pm"] == 0]
        rec = [r for r in st if r["recovered"]]
        flag = sum(r["flagged_mover"] for r in st)
        lo, hi = wilson(flag, len(st))
        out[mode] = {
            "n_static_injected": len(st), "n_static_recovered": len(rec), "n_flagged_mover": flag,
            "rate_per_injected": flag / len(st) if st else None, "rate_ci95": [lo, hi],
            "rate_per_recovered": flag / len(rec) if rec else None,
        }
    return out


def pull_summary(rows: list[dict], min_sigma: float = 0.0) -> dict:
    """Mean and std of (measured - injected) / reported error over recovered moving injections."""
    out = {}
    for mode in MODES:
        sel = [r for r in rows if r["mode"] == mode and r["placed"] and r["recovered"] and r["pm"] > 0 and r.get("pull_e") is not None]
        p = np.array([v for r in sel for v in (r["pull_e"], r["pull_n"])], float)
        # Robust spread too: a few mislinked tracks dominate the plain std.
        mad = float(1.4826 * np.median(np.abs(p - np.median(p)))) if len(p) else None
        out[mode] = {
            "n_components": int(len(p)), "mean": float(p.mean()) if len(p) else None,
            "std": float(p.std()) if len(p) else None, "robust_std": mad,
            "fraction_within_1sigma": float(np.mean(np.abs(p) <= 1)) if len(p) else None,
            "fraction_within_3sigma": float(np.mean(np.abs(p) <= 3)) if len(p) else None,
            "by_pm": {
                str(pm): {
                    "n": int(len(q)), "std": float(q.std()), "robust_std": float(1.4826 * np.median(np.abs(q - np.median(q)))),
                }
                for pm in sorted({r["pm"] for r in sel})
                for q in [np.array([v for r in sel if r["pm"] == pm for v in (r["pull_e"], r["pull_n"])], float)]
                if len(q) >= 4
            },
        }
    return out


# --- main -------------------------------------------------------------------------------


def make_tasks(cache_dir: Path, fields: list[str], reps: int, modes) -> list[dict]:
    tasks = []
    for fi, name in enumerate(fields):
        cells = [(pm, mc) for pm in PM_GRID for mc in MAG_GRID for _ in range(reps * (STATIC_EXTRA if pm == 0 else 1))]
        rng = np.random.default_rng(1000 + fi)
        rng.shuffle(cells)
        for bi in range(0, len(cells), BATCH):
            tasks.append({
                "field": name, "cache_dir": str(cache_dir), "modes": list(modes),
                "cells": cells[bi : bi + BATCH], "seed": [fi, bi],
            })
    return tasks


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=ROOT / ".cache" / "dev", help="tools/fetch.py cutout cache")
    ap.add_argument("--epsf-from", type=Path, nargs="*", default=None, help="dirs with full cutouts (default: ../planetx, ../survey next to --cache-dir)")
    ap.add_argument("--fields", nargs="*", default=list(FIELDS), choices=list(FIELDS))
    ap.add_argument("--rerun", nargs="*", default=None, choices=list(FIELDS), help="only recompute these fields; take the rest from --rows-in")
    ap.add_argument("--rows-in", type=Path, default=None, help="raw rows (from --dump-rows) of an earlier run, for --rerun")
    ap.add_argument("--static-verdicts", action="store_true", help="only static injections, single frames: record the motion verdict of each recovered one and add `static_verdicts` to the existing results file")
    ap.add_argument("--reps", type=int, default=6, help="injections per (field, speed, brightness) cell")
    ap.add_argument("--modes", nargs="*", default=list(MODES), choices=list(MODES))
    ap.add_argument("--workers", type=int, default=4, help="processes (the machine is shared: at most 4)")
    ap.add_argument("--ref-mag", type=float, default=15.5, help="pm_50/pm_90 use injections brighter than this")
    ap.add_argument("--ref-pm-min", type=float, default=6.0, help="mag_50 uses injections moving at least this fast...")
    ap.add_argument("--ref-pm-max", type=float, default=12.0, help="...and at most this fast (above ~15 the engine does not search)")
    ap.add_argument("--quick", action="store_true", help="smoke test: 1 rep, no output file unless --out is given")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--dump-rows", type=Path, default=None, help="also write every injection's raw outcome here (not tracked)")
    args = ap.parse_args()
    if args.workers > 4:
        ap.error("--workers: use at most 4 processes")
    reps = 1 if args.quick else args.reps
    out_path = args.out or (None if args.quick else ROOT / "results" / "injection_recovery.json")

    # Build shared caches once, before forking workers.
    sources = args.epsf_from if args.epsf_from is not None else [args.cache_dir.parent / "planetx", args.cache_dir.parent / "survey"]
    epsf_library(args.cache_dir, sources)
    field_info = []
    for name in args.fields:
        frames = load_field_frames(args.cache_dir, name)
        from planetx.engine import group_into_passes

        field_info.append({
            "name": name, "ra": FIELDS[name][0], "dec": FIELDS[name][1], "n_frames": len(frames),
            "n_passes": len(group_into_passes([f.mjd for f in frames], 60.0)),
            "detectors": sorted({f.detector for f in frames}),
            "mjd_min": min(f.mjd for f in frames), "mjd_max": max(f.mjd for f in frames),
            # The engine needs a mover to shift by more than one pixel over its baseline.
            "one_pixel_pm_arcsec_yr": frames[0].pixel_scale_arcsec / ((max(f.mjd for f in frames) - min(f.mjd for f in frames)) / 365.25),
        })
    if args.static_verdicts:
        sv_path = args.out or ROOT / "results" / "injection_recovery.json"
        tasks = [
            {**t, "cells": [c for c in t["cells"] if c[0] == 0.0], "modes": ["single"], "verdicts": True}
            for t in make_tasks(args.cache_dir, args.fields, reps, ["single"])
        ]
        tasks = [t for t in tasks if t["cells"]]
        print(f"{len(tasks)} static-verdict batches", flush=True)
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            sv_rows = [r for batch in pool.map(run_batch, tasks) for r in batch]
        rec = [r for r in sv_rows if r["placed"] and r["recovered"]]
        counts: dict = defaultdict(int)
        for r in rec:
            counts[r["verdict"]] += 1
        causes: dict = defaultdict(int)
        for r in rec:
            if r["verdict"] in ("fake_motion", "unsure") and r["verdict_cause"]:
                causes[r["verdict_cause"]] += 1
        sv = {
            "n_static_injected": sum(r["placed"] for r in sv_rows), "n_recovered": len(rec),
            "verdicts": dict(counts), "causes_of_fake_or_unsure": dict(causes),
            "fraction_fake_or_unsure": (counts["fake_motion"] + counts["unsure"]) / len(rec) if rec else None,
            "fraction_moving": counts["moving"] / len(rec) if rec else None,
            "note": "Static injections the engine recovered, judged by planetx.classify.reliability.object_verdict with SPHEREx evidence only (no archive, no catalogues).",
        }
        doc = json.loads(sv_path.read_text())
        doc["static_verdicts"] = sv
        sv_path.write_text(json.dumps(doc, indent=1, default=float) + "\n")
        print(json.dumps(sv, indent=1))
        return

    run_fields = args.rerun if args.rerun is not None else args.fields
    tasks = make_tasks(args.cache_dir, args.fields, reps, args.modes)
    tasks = [t for t in tasks if t["field"] in run_fields]  # seeds depend on the field's position, so they match a full run
    print(f"{len(tasks)} batches ({sum(len(t['cells']) for t in tasks)} injections) over {run_fields}", flush=True)

    rows: list[dict] = []
    if args.rerun is not None:
        if not args.rows_in:
            ap.error("--rerun needs --rows-in")
        rows = [r for r in json.loads(args.rows_in.read_text()) if r["field"] not in run_fields]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for i, batch_rows in enumerate(pool.map(run_batch, tasks), 1):
            rows += batch_rows
            print(f"  batch {i}/{len(tasks)}  {time.time() - t0:.0f}s", flush=True)
    errors = sorted({r["error"] for r in rows if r["error"]})

    grid = grid_rows(rows) + grid_rows(rows, by_field=True)
    pooled = [g for g in grid if g["field"] == "all"]
    summary = summarize(pooled, args.ref_mag, (args.ref_pm_min, args.ref_pm_max))
    fp = false_positive_summary(rows)
    result = {
        "name": "injection_recovery", "version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Paths are machine-specific: show the knobs that change the result, not where the cache lives.
        "command": "uv run python tools/injection_recovery.py"
        + f" --fields {' '.join(args.fields)} --reps {reps} --modes {' '.join(args.modes)}",
        "notes": [
            "Fake point sources are planted into real cached SPHEREx cutouts (0.1 degree) using each detector's real EPSF; the engine then runs on those frames.",
            "Magnitudes are AB, flat in f_nu, so the same in every frame whatever its wavelength; a bin's injections are uniform in +-0.5 mag.",
            "A mover counts as recovered when the engine returns an object within 12 arcsec of where it was; flagged_mover when that object is_mover. Completeness = flagged fraction.",
            "No noise is added: the frames are real and already noisy. Saturation of bright injections is not modelled.",
            f"The engine links motions only up to {MAX_PM_SEARCHED:g} arcsec/yr, so the 16 arcsec/yr row is beyond its search by construction.",
            "Directions are random per injection; the same injections are used in single-frame and stack modes.",
            "pm_50/pm_90 use injections brighter than ref_mag, mag_50 those with speed in ref_pm_range; see summary.by_mode and summary.by_field.",
            "The engine only calls a track a mover if it shifts by more than one pixel (6.15 arcsec) over its baseline, so slow injections are recovered and measured but not flagged: see fields[].one_pixel_pm_arcsec_yr.",
            "Static injections are repeated 3x more often than other speeds, to count false movers.",
        ] + ([f"{len(errors)} distinct engine errors, counted as misses: {errors[:3]}"] if errors else []),
        "fields": field_info,
        "grid": grid,
        "false_positives": {
            "n_fields": len(args.fields),
            "n_static_injected": fp[args.modes[0]]["n_static_injected"],
            "n_flagged_mover": fp[args.modes[0]]["n_flagged_mover"],
            "by_mode": fp,
        },
        "summary": {
            "pm_50": summary[args.modes[0]]["pm_50"], "pm_90": summary[args.modes[0]]["pm_90"],
            "mag_50": summary[args.modes[0]]["mag_50"], "mode": args.modes[0], "by_mode": summary,
            "by_field": {
                name: summarize(grid, args.ref_mag, (args.ref_pm_min, args.ref_pm_max), field=name) for name in args.fields
            },
        },
        "pm_pull": pull_summary(rows),
        "n_unplaced": sum(1 for r in rows if not r["placed"] and r["mode"] == args.modes[0]),
        "seconds": time.time() - t0,
    }
    text = json.dumps(result, indent=1, default=float)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n")
        print(f"wrote {out_path}")
    print(json.dumps({k: result[k] for k in ("false_positives", "summary", "pm_pull")}, indent=1, default=float))
    if args.dump_rows:
        args.dump_rows.write_text(json.dumps(rows, default=float))


if __name__ == "__main__":
    main()
