"""Audit the engine's movers: which are not really moving, and why (results/false_movers.json).

    uv run python tools/false_mover_audit.py                    # every field below that is cached
    uv run python tools/false_mover_audit.py --fields barnard,nep --out /tmp/fm.json

Runs the engine (single frames and per-pass stacks) on fields whose SPHEREx
cutouts are already on disk, never downloading any:

* "dev" fields: tools/fetch.py's trimmed cutouts of the test targets (.cache/dev);
* "survey" fields: fields of a finished survey patch, cropped from the survey's
  raw patch cutouts ($PLANETX_CACHE_DIR/../survey/cutouts) the way the survey does;
* "gaia" fields: the Gaia-validation tool's trimmed cutouts
  ($PLANETX_CACHE_DIR/gaia_validation/<name>/), when present.

Each mover is then checked against Gaia DR3 (from IRSA's mirror, cached as JSON
next to the cutouts) and against its own pixels by
`planetx.analysis.false_movers`. Besides the movers, `near_movers` lists tracks
seen in 3+ passes whose motion is >= 5 sigma but under one pixel: the engine's
1 px bar is the only thing keeping them from being movers, so a longer baseline
(a new data release) would turn any fake among them into a fake mover.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import numpy as np
import requests
from astropy import log
from astropy.coordinates import SkyCoord
from astropy.io import fits
from planetx import pipeline
from planetx.analysis import false_movers as fm
from planetx.classify.catalogs import CatalogObject
from planetx.config import settings
from planetx.engine import EngineResult, find_motion, frame_from_hdul
from planetx.engine.motion import DAYS_PER_YEAR

ROOT = Path(__file__).resolve().parents[1]
GATOR_URL = "https://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query"
# Gaia stars up to this fast can have moved into the field since 2016.0.
GAIA_REACH_ARCSEC = 15.0 * 11


@dataclass(frozen=True)
class FieldSpec:
    name: str
    ra: float
    dec: float
    source: str  # "dev" | "survey" | "gaia"
    size_deg: float = 0.1
    detectors: tuple[int, ...] = (1, 2, 3)
    per_pass: int = 6
    patch: tuple[int, int] | None = None  # (nside, ipix) for survey fields
    index: int = -1  # field index in the patch


def dev_fields() -> list[FieldSpec]:
    from targets import ENGINE_TARGETS_DEG

    # The configurations the engine's real-data tests use (tests/engine/test_engine_real.py).
    configs = {
        "barnard": ((1, 2, 3, 4, 5, 6), 6), "eps-indi-b": ((3, 4, 5), 6), "wise0855": ((5, 6), 6),
        "r-dra": ((1, 2, 3), 30), "nep": ((1, 2, 3), 4),
    }
    return [
        FieldSpec(name, *ENGINE_TARGETS_DEG[name], source="dev", detectors=dets, per_pass=pp)
        for name, (dets, pp) in configs.items()
    ]


def survey_fields(nside: int, ipix: int, every: int) -> list[FieldSpec]:
    from planetx.survey.motion import patch_fields

    return [
        FieldSpec(f"survey{ipix}-f{i:02d}", f.ra, f.dec, "survey", f.size_deg, patch=(nside, ipix), index=i)
        for i, f in enumerate(patch_fields(ipix, nside)) if i % every == 0
    ]


def gaia_validation_fields() -> list[FieldSpec]:
    root = settings.cache_dir / "gaia_validation"
    out = []
    for d in sorted(p for p in root.glob("*") if p.is_dir()):
        files = sorted(d.glob("*.fits"))
        if len(files) < 20:
            continue
        with fits.open(files[0]) as h:
            hd = h["IMAGE"].header
        # The validation fields are 0.2 deg requests; the centre is that of the cutouts.
        from astropy.wcs import WCS

        w = WCS(hd).celestial
        ny, nx = hd["NAXIS2"], hd["NAXIS1"]
        ra, dec = (float(v) for v in w.all_pix2world((nx - 1) / 2, (ny - 1) / 2, 0))
        out.append(FieldSpec(f"gv-{d.name}", ra, dec, "gaia", 0.2))
    return out


# --- frames (cache only) ---------------------------------------------------------

def load_dev(spec: FieldSpec) -> list:
    from fetch import fetch_cutout, find_visits, pick_visits

    coord = SkyCoord(spec.ra, spec.dec, unit="deg")
    visits = pick_visits(find_visits(coord, spec.detectors), spec.per_pass)
    frames = []
    for v in visits:
        # Checked first so that fetch_cutout, which downloads what is missing, only ever reads the cache.
        if not _dev_cached_path(v, coord, spec.size_deg).exists():
            raise FileNotFoundError("cutouts not cached; run tools/devview.py on the target first")
        with fits.open(fetch_cutout(v, coord, spec.size_deg)) as h:
            frames.append(frame_from_hdul(h, v.obs_id))
    return frames


def _dev_cached_path(visit, coord, size_deg: float) -> Path:
    import hashlib
    import re

    import fetch

    key = hashlib.sha1(f"{visit.url}|{coord.ra.deg:.6f}|{coord.dec.deg:.6f}|{size_deg}".encode()).hexdigest()[:16]
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", visit.obs_id).lstrip(".")
    return fetch.CACHE / f"{safe_id}_D{visit.detector}_{key}.fits"


_survey_index: list | None = None


def _survey_exposures() -> list:
    """One raw patch cutout per (exposure, detector) in the survey's cache, largest first."""
    global _survey_index
    if _survey_index is not None:
        return _survey_index
    from planetx.spherex import cutouts

    best: dict = {}
    for p in sorted(glob.glob(str(settings.cache_dir.parent / "survey" / "cutouts" / "*" / "*.fits"))):
        with fits.open(p) as h:
            hd = h["IMAGE"].header
            v = SimpleNamespace(
                path=p, mjd=float(hd["MJD-AVG"]), detector=int(hd["DETECTOR"]), access_url=p,
                obs_id=str(h[0].header.get("OBSID", hd.get("OBSID", ""))),
                wcs=cutouts.cutout_wcs(hd), shape=(int(hd["NAXIS2"]), int(hd["NAXIS1"])),
            )
        k = (v.obs_id, v.detector)
        if k not in best or v.shape[0] > best[k].shape[0]:
            best[k] = v
    _survey_index = list(best.values())
    return _survey_index


