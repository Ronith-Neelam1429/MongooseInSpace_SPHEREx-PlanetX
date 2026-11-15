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

## Engine results (real data, cached cutouts, 2026-11-14 evening)
- Single frames: Barnard 10.45"/yr PA 358, 6.0 sigma; Eps Indi B 4.65"/yr PA 122, 31 sigma, parallax 0.36" (lit 4.7/122,
  0.28"); R Dra variable; no false movers on Barnard, Eps Indi B, R Dra, WISE 0855, NEP (RW Aur not rechecked).
- Stacks (per pass and detector): WISE 0855 7.05 +- 1.35"/yr PA 276, 5.2 sigma (lit ~8.1); Eps Indi B 4.50"/yr 10 sigma.
- Position errors are refitted on 361 static tracks (`tools/calibrate_errors.py`) and then calibrated per field and
  per SNR band (<15, 15-40, >=40) from that field's static stars (`EngineResult.position_error_scale`, notes).
  Static-star chi2/dof ~0.9-1.3 on most fields.

## Engine known problems (still open)
- Faint static 3-pass tracks can show large apparent motions (e.g. 5.5"/yr at 10 sigma, chi2 fine); they stay
  non-movers only via the 1 px bar. Suspect photocentre shifts of blended pairs.
- NEP single-frame errors still too small (static chi2/dof ~1.9).
- Stack mode does no variability check (one stack per pass and detector gives too few pairs; needs forced photometry).
- Stack epochs are frame-weighted, not weighted by a red object's own flux; parallax from 3 passes is unreliable
  (a 1-dof parallax fit no longer removes the line fit's chi2 inflation).
- Saturated-star floor (SATURATED_PASS_FLOOR_PX) and wing annuli not refitted (too few saturated static stars).
- The website samples ~76 frames per field: too few matching-wavelength pairs for R Dra's variability.
- Guards added this round (keep their tests): blended pass needs blend evidence (blocker seen in >= 2 other passes
  and brighter by >= half the mover's flux); a 2-measured-pass track with a pass seen in < half its frames at
  SNR < 10 cannot be a mover; 2-pass movers need 10 sigma.

## Engine in the website (2026-11-14)
- Every object card opens with telescope pictures oldest first (2MASS ~2000, WISE ~2010, SPHEREx passes), with
  two zooms ("SPHEREx passes" / "25 years"); catalogued stars get pictures before any engine run
  (`/api/motion/evidence/surveys`, `/api/frames`, `/api/cutout.png`, loaded lazily). Then the verdict, the
  "Is the motion real?" checklist, the position chart, and a "Show spectrum" section (`GET /api/spectrum`).
- "Find what moves" polls `GET /api/motion` (about 54 frames, ~216 MB from IRSA the first time); it only runs on request,
  also from the candidates list ("Run the engine here").
- Barnard's Star test URL: `?ra=269.4473&dec=4.7685&fov=0.25` (the J2000 position 269.452/4.693 is 4.5' off now
  and falls outside the engine's field).
- Dev servers on non-default ports need `PLANETX_CORS_ORIGINS` set to the page's exact origin.

## Spectra (`backend/planetx/spherex/spectrum.py`, `GET /api/spectrum`)
- Forced aperture photometry (1.5 px, EPSF aperture correction) following proper motion, binned to 102 bands, flux in
  mJy; methane (1.6, 2.2, 3.3 um) and water (1.4, 1.9, 2.7 um) depths. Wording: "suggests", never "is".
- Live: a plain star matches 2MASS to ~5% with no false dips; Eps Indi B shows methane and water ("suggests a cool
  brown dwarf"); WISE 0855 shows 3.3 um methane but a G 16.3 star ~4" away supplies all its light below 2.5 um
  (no neighbour check yet); Barnard is saturated in every exposure.

## Known-asteroid check (`backend/planetx/classify/`)
- SkyBoT takes only MPC codes (`-observer`; astroquery's `location` sends `-loc`, which SkyBoT ignores; SPHEREx's
  code 288 answers as 500). JPL Horizons has SPHEREx as body -163182, so SkyBoT's geocentric positions are moved
  to SPHEREx's position per pass (`classify/observer.py`, cached per day). JPL SBIdent takes `xobs` but is unused.
  Horizons down -> radius widened by the worst-case parallax, a note, and a 10 min back-off. Stacked runs skip it.
- An object near a slow Gaia star is static when its motion agrees with the star's; otherwise it is named "near Gaia DR3 ...".

## Open questions
- Whether the DR1 all-sky cube (HiPS order-5 tiles) can be used directly as an Aladin sky map.

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
- Real data (live 54-frame Barnard sample): Barnard refits to (-0.82, +10.36) +- 0.03 arcsec/yr (Gaia -0.80, +10.36).
- Under 3 sigma in SPHEREx, confirmation needs secure (unlikely-by-chance) epochs from two independent survey
  families (2MASS vs WISE; AllWISE+CatWISE share exposures and count as one). A significant one-family motion sets
  `FusedMotion.archive_candidate` (API `ArchiveCheck.candidate`): shown as a candidate, never confirmed.
  `ArchiveCheck.notes` says why. This stopped the Barnard-halo false "confirmed" mover.
- False movers seen on the way and the guards that stop them: a faint bystander inside a loose first search
  area (chance-alignment test, brightness-ranked); neighbours resolved by 2MASS/WISE inside one blended SPHEREx
  pixel (motion needs > 6.2" total shift; a single archival epoch confirms only if SPHEREx alone sees >= 3 sigma).
- Usage needs both this package and the engine: `refine_objects(find_motion(frames).objects)`.
