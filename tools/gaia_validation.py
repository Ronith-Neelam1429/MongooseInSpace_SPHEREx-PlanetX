"""Measure the motion engine against Gaia DR3 on real SPHEREx fields: results/gaia_validation.json.

    uv run python tools/gaia_validation.py                  # every field in FIELDS
    uv run python tools/gaia_validation.py nep barnard      # some of them
    uv run python tools/gaia_validation.py --list

Each field goes through the website's and the survey's own path. It picks visits with
`pipeline.pick_visits` (detectors 1-3, 6 frames per pass and detector), requests cutouts
with `cutouts.cutout_request(ra, dec, 0.2)`, and runs `pipeline.analyse_frames` with
PLANETX_OBSERVATORIES=spherex, so the motions are SPHEREx-only (no archival refit, no
catalogue check). Then it fetches Gaia DR3 once per field and compares
(planetx.analysis.gaia_truth).

Cutouts already in the shared cache (PLANETX_CACHE_DIR/cutouts) are read from there.
New ones are trimmed to the HDUs the engine reads (about 0.3 MB instead of 4 MB) and
kept in PLANETX_CACHE_DIR/gaia_validation/<field>/, outside the cache's eviction, so
this tool never evicts anyone else's cutouts. Gaia answers are kept there too, and
reruns are offline. The results file is rewritten after every field, so a long run
can be read part way.
"""
from __future__ import annotations

import os

# SPHEREx-only motions: set before planetx reads its settings.
os.environ["PLANETX_OBSERVATORIES"] = "spherex"

import argparse  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import random  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import warnings  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

import astropy.units as u  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402
from astropy.coordinates import SkyCoord  # noqa: E402
from astropy.io import fits  # noqa: E402
from planetx import pipeline  # noqa: E402
from planetx.analysis import gaia_truth  # noqa: E402
from planetx.api.schemas import Visit  # noqa: E402
from planetx.config import settings  # noqa: E402
from planetx.engine import Frame, frame_from_hdul  # noqa: E402
from planetx.observatories import SPHEREX_ONLY, parse_observatories  # noqa: E402
from planetx.spherex import archive, cutouts  # noqa: E402

log = logging.getLogger("gaia_validation")

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "results" / "gaia_validation.json"
STORE = settings.cache_dir / "gaia_validation"
KEEP_HDUS = ("IMAGE", "FLAGS", "VARIANCE", "WCS-WAVE")
FIELD_DEG = 0.2
FIELD_ATTEMPTS = 5
# Pause before trying a field again after an IRSA failure (outages last minutes).
RETRY_PAUSE_S = 120.0
DOWNLOAD_ROUNDS = 3
DOWNLOAD_PAUSE_S = 30.0

GAIA_TAP_URL = "https://gea.esac.esa.int/tap-server/tap/sync"
GATOR_URL = "https://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query"
GAIA_TIMEOUT_S = (10.0, 240.0)
GAIA_MAX_ROWS = 100_000
GAIA_MAX_G = 21.0
# Gaia positions are at 2016.0; a 15"/yr star (the engine's max) has moved 165" by 2027.
GAIA_REACH_ARCSEC = 200.0