def load_survey(spec: FieldSpec) -> list:
    from planetx.spherex import cutouts

    req = cutouts.cutout_request(spec.ra, spec.dec, spec.size_deg)
    covering = [v for v in _survey_exposures() if cutouts.crop_box(v.wcs, v.shape, *req) is not None]
    frames = []
    # The survey worker evicts old patch cutouts as it goes: sample only those still there.
    covering = [v for v in covering if Path(v.path).exists()]
    for v in pipeline.pick_visits(covering):
        try:
            with fits.open(v.path) as h:
                crop = cutouts.crop_cutout(h, *req, wcs=v.wcs)
        except FileNotFoundError:
            continue
        if crop is not None:
            frames.append(frame_from_hdul(crop, obs_id=v.obs_id))
    return frames


def load_gaia_validation(spec: FieldSpec) -> list:
    d = settings.cache_dir / "gaia_validation" / spec.name.removeprefix("gv-")
    vs = []
    for p in sorted(d.glob("*.fits")):
        with fits.open(p) as h:
            hd = h["IMAGE"].header
            vs.append(SimpleNamespace(path=p, mjd=float(hd["MJD-AVG"]), detector=int(hd["DETECTOR"]),
                                      access_url=str(p), obs_id=str(h[0].header.get("OBSID", ""))))
    frames = []
    for v in pipeline.pick_visits(vs):
        with fits.open(v.path) as h:
            frames.append(frame_from_hdul(h, v.obs_id))
    return frames


def load_frames(spec: FieldSpec) -> list:
    return {"dev": load_dev, "survey": load_survey, "gaia": load_gaia_validation}[spec.source](spec)


# --- Gaia DR3 (IRSA mirror, cached) -----------------------------------------------

