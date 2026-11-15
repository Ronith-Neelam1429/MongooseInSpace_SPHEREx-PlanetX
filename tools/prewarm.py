"""Fill the caches for the demo, so the pitch makes no slow first-time IRSA requests.

Drives the real API, in the order and with the parameters the website uses, for the five
test targets: visits, the time-lapse and sky-picture frames (cutout PNGs), the 2MASS/WISE
pictures, a /api/motion run (+ the evidence of its top objects), and a spectrum.

    uv run python tools/prewarm.py                    # starts its own API on port 8765
    uv run python tools/prewarm.py --base http://127.0.0.1:8000   # warm a running server too
    uv run python tools/prewarm.py --targets barnard,r-dra --dry-run

The disk caches (cutouts, surveys) survive a restart; the in-process ones (SIA visit lists,
finished motion runs, spectra) live in the API process, so to warm those run this against the
server the demo will use (--base). Re-running is cheap and resumable: whatever is on disk is
served from disk. Honours PLANETX_CACHE_DIR / PLANETX_CACHE_MAX_MB like the API.
Never more than 4 requests in flight (IRSA's limit in docs/API.md).
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import requests

MAX_IN_FLIGHT = 4
MOTION_SIZE_DEG = 0.1  # App.jsx motionSize() for the target view (fov 0.25 -> radius 0.075)
OLD_YEAR_TIMELAPSE, MARGIN_TIMELAPSE, MIN_SIDE_TIMELAPSE = 1999.5, 90, 180  # lib/timelapse.js
OLD_YEAR_PICTURES, MARGIN_PICTURES, MIN_SIDE_PICTURES = 1999.0, 75, 150  # lib/pictures.js
MAX_SIDE = 900
PASS_GAP_DAYS = 30
DETECTOR = 3
RETRIES = 3


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    ra: float  # J2000 (epoch 2000), as frontend/src/lib/targets.js
    dec: float
    pmra: float  # mas/yr (includes cos dec)
    pmdec: float
    view: tuple[float, float] | None = None  # shared-URL centre when it is not the moved position
    fov: float = 0.25
    spectrum: bool = False  # warm the spectrum by default (~290 MB of cutouts each)
    # Gaia DR3 (ra, dec, pmra, pmdec) at epoch 2016.0, as the website's object card sends them to
    # /api/spectrum (read off its requests, 2026-11-15). None: not in Gaia, so the card sends the
    # SIMBAD J2000 position moved to epoch_now(), which `spectrum_query` reproduces.
    gaia: tuple[float, float, float, float] | None = None


# Mirrors TARGETS in frontend/src/lib/targets.js (a test keeps them in step).
TARGETS = [
    Target("barnard", "Barnard's Star", 269.4520769586187, 4.693364966576667, -801.551, 10362.394,
           view=(269.4473, 4.7685), gaia=(269.448503, 4.739420, -801.6, 10362.4)),  # CLAUDE.md: the J2000 position is 4.5' off now
    Target("eps-indi-b", "Epsilon Indi B", 331.04414658852124, -56.782844195116105, 3981.977, -2466.832,
           spectrum=True, gaia=(331.076453, -56.793812, 3982.0, -2466.8)),
    Target("wise0855", "WISE 0855-0714", 133.81890825, -7.247048, -8123.7, 673.2, spectrum=True),
    Target("rw-aur", "RW Aurigae", 76.95652603458333, 30.401438173888888, 7.23, -20.33),
    Target("r-dra", "R Draconis", 248.16761083295, 66.75497941722, -20.643, 6.149),
]


# ---------- pure helpers (tested offline) ----------

def decimal_year(now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    start = datetime(now.year, 1, 1, tzinfo=UTC)
    end = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    return now.year + (now - start) / (end - start)


def epoch_now(now: datetime | None = None) -> float:
    """The website's "now" (epochNow in frontend/src/lib/coords.js): the start of the UTC month.
    Every position it asks the API about is computed at this epoch, so warming the same
    positions only works with the same rule; a re-run is needed after the month changes."""
    now = now or datetime.now(UTC)
    return decimal_year(datetime(now.year, now.month, 1, tzinfo=UTC))


def propagate(ra: float, dec: float, pmra: float, pmdec: float, years: float) -> tuple[float, float]:
    """Linear proper-motion step (mas/yr) from a J2000 position; fine over a few arcminutes."""
    new_dec = dec + pmdec * years / 3.6e6
    new_ra = ra + pmra * years / 3.6e6 / math.cos(math.radians(dec))
    return new_ra % 360, new_dec


def view_centre(t: Target, now_year: float) -> tuple[float, float]:
    return t.view or propagate(t.ra, t.dec, t.pmra, t.pmdec, now_year - 2000.0)


def sky_patch(t: Target, now_year: float, old_year: float, margin: float, min_side: float) -> dict:
    """The square every picture of a catalogued star shows: centred between where it was in
    `old_year` and where it is now, wide enough for both. ra/dec in deg, side in arcsec."""
    then = propagate(t.ra, t.dec, t.pmra, t.pmdec, old_year - 2000.0)
    now = propagate(t.ra, t.dec, t.pmra, t.pmdec, now_year - 2000.0)
    east = (then[0] - now[0]) * math.cos(math.radians(now[1])) * 3600
    north = (then[1] - now[1]) * 3600
    span = math.hypot(east, north)
    want = span + 2 * margin
    side = round(min(MAX_SIDE, max(min_side, want)))
    centre = now if want > MAX_SIDE else ((then[0] + now[0]) / 2, (then[1] + now[1]) / 2)
    ra, dec = snap_centre(*centre)
    return {"ra": ra, "dec": dec, "side": side}


def snap_centre(ra: float, dec: float) -> tuple[float, float]:
    """snapCentre in frontend/src/lib/coords.js: round to 0.001 deg (JS Math.round, half up) so
    the website's Gaia/SIMBAD positions and these give the same patch, i.e. the same cache entry."""
    return (math.floor(ra * 1000 + 0.5) / 1000) % 360, math.floor(dec * 1000 + 0.5) / 1000