# The targets' centres are tests/targets.py ENGINE_TARGETS_DEG (what the engine tests use).
FIELDS: dict[str, tuple[float, float, str]] = {
    "nep": (270.0, 66.5607, "North Ecliptic Pole: most visits, where the survey crawl starts"),
    "nep-s": (270.0, 66.06, "0.5 deg south of the NEP: near-continuous coverage, different stars"),
    "nep-n": (270.0, 67.06, "0.5 deg north of the NEP"),
    "nep-e": (271.25, 66.5607, "0.5 deg east of the NEP"),
    "barnard": (269.4462678797139, 4.768204454786656, "test target: Barnard's Star (10.4\"/yr), crowded b=+14 field"),
    "wise0855": (133.75976524998046, -7.24218218007099, "test target: WISE 0855 (~8\"/yr, very red, faint)"),
    "eps-indi-b": (331.09666878308445, -56.8006491764846, "test target: Epsilon Indi B (4.7\"/yr)"),
    "rw-aur": (76.95658657548722, 30.401291346097146, "test target: RW Aur (variable, not a mover)"),
    "r-dra": (248.16723307211967, 66.75502382621286, "test target: R Dra (Mira variable, near the NEP)"),
    "mid-a": (150.0, 40.0, "ordinary field: ecliptic lat +26, galactic lat +53"),
    "mid-b": (200.0, 10.0, "ordinary field: ecliptic lat +17, galactic lat +72 (sparse)"),
    "mid-c": (30.0, -10.0, "ordinary field: ecliptic lat -21, galactic lat -67"),
    "mid-d": (60.0, -35.0, "ordinary field: ecliptic lat -54, galactic lat -49"),
    # Blends are the suspected source of false movers: two fields as crowded as Barnard's.
    "crowd-a": (272.628, 1.968, "crowded field: galactic l=30 b=+10, ecliptic lat +25 (blends)"),
    "crowd-b": (87.979, 6.717, "crowded field: galactic anticentre l=200 b=-10, ecliptic lat -17 (blends)"),
}

_write_lock = threading.Lock()


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _trimmed(body: bytes) -> bytes:
    """The cutout with only the HDUs `frame_from_hdul` reads (the EPSF table is most of the 4 MB)."""
    with fits.open(io.BytesIO(body)) as hdul:
        keep = fits.HDUList([fits.PrimaryHDU(header=hdul[0].header)] + [hdul[n].copy() for n in KEEP_HDUS if n in hdul])
        out = io.BytesIO()
        keep.writeto(out)
    return out.getvalue()


def cutout_file(name: str, url: str, request: tuple[float, float, float], shared_ok: bool = True) -> Path:
    """A local file holding the cutout: the shared cache's copy if there is one, else our trimmed copy."""
    shared = cutouts.cutout_path(url, *request)
    if shared_ok and shared.exists():
        return shared
    digest = hashlib.sha256(f"{url}|{request[0]:.7f}|{request[1]:.7f}|{request[2]:.5f}".encode()).hexdigest()
    mine = STORE / name / f"{digest}.fits"
    if not mine.exists():
        _atomic_write(mine, _trimmed(archive.fetch_cutout(url, *request)))
    return mine


def load_frames(name: str, visits, request) -> tuple[list[Frame], int, list[str]]:
    """(frames, visits without data here, failure messages), like pipeline's own loader.

    Downloads IRSA refused as busy are tried again in later rounds, so the sample
    stays the one `pick_visits` chose.
    """
    frames: list[Frame] = []
    no_data = 0
    lock = threading.Lock()
    failed: dict[str, str] = {}  # access_url -> message
    busy: list = []

    def load(visit) -> None:
        nonlocal no_data
        try:
            try:
                with fits.open(cutout_file(name, visit.access_url, request)) as hdul:
                    frame = frame_from_hdul(hdul, obs_id=visit.obs_id)
            except FileNotFoundError:
                # The shared cache evicted it between the check and the open: keep our own copy.
                with fits.open(cutout_file(name, visit.access_url, request, shared_ok=False)) as hdul:
                    frame = frame_from_hdul(hdul, obs_id=visit.obs_id)
        except archive.UpstreamError as exc:
            with lock:
                if exc.status in pipeline.NO_CUTOUT_STATUS and not isinstance(exc, archive.IrsaBusy):
                    no_data += 1
                else:
                    failed[visit.access_url] = str(exc)
                    if exc.transient:
                        busy.append(visit)
        except (KeyError, ValueError, OSError) as exc:
            with lock:
                failed[visit.access_url] = f"{type(exc).__name__}: {exc}"
        else:
            with lock:
                failed.pop(visit.access_url, None)
                frames.append(frame)

    todo = list(visits)
    for round_ in range(DOWNLOAD_ROUNDS):
        if round_:
            log.info("%s: %d cutouts refused as busy; trying again in %.0fs", name, len(todo), DOWNLOAD_PAUSE_S)
            time.sleep(DOWNLOAD_PAUSE_S)
        busy.clear()
        # pipeline.DOWNLOAD_WORKERS (3) keeps us under the team's 4 concurrent IRSA requests.
        with ThreadPoolExecutor(max_workers=pipeline.DOWNLOAD_WORKERS) as pool:
            list(pool.map(load, todo))
        log.info("%s: %d/%d frames loaded", name, len(frames), len(visits))
        todo = list(busy)
        if not todo:
            break
    return frames, no_data, list(failed.values())


