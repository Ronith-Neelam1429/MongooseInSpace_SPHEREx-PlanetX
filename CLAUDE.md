# SPHEREx PlanetX (NASA Space Apps)

Public web tool for the NASA Space Apps SPHEREx challenge. Users pick a patch of
sky and see SPHEREx images of it as a time-lapse to spot moving and variable
objects. An offline survey crawler looks for motion automatically and checks
detections against other telescopes' catalogs.

Repo: https://github.com/Ronith-Neelam1429/MongooseInSpace_SPHEREx-PlanetX

## Goals
1. **Explore**: full-sky map; search by RA/Dec or object name; SPHEREx time-lapse of the
   patch. Coordinates in the URL (`?ra=&dec=&fov=`) so views can be shared and embedded.
2. **Compare**: blink, stack, and difference frames; choose field of view and wavelength
   band; combine bands per detector (6 detectors, 0.75-5.0 um, 102 bands) for
   better signal-to-noise.
3. **Survey on its own**: offline crawler over HEALPix sky patches, starting at the
   North Ecliptic Pole (RA 18h, Dec +66.56 deg), where SPHEREx covers the sky most often,
   and spiraling outward. It saves progress and resumes after a restart.
4. **Detect**: find sources in each frame, match them across frames, fit motion and
   keep only motion larger than the measurement error. Also flag brightness changes and
   mask artifacts using SPHEREx quality flags.
5. **Verify**: check against Gaia DR3 proper motions projected to each frame's date,
   CatWISE2020, 2MASS, SIMBAD, and SkyBoT/JPL SBIdent (known asteroids).
6. **Classify and score**: known moving star / known asteroid or comet /
   variable star / artifact / **unknown candidate**. Always say "candidate", never "discovery".
7. **Bonus**: spectra on the fly (methane and water absorption suggests a brown dwarf),
   light curves, user voting on candidates, automatic re-run when new data is released.

## Physical constraints (these drive the design)
- SPHEREx pixels are about 6.2 arcsec. It covers the whole sky every 6 months, starting May 2025, so most spots
  have only 3-4 visits. Stars move only a few pixels, so the pipeline needs sub-pixel alignment and centroids.
  The ecliptic poles get far more visits.