def spectrum_query(t: Target, now_year: float) -> str:
    """The /api/spectrum query string the website's object card sends for this target."""
    if t.gaia:
        ra, dec, pmra, pmdec = t.gaia
        epoch = 2016.0
    else:
        (ra, dec), pmra, pmdec, epoch = propagate(t.ra, t.dec, t.pmra, t.pmdec, now_year - 2000.0), t.pmra, t.pmdec, now_year
    return f"ra={ra:.6f}&dec={dec:.6f}&pmra={pmra:.1f}&pmdec={pmdec:.1f}&epoch={epoch:.3f}"


def cutout_size_deg(side_arcsec: float) -> float:
    return round(min(0.5, max(0.01, side_arcsec / 3600)), 5)


def frame_per_pass(frames: list[dict]) -> list[dict]:
    """One frame per survey pass (nearest the pass's middle), oldest first: lib/timelapse.js."""
    passes: list[list[dict]] = []
    for f in sorted(frames, key=lambda f: f["mjd"]):
        if passes and f["mjd"] - passes[-1][-1]["mjd"] <= PASS_GAP_DAYS:
            passes[-1].append(f)
        else:
            passes.append([f])
    out = []
    for p in passes:
        mid = (p[0]["mjd"] + p[-1]["mjd"]) / 2
        out.append(min(p, key=lambda f: abs(f["mjd"] - mid)))
    return out


def dir_stats(root: Path, since: float | None = None) -> tuple[int, int]:
    """(file count, bytes) under root; with `since`, only files modified at or after it
    (the cache refreshes an entry's mtime on every hit, so these are the files a run used)."""
    n = size = 0
    for p in root.rglob("*") if root.exists() else ():
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file() and (since is None or st.st_mtime >= since):
            n += 1
            size += st.st_size
    return n, size


def cache_report(cache_dir: Path, cap_mb: int, since: float) -> dict:
    """What the cache holds and whether this run's set fits the cap."""
    total_n, total = dir_stats(cache_dir)
    used_n, used = dir_stats(cache_dir, since)
    cap = cap_mb * 1024 * 1024
    warnings = []
    if used > 0.9 * cap:
        warnings.append(f"the demo set ({mb(used)}) is over 90% of PLANETX_CACHE_MAX_MB ({cap_mb}): "
                        "entries the API evicts may include demo ones; raise the cap")
    elif total > cap:
        warnings.append(f"the cache ({mb(total)}) is over its cap ({cap_mb} MB): the API drops the "
                        "least recently used cutouts at its next download (this run's are newest, so they stay)")
    return {"files": total_n, "bytes": total, "demo_files": used_n, "demo_bytes": used,
            "cap_bytes": cap, "warnings": warnings}


