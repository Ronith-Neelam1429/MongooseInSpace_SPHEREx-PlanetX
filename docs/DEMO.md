# Demo runbook

How to start the demo, what to click, what you should see, and what to do when something is
down. Commands run from the repo root. The shared cache for the team is
`PLANETX_CACHE_DIR=.cache/planetx PLANETX_CACHE_MAX_MB=4000`.

## 0. The day before

1. `uv sync`, then `cd frontend && npm install`.
2. Warm the caches (needs IRSA up; about 1 GB of downloads the first time):
   ```
   export PLANETX_CACHE_DIR=$PWD/.cache/planetx PLANETX_CACHE_MAX_MB=4000
   uv run python tools/prewarm.py --dry-run          # the plan and the cache state, no downloads
   uv run python tools/prewarm.py                    # fills the disk cache; starts its own API on :8765
   ```
   It stops by itself if the disk has under 8 GB free (`--min-free-gb`) or if it has downloaded
   more than 1.5 GB (`--max-new-mb`). It is safe to re-run: what is cached is served from disk.
   It exits 1 and lists what failed; if two targets in a row cannot list visits it concludes IRSA is
   down and stops (IRSA often answers 502/503: try again in a few minutes).
   The summary warns when the demo set does not fit `PLANETX_CACHE_MAX_MB`. Do not lower the cap.
3. Warm the *running* server too (see section 1): `uv run python tools/prewarm.py --base http://127.0.0.1:8000`.
   The visit lists (10 min) and finished motion runs (1 hour) live in the API process, so only a
   pass against the live server keeps them warm; the disk cache alone makes them take seconds
   instead of minutes. Do this 10 minutes before going on stage.
4. Record the backup video (section 6).

If the demo runs on Render, point prewarm at it (`--base https://<service>.onrender.com`). The
cache then fills on its persistent disk. Its cap is `PLANETX_CACHE_MAX_MB=6144` (render.yaml).

## 1. Start order

1. API (leave running; one worker process only, jobs live in its memory):
   ```
   export PLANETX_CACHE_DIR=$PWD/.cache/planetx PLANETX_CACHE_MAX_MB=4000
   uv run uvicorn planetx.api.app:app --port 8000
   curl http://127.0.0.1:8000/api/health        # {"status":"ok"}
   ```
2. Warm it: `uv run python tools/prewarm.py --base http://127.0.0.1:8000` (minutes when the disk cache is warm).
3. Frontend: `cd frontend && VITE_API_BASE=http://127.0.0.1:8000/api npm run dev` -> http://localhost:5173.
   A different port needs `PLANETX_CORS_ORIGINS=<that exact origin>` on the API.
4. Smoke test (section 5), then open the first URL below.

For the public site: backend on Render (docs/DEPLOY.md), frontend on GitHub Pages. See section 4.

## 2. Targets: shareable URLs and what to expect

Append to the site's address (`http://localhost:5173/` or the Pages URL). The page centres the
map there. Then click the middle of the map (selects the patch) and press **Find what moves**.
A first run on cached data takes seconds; on cold data one to two minutes (about 54 frames,
216 MB).

| Target | URL query | Expect (from CLAUDE.md "Engine results") |
|---|---|---|
| Barnard's Star | `?ra=269.4473&dec=4.7685&fov=0.25` | Mover: 10.45"/yr, position angle 358, about 6 sigma in single frames. The multi-observatory refit gives (-0.82, +10.36)"/yr, Gaia says (-0.80, +10.36). No false movers. Spectrum: saturated in every exposure (do not demo the spectrum here). Use this exact URL: the J2000 position is 4.5' off now. |
| Epsilon Indi B | `?ra=331.0984&dec=-56.8013&fov=0.25` | Mover: 4.65"/yr, PA 122, 31 sigma, parallax 0.36" (literature 4.7"/yr, PA 122, 0.28"). Stacks: 4.50"/yr, 10 sigma. Spectrum: methane and water, "suggests a cool brown dwarf". The system is Ba/Bb, unresolved. |
| WISE 0855 | `?ra=133.7578&dec=-7.2420&fov=0.25` | Faint: single frames may not show it; the stacked mode gives 7.05 +- 1.35"/yr, PA 276, 5.2 sigma (literature about 8.1). Spectrum: 3.3 um methane, but a G 16.3 star about 4" away supplies all the light below 2.5 um. |
| RW Aurigae | `?ra=76.9566&dec=30.4013&fov=0.25` | A brightness variable, not a mover. No false movers (not re-checked this round). |
| R Draconis | `?ra=248.1672&dec=66.7550&fov=0.25` | Variable (Mira, period about 245 d), not a mover; inside the first survey region near the North Ecliptic Pole. The website samples only about 76 frames, so its variability may be weak. |

(The positions are the website's own targets moved to the current year, as `tools/prewarm.py --dry-run`
prints them. The map also finds them by name in the search box.)

Suggested story: Barnard's Star (the obvious mover, with the telescope-pictures time-lapse) ->
Epsilon Indi B (brown dwarf, spectrum) -> R Draconis (a variable is not a mover) -> candidates list
(survey) -> Results page. Always say "candidate", never "discovery".