- Epsilon Indi Ba/Bb (<1" apart) are not resolved; show the system's motion and spectrum.
- RW Aur and R Dra are *brightness* variables (R Dra is a Mira, period about 245 days), not movers.

## Test targets
Barnard's Star; Epsilon Indi B; WISE 0855 (08h55m10.831s, -07d14m42.53s);
RW Aurigae; R Draconis (also lies inside the first survey region near the NEP).

## Data sources
SPHEREx data comes from IRSA via SIA2 at `https://irsa.ipac.caltech.edu/SIA` (astroquery `Irsa.query_sia`):
- `spherex_dr1` (Wide, main source), `spherex_dr1_deep`, `spherex_dr1_cube` (all-sky cube)
- `spherex_qr3`, `spherex_qr3_deep` (Quick Release data newer than DR1)
- QR2 is no longer searchable; don't use it. SIA results lag new data by about a day.
- Cutouts: append `?center=RA,Dec&size=...` to a file's access URL. Full files are
  several hundred MB, so always use cutouts.
- pyvo/astroquery SIA queries have **no timeout** and can hang (e.g. 10' cones). Use small cones
  (~1') and `maxrec`, and wrap calls in your own timeout. IRSA often returns 502/503: retry only on 5xx.
- Each cutout is about 4 MB even when tiny (an EPSF table dominates the file). `size` is in degrees.

Verification and context:
- Gaia DR3 (ESA TAP, `astroquery.gaia`)
- VizieR: CatWISE2020 `II/365`, 2MASS `II/246`
- SIMBAD (name lookup)
- SkyBoT (`astroquery.imcce`) / JPL SBIdent / JPL Horizons
- CDS HiPS (AllWISE, 2MASS color) for sky map backgrounds

Docs: IRSA SPHEREx overview https://irsa.ipac.caltech.edu/data/SPHEREx/docs/overview_qr.html,
archive guide https://caltech-ipac.github.io/spherex-archive-documentation/,
tutorials https://caltech-ipac.github.io/irsa-tutorials/spherex-intro

## Tech stack
- Frontend: Aladin Lite v3 sky map, React + Vite (or plain JS), canvas for the player, Plotly.js for charts
- Backend: Python 3.12+, FastAPI, astropy, astroquery, pyvo, reproject, photutils/sep, numpy
- Survey: Python worker process; astropy-healpix (patch order); mocpy (coverage overlay);
  SQLite (patch queue + candidates); rate limiting and retries against IRSA
- Hosting: backend and worker on Render, Fly.io or HF Spaces; frontend on GitHub Pages or Vercel

## Commands
- Install: `uv sync`
- Offline tests (what CI runs): `uv run pytest -m "not network" -q`
- Live tests (network): `uv run pytest -m network -q`; one part: e.g. `uv run pytest tests/api -m network`
- API server: `uv run uvicorn planetx.api.app:app --reload` (docs at http://127.0.0.1:8000/docs)
- Frontend: `cd frontend && npm install && VITE_API_BASE=http://127.0.0.1:8000/api npm run dev` (http://localhost:5173)
- Engine dev viewer: `uv run python tools/devview.py barnard --open` (also `wise0855 --stack`, `eps-indi-b`,
  `r-dra --per-pass 30`, `rw-aur`, a SIMBAD name or "ra dec"); writes `tools/out/<target>.html`
- Survey: `uv run python -m planetx.survey init|status|run --processor <name>|requeue-failed`
- Settings (env vars, see `backend/planetx/config.py`): `PLANETX_CACHE_DIR`, `PLANETX_CACHE_MAX_MB` (default 2048),
  `PLANETX_DB`, `PLANETX_CORS_ORIGINS`, `PLANETX_OBSERVATORIES`

## Layout
`frontend/` website (see `frontend/README.md`); `backend/planetx/` all Python code (import name `planetx`);
`tests/<part>/` mirrors the backend parts; `tools/` dev scripts; `docs/`.
- `backend/planetx/spherex/archive.py`: SIA2 queries (dr1 + qr3), IRSA URL allow-list, cutout download with timeouts/retries
- `backend/planetx/spherex/cutouts.py`: capped disk cache, resampling onto one north-up grid per position, PNG/float rendering
- `backend/planetx/api/`: FastAPI app; `schemas.py` is the frontend contract (guide: `docs/API.md`)
- `backend/planetx/api/motion.py`: `/api/motion` background runs (visit sample -> cutouts -> `find_motion` ->
  `refine_objects`), polled by the frontend's "Motion engine" panel section (`runMotion` in `frontend/src/lib/services.js`)
- `backend/planetx/survey/`: HEALPix crawl order from the NEP, resumable SQLite patch queue, worker + CLI
- `backend/planetx/engine/`: motion engine, pure functions (no network). `frame_from_hdul(hdul)` -> `Frame`;
  `find_motion(frames, stack=False)` -> `EngineResult` (.movers, .variables, .objects, .notes).
  `detect.py` per-frame sources (saturated stars located from their OVERFLOW/BLOOM patch); `motion.py` passes,
  alignment, linking, astrometric fit (parallax when it explains a misfit), variability at matched wavelengths;
  `stack.py` per-pass co-adds for faint objects
- `backend/planetx/observatories/`: archival cross-check (`catalogs.py` IRSA Gator queries, `fusion.py` refit)
- `tools/`: engine dev tools only (`fetch.py` cached cutout fetcher, `devview.py` HTML blink viewer)
- `tests/`: `api/ spherex/ engine/ observatories/ survey/` per part; `services/` live checks that each external
  data service works. Live tests are marked `network`. Shared helpers sit in `tests/` (`conftest.py` keeps offline
  tests off the network, `synthetic.py` fake SPHEREx files, `targets.py` reference positions).

## Key facts (verified live)
- Cutout `size` is in degrees. Cutout HDUs: PRIMARY, IMAGE, FLAGS, VARIANCE, ZODI, EPSF, WCS-WAVE.
- SIA `em_min/em_max` are metres. `obs_id` is shared by the 6 detectors of one exposure: key frames by
  `access_url` or (obs_id, detector).
- Bright-star cores (e.g. Barnard's) carry BLOOM/SUR_ERROR flags: display masks only NONFUNC, MISSING_DATA,
  HOT, COLD; photometry should use `planetx.spherex.cutouts.PHOTOMETRY_BAD_FLAGS`.
- ~360 visits at Barnard's Star in 3 passes; each first-time cutout is ~4 MB from IRSA.

## Plan / timeline
Status: hackathon in progress. Step 1 (data connections) done, all services reachable (CDS through mirrors).
Work lands on `main` via feature branches (data connections, backend API, survey queue, motion engine, frontend).
1. Before the hackathon: data connections tested; notebook showing Barnard's Star moving; FastAPI + Aladin skeleton.
2. Hackathon (48 h):
   - 0-4: roles and API contract
   - 4-14: cutout/alignment API, sky map, search, URL params, player
   - 10-18: single-patch engine on Barnard's Star and R Dra
   - 18-22: crawler; **start it running by hour ~20 and leave it running**
   - 22-32: catalog verification and classification, difference/stack, Gaia overlays, all 5 targets
   - 32-40: coverage overlay, candidate list page, spectra
   - 40-45: deploy, fix bugs, backup demo video
   - 45-48: pitch
3. Scope rule: after hour 24, only must-have features; cut from the bottom of the list.

## Verified facts (connection tests, 2026-11-14)
- SPHEREx MEF layers: IMAGE (MJy/sr, TAN-SIP WCS), FLAGS, VARIANCE, ZODI, EPSF, WCS-WAVE. The header has
  MJD-AVG/DATE-AVG. Get wavelength per pixel with `WCS(hdul["IMAGE"].header, fobj=hdul, key="W")`,
  which returns (wavelength, bandwidth) in um.
- Barnard: DR1 has 2 passes (Aug-Oct 2025, Mar-May 2026); QR3 has a 3rd (Aug-Sep 2026). DR1 and QR3 share **no**
  frames, so merging them doesn't double-count. Measured star position agrees with the Gaia-propagated one to 0.2 px.
- Barnard is effectively missing from CatWISE2020 (saturated), and its halo has fake movers. Use CatWISE
  for fainter objects (WISE 0855 gives ~8"/yr). The VizieR table is `II/365/catwise`, with PM in arcsec/yr.
- CDS primary hosts (simbad/vizier/alasky.cds.unistra.fr) returned 404 on 2026-11-14. Use mirrors:
  `Simbad().server = "simbad.harvard.edu"`, `Vizier(vizier_server="vizier.cfa.harvard.edu")`, HiPS mirrors at
  irsa.ipac.caltech.edu / skies.esac.esa.int. The HiPS IDs are `CDS/P/allWISE/color` and `CDS/P/2MASS/color`
  (alasky paths `AllWISE/RGB-W4-W2-W1` and `2MASS/Color`).
- python.org CPython has no CA bundle: set `SSL_CERT_FILE=certifi.where()` at app startup.
- astroquery caches responses, including errors, for a week. Disable the cache for anything that checks service health.
- JPL SBIdent is slow (~20 s per call). SkyBoT is ~4 s. Gaia is ~7 s.

## Engine results (real data, 2026-11-14)
- Barnard 10.45"/yr PA 358 (Gaia 10.39/356); Eps Indi B 4.67"/yr PA 122, parallax 0.36±0.18" (lit 4.7/122,
  0.28"); R Dra variable (P3/P1 0.56); no false movers on these fields.
- WISE 0855 only in per-pass stacks: 8.9"/yr PA 284 but 3.2 sigma (stack centroid errors underestimated), so it's
  a "candidate", not a mover.
- Constants tuned on these few targets (SATURATED_PASS_FLOOR_PX, PASS_FLOOR_ARCSEC, centroid sigma FWHM/SNR,
  wing annuli): re-derive from many static stars before trusting survey-wide significances.

## Engine known problems (from review; fix before the survey crawler relies on it)
- Passes split on > 60-day gaps: near the ecliptic poles (where the crawl starts) SPHEREx revisits continuously,
  so everything becomes one pass and nothing is measured. Bin by time windows instead.
- Linking is O(n^2) in unlinked sources with per-call SkyCoord transforms: minutes per crowded field.
  Vectorise offsets once and use scipy cKDTree.
- Stack mode: one co-add across detectors dilutes red objects; stacks carry no wavelength, so no variability.
- A mover blending with a field star in one pass is rejected via the missing-pass check.
- Variability pairs overlap (one detection in many pairs), which overstates significance in deep fields.
- Results depend on input order (reference frame tie-break): sort frames before calling `find_motion`.
- Real-target tests resolve names via SIMBAD each run (not fully offline).

## Engine in the website (2026-11-14)
- "Run engine" in the field panel polls `GET /api/motion`; results are drawn in cyan on the sky (`StarField.setMotion`),
  Gaia's catalogued motion stays red. The engine runs only on request (about 54 frames, ~216 MB from IRSA the first time).
- Live on Barnard's Star (54 frames, detectors 1-3): mover 9.91"/yr PA 2, 5.1 sigma; 281 tracks, 1 candidate.
- Seen there: a second "mover" that SPHEREx alone puts at 1.4 sigma but AllWISE+CatWISE "confirm" at 2.0"/yr.
  Likely a false match in Barnard's halo (see the CatWISE note above); the fusion guards need a look.
- `engine/frames.py` evaluates WCS-WAVE without dropping the SIP cards (`spherex/cutouts.py` drops them): astropy logs
  an INFO per frame and the frame wavelength may be slightly off.

## Open questions
- Whether the DR1 all-sky cube (HiPS order-5 tiles) can be used directly as an Aladin sky map.
- Observer location: Horizons/SkyBoT/SBIdent tests use geocentric (500). Near-Earth objects need SPHEREx's own orbit as the observer location.

## Multi-observatory motion (`backend/planetx/observatories/`)
- `refine_objects(engine_result.objects, observatories=...)` refits each SPHEREx track together with archival
  positions: 2MASS (~2000), AllWISE (2010.5), CatWISE2020 (2015.4), optionally NEOWISE single exposures (to 2024).
- Toggle: `observatories="spherex"` (SPHEREx only, no network) | `"multi"` (default) | `"all"` (+NEOWISE) | comma list.
  Default comes from env `PLANETX_OBSERVATORIES`. The engine itself stays SPHEREx-only and network-free.
- Verdicts: confirmed / stationary / refuted / unconfirmed / spherex_only. `FusedMotion.is_mover` is None when the
  archive has no say, so keep the engine's own flag then.
- Catalogs come from IRSA Gator (`/cgi-bin/Gator/nph-query`), ~1.5 s per cone. IRSA TAP timed out (>90 s) on
  `catwise_2020` and `neowiser_p1bs_psd` on 2026-11-14. Gator reports errors as HTTP 200 with a `[struct stat="ERROR"` body.
- NEOWISE single exposures near the NEP: ~45,000 rows in a 20" cone, ~1 min. Hence opt-in, per mover candidate, 30" cone.
- CatWISE lists a fast mover as several entries (WISE 0855: three within 1.6" at the 2015.4 position); same-catalog
  entries within 2" count as one object. Barnard's Star has no usable AllWISE/CatWISE entry (saturated); 2MASS carries it.
- Real data (engine output on the cached Barnard cutouts, 163 frames, 95 tracks): Barnard goes from
  (-0.48, +10.42) +- 1.77 to (-0.820, +10.362) +- 0.029 arcsec/yr (Gaia: -0.80, +10.36); 83 tracks stationary,
  11 unconfirmed, no false movers. RW Aur field: 50 stationary, 2 unconfirmed, no movers.
- False movers seen on the way and the guards that stop them: a faint bystander inside a loose first search
  area (chance-alignment test, brightness-ranked); neighbours resolved by 2MASS/WISE inside one blended SPHEREx
  pixel (motion needs > 6.2" total shift; a single archival epoch confirms only if SPHEREx alone sees >= 3 sigma).
- Usage needs both this package and the engine: `refine_objects(find_motion(frames).objects)`.