def mb(n: float) -> str:
    return f"{n / 1024 / 1024:.0f} MB"


class StopRun(Exception):
    """A budget guard tripped: stop cleanly and report."""


@dataclass
class Budget:
    cache_dir: Path
    max_new_mb: int
    min_free_gb: float
    start_bytes: int = 0

    def check(self) -> None:
        free = shutil.disk_usage(self.cache_dir if self.cache_dir.exists() else self.cache_dir.parent).free
        if free < self.min_free_gb * 1024**3:
            raise StopRun(f"disk free {free / 1024**3:.1f} GB is below --min-free-gb {self.min_free_gb}")
        grown = dir_stats(self.cache_dir)[1] - self.start_bytes
        if grown > self.max_new_mb * 1024 * 1024:
            raise StopRun(f"downloaded {mb(grown)} this run, over --max-new-mb {self.max_new_mb}")


# ---------- HTTP ----------

class Api:
    def __init__(self, base: str, log=print):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.log = log

    def get(self, path: str, timeout: float = 150, binary: bool = False, ok404: bool = False):
        """GET with retries on 503 (honours Retry-After) and 502/5xx, as the website does."""
        url = self.base + path
        for attempt in range(1, RETRIES + 3):
            try:
                r = self.s.get(url, timeout=timeout)
            except requests.RequestException as exc:
                if attempt > RETRIES:
                    raise RuntimeError(f"{path.split('?')[0]}: {exc}") from exc
                time.sleep(2 * attempt)
                continue
            # 503 = busy (wait and retry). 502 means the API already retried IRSA itself: do not
            # multiply those waits. Other 5xx once.
            if r.status_code == 503 or (r.status_code in (500, 504) and attempt == 1):
                try:
                    wait = float(r.headers.get("Retry-After", 3 * attempt))
                except ValueError:
                    wait = 3 * attempt
                time.sleep(min(wait, 30))
                continue
            if r.status_code == 404 and ok404:
                return None
            if r.status_code >= 400:
                raise RuntimeError(f"{path.split('?')[0]}: HTTP {r.status_code} {r.text[:160]}")
            return r.content if binary else r.json()
        raise RuntimeError(f"{path.split('?')[0]}: still busy after retries")

    def poll(self, path: str, timeout_s: float, every_s: float = 4.0, label: str = "") -> dict:
        t0, last = time.monotonic(), None
        while time.monotonic() - t0 < timeout_s:
            job = self.get(path)
            if job["status"] == "done":
                return job
            if job["status"] == "failed":
                raise RuntimeError(f"{label} failed: {job.get('error')}")
            prog = (job.get("stage"), job.get("frames_done"), job.get("frames_total"))
            if prog != last:
                self.log(f"      {label}: {prog[0]} {prog[1] or 0}/{prog[2] or '?'}")
                last = prog
            time.sleep(every_s)
        raise RuntimeError(f"{label}: not done after {timeout_s:.0f} s")


# ---------- tasks ----------

@dataclass
class Result:
    target: str
    task: str
    ok: bool
    seconds: float
    detail: str


