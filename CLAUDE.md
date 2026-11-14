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
- Connection tests (live network): `uv run pytest tests/connections -v`

## Layout
- `tests/connections/`: live tests proving each external data service works
  (`conftest.py` holds the Barnard's Star reference fixture; `test_spherex.py`; `test_catalogs.py`)

## Plan / timeline
Status: step 1 (connecting to the datasets and testing the connections) in progress on branch `setup/data-connections`.
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

## Open questions
- Cutout `size` units (assumed degrees) and the SPHEREx file layer names: confirm from the connection tests.
- Whether the DR1 all-sky cube can be used directly as an Aladin sky map.

## Multi-observatory motion (`src/planetx/observatories/`)
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