## 3. If IRSA is down

IRSA answers 502/503 often, and was returning 502 on 2026-11-14 for over 20 minutes. What happens:

- **Works without IRSA**: the cutout files already on disk (`GET /api/cutout.png|json` for a frame URL
  that is cached), the 2MASS/WISE pictures already cached (`/api/motion/evidence/surveys`), and
  motion runs, evidence and spectra the server still holds in memory (about 1 hour; lost on restart).
- **Does not work**: `/api/visits`, `/api/frames`, a *new* `/api/motion` or `/api/spectrum` run. They all
  list the exposures first with a SIA query, and that list is cached **in memory for 10 minutes only**
  (`backend/planetx/spherex/archive.py`, `SIA_CACHE_TTL_S`). Confirmed on 2026-11-14: a fresh API with a
  warm cutout cache answered `/api/visits` with 502. Gaia, SIMBAD, CatWISE and JPL (the verdict step)
  are not cached on disk either.
- So **today the app cannot be served from cache alone**. In the demo: keep the already-open pages
  (the finished motion result stays on screen), skip to the next target you have already run, and
  fall back to the backup video.
- **Smallest change to fix it** (backend owner, about 30 lines plus a test): make the visit list
  stale-if-error and persistent. In `archive.query_visits`, after a successful query write the
  list as JSON to `settings.cache_dir / "sia" / <sha1 of the key>.json` (atomic write like
  `cutouts._atomic_write`; the key is already `(ra, dec, radius)` rounded). In the same function,
  wrap the collection loop in `try/except UpstreamError`: if that file exists, return its visits
  (log a warning, do not refresh the in-memory timestamp), else re-raise. With that, anything
  `tools/prewarm.py` has run is served with IRSA unreachable, except the catalogue verdict, which
  degrades to the engine-only result (`PLANETX_OBSERVATORIES=spherex` skips those services; use
  it on stage when the network is poor). Not implemented here: backend code is owned elsewhere.

## 4. Only the repo owner can do (GitHub Pages)

Pages cannot be enabled by a workflow. In the repo: **Settings -> Pages -> Build and deployment ->
Source = GitHub Actions**. Then:

1. Settings -> Secrets and variables -> Actions -> Variables: `VITE_API_BASE` = `https://<service>.onrender.com/api`.
2. Actions -> "Deploy frontend to GitHub Pages" -> Run workflow (it also runs on every push to
   `main` that changes `frontend/`).
3. The site is `https://ronith-neelam1429.github.io/MongooseInSpace_SPHEREx-PlanetX/`. The Render service's
   `PLANETX_CORS_ORIGINS` must be `https://ronith-neelam1429.github.io` (origin only, no path).
4. Open it, run the smoke test against it (section 5) and `curl https://<service>.onrender.com/api/health`.

Render itself: docs/DEPLOY.md. A starter instance has 512 MB of RAM and the engine analyses about 54
frames at once: if "Find what moves" ends with the service restarting, move to a larger plan.
That is the repo owner's call (cost); it was not measured here.

## 5. Smoke test (run before the pitch)

`tools/demo_walkthrough.cjs` drives a real Chrome through the demo (health check, home, search
suggestions, then for each target: open the shared URL, select the patch, Find what moves, check the
result text and that no error banner shows), saves a screenshot per step and exits 1 on any failed step
or API 5xx.

```
NODE_PATH=<a node_modules containing playwright-core> \
  node tools/demo_walkthrough.cjs --site http://localhost:5173 --api http://127.0.0.1:8000 --targets barnard
```

Needs Google Chrome installed and `playwright-core` (not a project dependency: `npm i --no-save
playwright-core` in a scratch directory and point `NODE_PATH` at its `node_modules`). Screenshots
go to `tools/out/walkthrough/`. Its text checks are loose on purpose (the page is still changing); a
red step means look at the screenshot.

## 6. Backup video checklist

- [ ] Prewarmed API and frontend running as in section 1, a fresh browser window, 1920x1080, zoom 100%,
      notifications off, other tabs closed, dark room mode off.
- [ ] Network ON (a recording that needs IRSA can stall): run the walkthrough first so everything is in memory.
- [ ] Screen recorder (QuickTime: File -> New Screen Recording) with the microphone on; do a 10 s test clip
      and play it back with sound.
- [ ] Record in this order, one URL each, narrating: Barnard's Star (time-lapse, Find what moves, the
      "Is the motion real?" checklist), Epsilon Indi B (spectrum), R Draconis (variable, not a mover),
      candidates list, Results page. Say "candidate", never "discovery".
- [ ] Pause 2 s on each result so it can be read; do not scroll fast.
- [ ] Keep the clip under 3 minutes; export H.264 MP4. Save to disk, to a USB stick and to cloud storage.
- [ ] Watch it once through with sound; open the file on the presenting laptop.
- [ ] Put the same screenshots from `tools/out/walkthrough/` into the slides as a last resort.