@dataclass
class Runner:
    api: Api
    budget: Budget
    now_year: float
    jobs: int = 3
    motion_timeout: float = 900
    results: list[Result] = field(default_factory=list)
    visits_failed_in_a_row: int = 0

    def run(self, target: Target, task: str, fn) -> None:
        self.budget.check()
        t0 = time.monotonic()
        try:
            detail, ok = fn(), True
        except StopRun:
            raise
        except Exception as exc:  # one failed task must not stop the others
            detail, ok = f"{type(exc).__name__}: {exc}", False
        res = Result(target.id, task, ok, time.monotonic() - t0, str(detail))
        self.results.append(res)
        print(f"  [{'ok' if ok else 'FAIL'}] {task:<22} {res.seconds:6.1f}s  {res.detail}", flush=True)

    def pmap(self, fn, items: list) -> list:
        with ThreadPoolExecutor(max_workers=min(self.jobs, MAX_IN_FLIGHT)) as pool:
            return list(pool.map(fn, items))

    # each returns a short detail string, or raises
    def visits(self, t: Target) -> str:
        ra, dec = view_centre(t, self.now_year)
        body = self.api.get(f"/api/visits?ra={ra:.6f}&dec={dec:.6f}")
        return f"{body['n_visits']} visits in {len(body['passes'])} passes"

    def frames(self, t: Target, patch: dict) -> str:
        size = cutout_size_deg(patch["side"])
        q = f"ra={patch['ra']:.6f}&dec={patch['dec']:.6f}&size_deg={size}"
        body = self.api.get(f"/api/frames?{q}&detector={DETECTOR}")
        frames = body["frames"] or self.api.get(f"/api/frames?{q}")["frames"]
        chosen = frame_per_pass(frames)
        self.pmap(lambda f: self.api.get(f["image_url"], binary=True), chosen)
        return f"{len(chosen)} cutout PNGs ({size} deg)"

    def surveys(self, t: Target, patch: dict) -> str:
        q = f"ra={patch['ra']:.6f}&dec={patch['dec']:.6f}&side_arcsec={patch['side']}"
        body = self.api.get(f"/api/motion/evidence/surveys?{q}")
        got = [i.get("survey") or i.get("name") for i in body["images"]]
        miss = [u.get("survey") if isinstance(u, dict) else u for u in body["unavailable"]]
        if not got:
            raise RuntimeError(f"no archive image (unavailable: {miss})")
        return f"{len(got)} images, unavailable: {miss or 'none'}"

    def motion(self, t: Target) -> str:
        ra, dec = view_centre(t, self.now_year)
        q = f"ra={ra:.6f}&dec={dec:.6f}&size_deg={MOTION_SIZE_DEG:.4f}"
        job = self.api.poll(f"/api/motion?{q}", self.motion_timeout, label="motion")
        objs = job["result"]["objects"]
        notable = [o for o in objs if o["kind"] != "static" or o.get("is_variable")]
        for o in notable[:2]:  # the object card the demo opens
            self.api.get(f"/api/motion/evidence?{q}&object={o['id']}", timeout=300, ok404=True)
        movers = [o for o in objs if o["kind"] == "mover"]
        pm = max((math.hypot(o.get("pm_ra_cosdec") or 0, o.get("pm_dec") or 0) for o in movers), default=0)
        return f"{len(objs)} objects, {len(movers)} movers (fastest {pm:.2f} arcsec/yr), evidence for {min(2, len(notable))}"

    def spectrum(self, t: Target) -> str:
        q = spectrum_query(t, self.now_year)
        job = self.api.poll(f"/api/spectrum?{q}", self.motion_timeout, label="spectrum")
        res = job["result"]
        return f"{len(res.get('points', []))} exposures, {len(res.get('bands', []))} bands"

    def target(self, t: Target, with_spectrum: bool) -> None:
        print(f"{t.label}  (?ra={view_centre(t, self.now_year)[0]:.4f}&dec={view_centre(t, self.now_year)[1]:.4f}&fov={t.fov})", flush=True)
        lapse = sky_patch(t, self.now_year, OLD_YEAR_TIMELAPSE, MARGIN_TIMELAPSE, MIN_SIDE_TIMELAPSE)
        pics = sky_patch(t, self.now_year, OLD_YEAR_PICTURES, MARGIN_PICTURES, MIN_SIDE_PICTURES)
        self.run(t, "visits", lambda: self.visits(t))
        if not self.results[-1].ok:
            # Every later step lists visits first, so it would fail the same way, slowly.
            self.visits_failed_in_a_row += 1
            if self.visits_failed_in_a_row >= 2:
                raise StopRun("IRSA (or the API) looks down: two targets in a row could not list visits")
            print("  skipping the other steps for this target", flush=True)
            return
        self.visits_failed_in_a_row = 0
        self.run(t, "time-lapse frames", lambda: self.frames(t, lapse))
        self.run(t, "time-lapse surveys", lambda: self.surveys(t, lapse))
        self.run(t, "sky-picture frames", lambda: self.frames(t, pics))
        self.run(t, "sky-picture surveys", lambda: self.surveys(t, pics))
        self.run(t, "motion + evidence", lambda: self.motion(t))
        if with_spectrum:
            self.run(t, "spectrum", lambda: self.spectrum(t))


# ---------- server + main ----------

def start_server(port: int, env: dict) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "uvicorn", "planetx.api.app:app", "--host", "127.0.0.1", "--port", str(port)]
    return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_healthy(api: Api, proc: subprocess.Popen | None, timeout_s: float = 120) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if proc and proc.poll() is not None:
            raise RuntimeError("the API process exited during start-up")
        try:
            if requests.get(api.base + "/api/health", timeout=3).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError(f"{api.base}/api/health not healthy after {timeout_s:.0f} s")