def gaia_stars(spec: FieldSpec) -> list[CatalogObject]:
    radius = spec.size_deg * 0.75 * 3600 + GAIA_REACH_ARCSEC
    cache = settings.cache_dir / "false_mover_audit" / f"gaia_{spec.ra:.5f}_{spec.dec:+.5f}_{radius:.0f}.json"
    if cache.exists():
        return [CatalogObject(**row) for row in json.loads(cache.read_text())]
    params = {
        "catalog": fm.GATOR_GAIA_TABLE, "spatial": "cone", "objstr": f"{spec.ra:.6f} {spec.dec:+.6f}",
        "radius": f"{radius:.1f}", "radunits": "arcsec", "outfmt": "1", "selcols": fm.GATOR_GAIA_COLUMNS,
        "outrows": "100000",
    }
    for attempt in range(4):
        resp = requests.get(GATOR_URL, params=params, timeout=180)
        if resp.status_code < 500:
            break
        time.sleep(5 * (attempt + 1))  # IRSA answers 502/503 in bursts: retry only those
    resp.raise_for_status()
    stars = fm.parse_gator_gaia(resp.text)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([s.__dict__ for s in stars]))
    return stars


# --- audit ---------------------------------------------------------------------------

NOTES = (
    (
        "Truth from Gaia DR3 (IRSA mirror): the Gaia star (G <= 19.5) under every pass position, moved to "
        "that pass's date. static: under 0.05\"/yr; real_mover: over 0.5\"/yr and 3x the SPHEREx error."
    ),
    (
        "cause is set for every mover Gaia shows static, and for an unsettled one only when its pixels show a "
        "problem (saturated halo, frame edge, flagged pixels, wavelength-dependent centroid, hopping between "
        "Gaia stars)."
    ),
    (
        "near_movers: tracks in 3+ passes at >= 5 sigma but under the 1 px bar; not movers today, but a "
        "longer baseline would make the fake ones fake movers."
    ),
    (
        "fast_stars: every visible Gaia star (G <= 19.5) moving > 0.5\"/yr by at least a pixel over the run, and "
        "how the engine tracked it (mover, split into several tracks, one track not flagged, unlinked, missed)."
    ),
)

def near_movers(result: EngineResult) -> list:
    """Non-movers seen in 3+ passes at >= 5 sigma whose shift is under one pixel."""
    pix = float(np.median([f.pixel_scale_arcsec for f in result.frames]))
    out = []
    for o in result.objects:
        span = (o.sources[-1].mjd - o.sources[0].mjd) / DAYS_PER_YEAR
        if not o.is_mover and len(o.sources) >= 3 and o.pm_significance >= fm.MOVER_SIGMA and o.pm_total * span < pix:
            out.append(o)
    return out


class Audit:
    """Everything one engine found over the fields, in the results file's shape."""

    def __init__(self, engine=find_motion):
        self.engine = engine
        self.movers, self.near, self.fast, self.fields = [], [], [], []

    def add(self, spec: FieldSpec, stack: bool, frames: list, stars: list[CatalogObject], t0: float) -> None:
        result = self.engine(frames, stack=stack)
        kw = {"field_ra": spec.ra, "field_dec": spec.dec, "field_name": spec.name}
        movers = fm.audit_result(result, stars, **kw)
        near = fm.audit_objects(near_movers(result), result, stars, **kw)
        fast = fm.fast_star_recovery(result, stars, field_name=spec.name)
        self.movers += movers
        self.near += near
        self.fast += fast
        mode = "stack" if stack else "single"
        self.fields.append({
            "name": spec.name, "ra": spec.ra, "dec": spec.dec, "size_deg": spec.size_deg, "source": spec.source,
            "mode": mode, "n_frames": len(result.input_frames), "n_passes": len(result.passes),
            "n_objects": len(result.objects), "n_movers": len(movers), "n_near_movers": len(near),
            "n_gaia_stars": len(stars),
        })
        print(f"{spec.name} {mode}: {len(result.input_frames)} frames, {len(result.passes)} passes, "
              f"{len(result.objects)} objects, {len(movers)} movers ({sum(m.cause is not None for m in movers)} "
              f"fake), {len(near)} near movers ({sum(m.cause is not None for m in near)} fake), fast Gaia stars "
              f"{[f['status'] for f in fast]} [{time.time() - t0:.0f}s]", flush=True)
        for m in movers + near:
            print(f"    {m.truth:10s} {m.cause!s:20s} {m.pm_total:5.2f}\"/yr {m.significance:5.1f}sig  {m.evidence}")

    def summaries(self) -> dict:
        return {
            "summary": fm.summarize(self.movers),
            "near_summary": fm.summarize(self.near),
            "fast_summary": {k: sum(f["status"] == k for f in self.fast)
                             for k in ("mover", "split", "not_flagged", "unlinked", "missed")},
        }