def _gaia_esa(ra: float, dec: float, radius_arcsec: float) -> str:
    """ESA's Gaia archive (TAP sync): CSV with gaia_truth.GAIA_COLUMNS."""
    # Every value below is a number formatted here: no ADQL injection.
    query = (
        f"SELECT TOP {GAIA_MAX_ROWS} {', '.join(gaia_truth.GAIA_COLUMNS)} FROM gaiadr3.gaia_source "
        f"WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra:.7f}, {dec:.7f}, {radius_arcsec / 3600:.6f})) "
        f"AND phot_g_mean_mag < {GAIA_MAX_G:g} ORDER BY phot_g_mean_mag"
    )
    params = {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": query}
    resp = requests.post(GAIA_TAP_URL, data=params, timeout=GAIA_TIMEOUT_S)
    if resp.status_code != 200:
        raise RuntimeError(f"ESA Gaia returned HTTP {resp.status_code}: {resp.text[:200]!r}")
    return resp.text


def _gaia_gator(ra: float, dec: float, radius_arcsec: float) -> str:
    """The same columns from IRSA Gator's copy of Gaia DR3 (gaia_dr3_source), as CSV.

    Gator reports errors as HTTP 200 with a `[struct stat="ERROR"` body.
    """
    params = {
        "catalog": "gaia_dr3_source", "spatial": "cone", "objstr": f"{ra:.7f} {dec:+.7f}",
        "radius": f"{radius_arcsec:.1f}", "radunits": "arcsec", "outfmt": "1",
        "selcols": ",".join(gaia_truth.GAIA_COLUMNS),
    }
    resp = requests.get(GATOR_URL, params=params, timeout=GAIA_TIMEOUT_S)
    if resp.status_code != 200 or "[struct stat=" in resp.text[:500]:
        raise RuntimeError(f"Gator Gaia failed (HTTP {resp.status_code}): {resp.text[:200]!r}")
    return gaia_truth.gaia_ipac_to_csv(resp.text)


def fetch_gaia_csv(name: str, ra: float, dec: float, radius_arcsec: float) -> str:
    """Gaia DR3 stars in the cone as CSV with gaia_truth.GAIA_COLUMNS, cached per field and cone.

    ESA's archive first, then IRSA Gator's copy of the same table (ESA's TAP timed
    out on connect on 2026-11-14).
    """
    path = STORE / name / f"gaia_{ra:.5f}_{dec:.5f}_{radius_arcsec:.0f}.csv"
    if path.exists():
        return path.read_text()
    errors = []
    for source, attempts in ((_gaia_esa, 1), (_gaia_gator, 3)):
        for attempt in range(attempts):
            try:
                text = source(ra, dec, radius_arcsec)
                n_rows = len(gaia_truth.parse_gaia_csv(text))  # raises on an unexpected body
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                errors.append(f"{source.__name__}: {type(exc).__name__}: {exc}"[:300])
                log.warning("Gaia query for %s failed: %s", name, errors[-1])
                if attempt < attempts - 1:
                    time.sleep(3 * (attempt + 1) * (1 + random.random()))
                continue
            if n_rows >= GAIA_MAX_ROWS:
                # Cut at the faint end: the chance-match density would be too low.
                raise RuntimeError(f"Gaia cone for {name} hit the {GAIA_MAX_ROWS}-row limit; use a smaller field")
            _atomic_write(path, text.encode())
            log.info("%s: Gaia DR3 from %s", name, source.__name__)
            return text
    raise RuntimeError(f"Gaia query for {name} kept failing ({errors[-1]})")


def _footprint(frames: list[Frame]):
    """`inside(ra, dec)`: the position lies inside every analysed frame (2 px margin), and the cone radius."""
    def inside(ra: float, dec: float) -> bool:
        for f in frames:
            x, y = f.wcs.all_world2pix(ra, dec, 0)
            ny, nx = f.data.shape
            if not (2 <= x <= nx - 3 and 2 <= y <= ny - 3):
                return False
        return True

    f = frames[0]
    ny, nx = f.data.shape
    ra_c, dec_c = f.wcs.all_pix2world((nx - 1) / 2, (ny - 1) / 2, 0)
    corners = f.wcs.all_pix2world([0, nx - 1, 0, nx - 1], [0, 0, ny - 1, ny - 1], 0)
    centre = SkyCoord(float(ra_c) * u.deg, float(dec_c) * u.deg)
    half_diag = float(centre.separation(SkyCoord(corners[0] * u.deg, corners[1] * u.deg)).arcsec.max())
    return inside, float(ra_c) % 360, float(dec_c), half_diag


def list_visits(name: str, ra: float, dec: float) -> list[Visit]:
    """Every SPHEREx visit at the field centre, as `pipeline.analyse_field` lists them; cached per field
    so a rerun neither waits for IRSA's image search nor sees a different sample."""
    path = STORE / name / f"visits_{ra:.6f}_{dec:.6f}.json"
    if path.exists():
        return [Visit.model_validate(v) for v in json.loads(path.read_text())]
    visits = pipeline.retry_upstream(
        lambda: archive.query_visits(ra, dec, pipeline.VISIT_RADIUS_ARCSEC), f"{name}: visit listing", lambda: False,
    )
    _atomic_write(path, json.dumps([v.model_dump() for v in visits]).encode())
    return visits


def run_field(name: str) -> dict:
    ra, dec, why = FIELDS[name]
    t0 = time.monotonic()
    visits = pipeline.pick_visits(list_visits(name, ra, dec))
    request = cutouts.cutout_request(ra, dec, FIELD_DEG)
    log.info("%s: %d visits picked", name, len(visits))
    frames, no_data, failures = load_frames(name, visits, request)
    run = pipeline.analyse_frames(frames, n_visits=len(visits), no_data=no_data, failures=failures, where=name)
    result = run.result
    objects = result.objects
    inside, ra_c, dec_c, half_diag = _footprint(result.frames)
    radius = min(half_diag + gaia_truth.BLEND_RADIUS_ARCSEC + GAIA_REACH_ARCSEC, 3600.0)
    stars = gaia_truth.parse_gaia_csv(fetch_gaia_csv(name, ra_c, dec_c, radius))
    gaia = gaia_truth.GaiaField(stars, radius)
    matched, unmatched = gaia_truth.evaluate_field(objects, gaia, name)
    mid_mjd = float(np.median([f.mjd for f in result.frames]))
    missed = gaia_truth.untracked_movers(gaia, matched, mid_mjd, inside)
    for m in missed:
        m["field"] = name
    info = {
        "name": name, "ra": ra, "dec": dec, "why": why, "size_deg": FIELD_DEG,
        "cutout_request": {"ra": request[0], "dec": request[1], "size_deg": request[2]},
        "n_frames": len(result.frames), "n_passes": len(result.passes),
        "mjd_range": [round(min(f.mjd for f in result.frames), 3), round(max(f.mjd for f in result.frames), 3)],
        "n_objects": len(objects), "n_matched": len(matched), "n_unmatched": len(unmatched),
        "n_engine_movers": sum(o.is_mover for o in objects),
        "n_false_movers": len(gaia_truth.false_movers(matched)),
        "n_by_truth": {t: sum(r["truth"] == t for r in matched) for t in gaia_truth.TRUTHS},
        "n_untracked_gaia_movers": len(missed),
        "n_gaia_stars": len(stars), "n_gaia_visible": len(gaia), "gaia_radius_arcsec": round(radius, 1),
        "position_error_scale": round(result.position_error_scale, 3),
        "engine_notes": run.notes,
        "seconds": round(time.monotonic() - t0, 1),
    }
    log.info("%s: %d objects, %d matched, %d engine movers, %d Gaia stars (%.0fs)", name, len(objects),
             len(matched), info["n_engine_movers"], len(stars), info["seconds"])
    return {"field": info, "matched": matched, "unmatched": unmatched, "untracked_movers": missed}


def build(done: dict[str, dict], failed: dict[str, str], argv: list[str]) -> dict:
    matched = [r for d in done.values() for r in d["matched"]]
    unmatched = [r for d in done.values() for r in d["unmatched"]]
    missed = [r for d in done.values() for r in d["untracked_movers"]]
    n_objects = len(matched) + len(unmatched)
    summary = gaia_truth.summarize(matched, n_objects)
    fm = gaia_truth.false_movers(matched)
    engine_movers = sorted((r for r in matched + unmatched if r["engine_is_mover"]),
                           key=lambda r: -(r["pm_significance"] or 0.0))
    summary |= {
        "n_unmatched": len(unmatched),
        "n_engine_movers": len(engine_movers),
        "n_false_movers": len(fm),
        "n_untracked_gaia_movers": len(missed),
    }
    tp, fn = summary["confusion"]["tp"], summary["confusion"]["fn"]
    summary["recovered_fraction_incl_untracked"] = (
        round(tp / (tp + fn + len(missed)), 4) if tp + fn + len(missed) else None
    )
    return {
        "name": "gaia_validation",
        "version": 1,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": "uv run python tools/gaia_validation.py" + "".join(f" {a}" for a in argv),
        "notes": [
            ("SPHEREx-only engine motions (find_motion via pipeline.analyse_frames, no archival refit) compared "
             "with Gaia DR3 proper motions; single exposures, not stacks."),
            (f"Fields are {FIELD_DEG} deg as the website asks for them; the engine sees the oversized raw cutout "
             f"(cutout_request size), about {FIELD_DEG * cutouts.CUTOUT_OVERSIZE:.2f} deg on a side."),
            ("Gaia 2016.0 positions are moved to each pass's date with Gaia's proper motion. A track is matched "
             "to the Gaia star (G <= 19.5) found in the most passes, ties going to the brightest, within "
             f"max({gaia_truth.MATCH_RADIUS_ARCSEC}\", 3 x the pass position error) and at most "
             f"{gaia_truth.MAX_MATCH_RADIUS_ARCSEC}\"."),
            (f"truth: static = Gaia total PM < {gaia_truth.STATIC_PM}\"/yr with both components known to "
             f"{gaia_truth.STATIC_PM_ERR}\"/yr; real_mover = Gaia PM > {gaia_truth.MOVER_PM}\"/yr and > "
             f"{gaia_truth.MOVER_SIGMA:g} x the SPHEREx error; every star blended in the same pixel (within "
             f"{gaia_truth.BLEND_RADIUS_ARCSEC}\", up to {gaia_truth.BLEND_DMAG} mag fainter) must agree. Tracks whose "
             "passes fall on different static stars are static. Partial matches, chance matches "
             f"(p > {gaia_truth.CHANCE_MAX}) and in-between motions are ambiguous."),
            ("residual_median: median |SPHEREx - Gaia| PM (arcsec/yr); residual_mad: robust std (1.4826 x MAD) of "
             "one residual component; pull = residual / sqrt(spherex_pm_err^2 + gaia_pm_err^2) per component; "
             "pull_std is the plain std, pull_robust_std ignores outliers. They use the clean rows only: the Gaia "
             "star is in every pass, no blend, not a chance match."),
            ("Confusion counts tracked objects with truth real_mover or static: tp = engine mover and real, "
             "fp = engine mover but Gaia-static, fn = real but not flagged, tn = static, not flagged. "
             "false_mover_rate = fp / (fp + tn); recovered_fraction = tp / (tp + fn). Gaia stars faster than "
             f"{gaia_truth.MOVER_PM}\"/yr with G <= {gaia_truth.UNTRACKED_MAX_G} inside the frames that no track "
             "saw (as its star, a blend or a pass's star) are listed in untracked_gaia_movers; "
             "recovered_fraction_incl_untracked counts them as misses (without the 3-sigma condition, which "
             "needs a SPHEREx error)."),
            ("A Gaia star matched by several tracks with no pass in common is split_track: the engine cut one "
             "object's passes into pieces (n_stars_split_into_tracks)."),
            ("Engine motions are relative to the field's stars (frames are aligned on them); "
             "summary.frame_pm_offset is the median residual of the clean static stars."),
            "Engine movers are candidates to check, never confirmed objects.",
        ],
        "fields": [d["field"] for d in done.values()],
        "failed_fields": [{"name": k, "error": v} for k, v in failed.items()],
        "summary": summary,
        "false_movers": fm,
        "engine_movers": engine_movers,
        "untracked_gaia_movers": missed,
        # Per-pass positions only for movers (above): they would triple the file.
        "objects": [_without_passes(r) for r in matched],
        "unmatched": [_without_passes(r) for r in unmatched],
    }


def _without_passes(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "passes"}


def write(doc: dict) -> None:
    """One line per list entry: small enough to serve, and readable in a diff."""
    parts = []
    for key, value in doc.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            body = ",\n  ".join(json.dumps(v, allow_nan=False, separators=(",", ":")) for v in value)
            parts.append(f"{json.dumps(key)}: [\n  {body}\n]")
        else:
            parts.append(f"{json.dumps(key)}: {json.dumps(value, allow_nan=False, indent=1)}")
    text = "{\n" + ",\n".join(parts) + "\n}\n"
    json.loads(text)  # the hand-joined layout must still be valid JSON
    with _write_lock:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(OUT, text.encode())
        OUT.chmod(0o644)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fields", nargs="*", help=f"field names (default: all of {', '.join(FIELDS)})")
    parser.add_argument("--list", action="store_true", help="print the fields and exit")
    args = parser.parse_args(argv)
    if args.list:
        for name, (ra, dec, why) in FIELDS.items():
            print(f"{name:12s} {ra:10.5f} {dec:+9.5f}  {why}")
        return 0
    unknown = [f for f in args.fields if f not in FIELDS]
    if unknown:
        parser.error(f"unknown field(s): {', '.join(unknown)}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    warnings.filterwarnings("ignore", module="astropy")
    if parse_observatories() != SPHEREX_ONLY:
        raise SystemExit("PLANETX_OBSERVATORIES must be spherex for this tool")

    done: dict[str, dict] = {}
    failed: dict[str, str] = {}
    queue = [(name, 1) for name in args.fields or list(FIELDS)]
    while queue:
        name, attempt = queue.pop(0)
        if attempt > 1:
            time.sleep(RETRY_PAUSE_S)
        try:
            done[name] = run_field(name)
            failed.pop(name, None)
        except archive.UpstreamError as exc:
            # IRSA outages come in bursts: try the field again after the others.
            log.error("%s failed (attempt %d): %s", name, attempt, exc)
            failed[name] = f"{type(exc).__name__}: {exc}"
            if attempt < FIELD_ATTEMPTS:
                queue.append((name, attempt + 1))
        except Exception as exc:  # one broken field (bad cutout, odd catalogue answer) must not end the run
            log.exception("%s failed", name)
            failed[name] = f"{type(exc).__name__}: {exc}"
        doc = build(done, failed, argv)
        write(doc)
        s = doc["summary"]
        log.info("so far: %d objects, %d matched, %d false movers, median |residual| %s", s["n_objects"],
                 s["n_matched"], s["n_false_movers"], s["residual_median"])
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