def select_targets(spec: str) -> list[Target]:
    if spec == "all":
        return list(TARGETS)
    by_id = {t.id: t for t in TARGETS}
    out = []
    for name in (s.strip() for s in spec.split(",") if s.strip()):
        if name not in by_id:
            raise SystemExit(f"unknown target {name!r}; choose from {', '.join(by_id)}")
        out.append(by_id[name])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", help="API origin of a running server (default: start one on --port)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--targets", default="all", help="comma list of: " + ", ".join(t.id for t in TARGETS))
    ap.add_argument("--spectrum", default="default", help="'default' (eps-indi-b, wise0855), 'all', 'none' or a comma list")
    ap.add_argument("--max-new-mb", type=int, default=1500, help="stop after the cache has grown this much")
    ap.add_argument("--min-free-gb", type=float, default=8.0, help="stop when the disk has less free space")
    ap.add_argument("--jobs", type=int, default=3, help=f"parallel cutout requests (max {MAX_IN_FLIGHT})")
    ap.add_argument("--motion-timeout", type=float, default=900, help="seconds to wait for one motion/spectrum run")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and the cache state, download nothing")
    args = ap.parse_args(argv)

    cache_dir = Path(os.environ.get("PLANETX_CACHE_DIR", ".cache/planetx")).resolve()
    cap_mb = int(os.environ.get("PLANETX_CACHE_MAX_MB", "2048"))
    now_year = epoch_now()
    targets = select_targets(args.targets)
    if args.spectrum == "default":
        spec_ids = {t.id for t in targets if t.spectrum}
    elif args.spectrum == "all":
        spec_ids = {t.id for t in targets}
    elif args.spectrum == "none":
        spec_ids = set()
    else:
        spec_ids = {s.strip() for s in args.spectrum.split(",")}

    n0, b0 = dir_stats(cache_dir)
    print(f"cache {cache_dir}: {n0} files, {mb(b0)} (cap {cap_mb} MB); disk free "
          f"{shutil.disk_usage(cache_dir if cache_dir.exists() else cache_dir.parent).free / 1024**3:.1f} GB")
    if args.dry_run:
        for t in targets:
            c = view_centre(t, now_year)
            print(f"  {t.id:<11} ?ra={c[0]:.4f}&dec={c[1]:.4f}&fov={t.fov}  spectrum={'yes' if t.id in spec_ids else 'no'}")
        return 0

    proc = None
    env = dict(os.environ, PLANETX_CACHE_DIR=str(cache_dir), PLANETX_CACHE_MAX_MB=str(cap_mb))
    base = args.base
    if not base:
        base = f"http://127.0.0.1:{args.port}"
        env.setdefault("PLANETX_CORS_ORIGINS", "http://localhost:5173")
        proc = start_server(args.port, env)
    api = Api(base)
    started = time.time()
    runner = Runner(api, Budget(cache_dir, args.max_new_mb, args.min_free_gb, b0), now_year,
                    jobs=args.jobs, motion_timeout=args.motion_timeout)
    stopped = None
    try:
        wait_healthy(api, proc)
        for t in targets:
            runner.target(t, t.id in spec_ids)
        runner.budget.check()
    except StopRun as exc:
        stopped = str(exc)
    except KeyboardInterrupt:
        stopped = "interrupted; re-run to continue (cached work is kept)"
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()

    rep = cache_report(cache_dir, cap_mb, started)
    failed = [r for r in runner.results if not r.ok]
    print("\n== summary ==")
    print(f"tasks: {len(runner.results) - len(failed)} ok, {len(failed)} failed")
    for r in failed:
        print(f"  FAILED {r.target} / {r.task}: {r.detail}")
    print(f"cache: {rep['files']} files, {mb(rep['bytes'])} of {cap_mb} MB cap; this run used {rep['demo_files']} "
          f"files ({mb(rep['demo_bytes'])}), grew by {mb(rep['bytes'] - b0)}")
    for w in rep["warnings"]:
        print(f"WARNING: {w}")
    if stopped:
        print(f"STOPPED EARLY: {stopped}")
    if failed or stopped:
        print("Re-run to retry: finished work is served from the cache.")
    return 1 if (failed or stopped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