def load_engine(path: str):
    """`find_motion` of an engine package copied to directory `path` (an older revision, for before/after counts).

    The engine package only uses relative imports, so a copy loads under its own
    name next to the current one, e.g. from `git archive <rev> backend/planetx/engine`.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "baseline_engine", Path(path) / "__init__.py", submodule_search_locations=[path]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["baseline_engine"] = module
    spec.loader.exec_module(module)
    return module.find_motion


def public_command(argv: list[str]) -> str:
    """The command line as the results file shows it: local paths (a home directory) left out."""
    import shlex

    out, hide = [], {"--baseline-engine": "DIR", "--out": None}
    for k, arg in enumerate(argv):
        flag = argv[k - 1] if k else ""
        if flag in hide:
            out.append(hide[flag] or Path(arg).name)
        else:
            out.append(arg)
    return "uv run python tools/false_mover_audit.py " + shlex.join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fields", help="comma-separated field names (default: every cached field)")
    ap.add_argument("--sources", default="dev,survey,gaia", help="field sources to use (default: dev,survey,gaia)")
    ap.add_argument("--survey-patch", default="64:11605", help="nside:ipix of a finished survey patch")
    ap.add_argument("--survey-every", type=int, default=4, help="audit every n-th field of the patch (default 4)")
    ap.add_argument("--baseline-engine", metavar="DIR",
                    help="also run the engine package in DIR on the same frames and record its counts as `baseline`")
    ap.add_argument("--baseline-label", default="", help="what the baseline engine is (e.g. a git revision)")
    ap.add_argument("--out", default=str(ROOT / "results" / "false_movers.json"))
    args = ap.parse_args()
    log.setLevel("ERROR")
    warnings.simplefilter("ignore")

    nside, ipix = (int(x) for x in args.survey_patch.split(":"))
    sources = set(args.sources.split(","))
    specs = (
        (dev_fields() if "dev" in sources else [])
        + (survey_fields(nside, ipix, args.survey_every) if "survey" in sources else [])
        + (gaia_validation_fields() if "gaia" in sources else [])
    )
    if args.fields:
        want = set(args.fields.split(","))
        specs = [s for s in specs if s.name in want]
    audit = Audit()
    baseline = Audit(load_engine(args.baseline_engine)) if args.baseline_engine else None
    notes: list[str] = []
    for spec in specs:
        t0 = time.time()
        try:
            frames = load_frames(spec)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            # The note goes into a published file: the exception type, not its local paths.
            notes.append(f"{spec.name}: skipped ({type(exc).__name__}: its cached cutouts are incomplete)")
            print(f"{spec.name}: skipped ({exc})", flush=True)
            continue
        if len(frames) < 10:
            notes.append(f"{spec.name}: skipped (only {len(frames)} cached frames)")
            continue
        try:
            stars = gaia_stars(spec)
        except (requests.RequestException, ValueError) as exc:
            notes.append(f"{spec.name}: skipped, Gaia unavailable ({type(exc).__name__})")
            print(f"{spec.name}: Gaia unavailable: {exc}", flush=True)
            continue
        for stack in (False, True):
            audit.add(spec, stack, frames, stars, t0)
            if baseline is not None:
                print("  baseline:", end=" ")
                baseline.add(spec, stack, frames, stars, t0)

    out = {
        "name": "false_movers", "version": 1,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": public_command(sys.argv[1:]),
        "notes": [*NOTES, *notes],
        "fields": audit.fields,
        "movers": [m.to_json() for m in audit.movers],
        "near_movers": [m.to_json() for m in audit.near],
        "fast_stars": fm.clean_json(audit.fast),
        **audit.summaries(),
    }
    if baseline is not None:
        out["baseline"] = {
            "engine": args.baseline_label or Path(args.baseline_engine).name, **baseline.summaries(),
            "movers": [m.to_json() for m in baseline.movers],
            "near_movers": [m.to_json() for m in baseline.near],
            "fast_stars": fm.clean_json(baseline.fast),
        }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, allow_nan=False) + "\n")
    print(f"wrote {args.out}: {out['summary']} near: {out['near_summary']} fast: {out['fast_summary']}")
    if baseline is not None:
        print(f"baseline: {out['baseline']['summary']} near: {out['baseline']['near_summary']} "
              f"fast: {out['baseline']['fast_summary']}")


if __name__ == "__main__":
    main()
