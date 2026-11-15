# Presentation notes: SPHEREx PlanetX

Running collection of material for the pitch. Last updated 2026-11-15 about 03:30 (everything merged to main; added classification, evidence checks, survey candidates, website, CI/deploy and ideas for standing out).

Tags: **[verified]** = measured by our own live tests. **[project]** = from our design
notes (CLAUDE.md). **[check]** = general background knowledge, confirm against the source
before putting it on a slide.

---

## 1. One-line pitch

> Pick any patch of sky and watch SPHEREx's images of it as a time-lapse. Our crawler
> searches the sky for things that move or change, then checks every detection against
> Gaia, WISE, 2MASS, SIMBAD and the known-asteroid databases. Anything left over is an
> **unknown candidate**.

Wording rule: always say "candidate", never "discovery". **[project]**

## 2. The problem / why it matters

- SPHEREx maps the whole sky every 6 months, in 102 colors, from 0.75 to 5.0 µm. **[project]**
- It was built for cosmology and ices, not for finding things that move. Nobody is
  systematically blinking its frames to look for motion. That is the gap we fill.
- Things that move or change in the infrared:
  - nearby cool brown dwarfs (high proper motion, faint in visible light)
  - asteroids and comets
  - variable stars (Miras, young stars)
  - hypothetical distant planets ("Planet X")
- It's a public tool: shareable URLs (`?ra=&dec=&fov=`) let anyone point at a spot and look. **[project]**

## 3. SPHEREx quick facts (background slide)

| Fact | Value | Tag |
|---|---|---|
| Wavelength range | 0.75–5.0 µm | project |
| Spectral bands | 102 (6 detectors) | project |
| Pixel scale | ~6.15–6.2″ per pixel (we measured 6.15″) | **verified** |
| All-sky cadence | Full sky every ~6 months, survey started May 2025 | project |
| Visits per spot | Usually 3–4; ecliptic poles get far more | project |
| Launch | March 2025, Falcon 9 (rideshare with PUNCH) | check |
| How it gets colors | Linear variable filters: wavelength changes across the detector | **verified** (WCS-WAVE table) |
| Data archive | NASA/IPAC IRSA (SIA2 service) | verified |

## 4. Headline result so far: we can see Barnard's Star move in real SPHEREx pixels **[verified]**

This is the first proof-of-concept slide. **The headline number is now the 60× precision gain from fusion (section 8c): ±1.77 → ±0.029″/yr, matching Gaia.**

- Barnard's Star has the largest proper motion of any star, about 10.4″/yr (Gaia DR3 total of about 10,393 mas/yr).
- We downloaded a real SPHEREx DR1 cutout from **2025-08-15** and propagated
  Gaia's 2016 position forward to that date.
- The brightest source is **0.20 px from the predicted position**, and
  **16.2 px away from where the star was in 2016**.
- It is a very strong detection: peak 1034 MJy/sr against a background of 0.47 (MAD-sigma 0.08).
- At that pixel, the per-pixel wavelength table says the image was taken at **4.92 µm**
  (bandwidth 0.037 µm). That is a mid-infrared slice, which shows the color can be found for every pixel.

**Talking point:** "Our pipeline predicted where the star should be to a fifth of a pixel,
and it was there."

## 5. Why this is hard: the physics constraint **[verified math + project]**

- Even the fastest star moves about **1.7 px per year** in SPHEREx (10.4″/yr ÷ 6.15″/px).
- WISE 0855 (the coldest known brown dwarf) moves about 8″/yr, or **1.3 px/yr**.
- Most spots have only 3–4 visits. So we need **sub-pixel alignment and centroids**,
  and a motion fit that keeps only motion **larger than the measurement error**.
- Some sources are unresolved. Epsilon Indi Ba/Bb are less than 1″ apart, so we show the system's combined motion and spectrum.
- Brightness variables look like change but don't move. RW Aur and R Dra (a Mira with a period of about 245 days) are our checks for
  correct classification as *variable* instead of *mover*.

## 6. Data baseline at Barnard's Star **[verified]**

| Collection | Dates | Distinct dates |
|---|---|---|
| DR1 pass 1 | 2025-08-15 → 2025-10-01 | 15 |
| DR1 pass 2 | 2026-03-11 → 2026-05-05 | 11 |
| QR3 (newer quick release) | 2026-08-16 → 2026-09-22 | 14 |

- DR1 plus QR3 gives **3 separate passes over about 13 months**.
- The two releases share **zero frames** (0 shared obs_id: DR1 has 122, QR3 has 62), so merging them
  adds new data with no duplicates.
- Talking point: "Every new data release makes the time-lapse longer and the motion clearer."
  That is why we planned automatic re-runs when new data comes out.

## 7. Verification sources: every service tested live against a known answer **[verified]**

| Service | What we use it for | Known-answer test |
|---|---|---|
| IRSA SPHEREx (DR1, DR1 deep, DR1 cube, QR3, QR3 deep) | images, cutouts | Barnard's Star in pixels; deep fields at the NEP |
| Gaia DR3 (ESA) | proper motions projected to each frame's date | finds Barnard's Star and its motion |
| CatWISE2020 (VizieR `II/365/catwise`) | infrared proper motions | recovers WISE 0855 at about 8.0″/yr (literature value about 8.1″/yr) |
| 2MASS (VizieR `II/246`) | near-IR positions from about 2000 | finds Barnard near its J2000 position |
| SIMBAD | name lookup | Barnard's Star, WISE 0855 |
| SkyBoT and JPL SBIdent | known asteroids at a date and place | both find Ceres |
| CDS HiPS (AllWISE, 2MASS color) | sky-map backgrounds | served by at least one mirror |

- Suite status: SPHEREx **12/12 passed**. Catalogs **14 passed, 8 expected failures (xfail)**. All 8 xfails are
  because the main CDS servers (Strasbourg) were returning 404 on 2026-11-14. Their mirrors work,
  and the tests fall back to them automatically.
- Nice detail for judges: **CatWISE cannot see Barnard's Star** (too bright and saturated; the nearest
  source is 21″ away). That is exactly why we combine several catalogs instead of trusting one.

## 8. Pipeline (architecture slide) **[project]**

```
Sky map (Aladin Lite)  ->  pick patch / search by name or RA,Dec
        |
FastAPI backend  ->  IRSA SIA2 query -> small cutouts (never full ~100s-MB files)
        |
Per patch: detect sources -> match across frames -> sub-pixel centroids
           -> fit motion, keep only significant motion
           -> brightness-change flags; mask artifacts with SPHEREx quality FLAGS
        |
Verify: Gaia DR3 (propagated to frame date), CatWISE2020, 2MASS, SIMBAD, SkyBoT/SBIdent
        |
Classify: known moving star | known asteroid/comet | variable | artifact | UNKNOWN CANDIDATE
```

- **Survey crawler:** goes through HEALPix sky patches, starting at the **North Ecliptic Pole**
  (RA 18h, Dec +66.56°), where SPHEREx revisits most often, and spirals outward. It saves progress and
  resumes after a restart (SQLite queue).
- **Compare tools:** blink, stack and difference frames; pick the wavelength band; combine
  bands on each detector for better signal-to-noise.
- **Bonus:** spectra on the fly (methane and water absorption suggests a brown dwarf), light curves,
  user voting on candidates.

## 8a. Project status (updated 2026-11-15, about 03:30)

**Everything is merged on `main`** (PRs #1–#5 merged; commit `7d05747`), and GitHub CI is green on it.
Offline backend tests: **475 passed** (I ran this myself on `main`, 113 s). Frontend: **128 tests
pass**, and lint and build are clean (reported by the session that merged them).

Code layout now: `backend/planetx/` (Python), `frontend/` (website), `tools/` (engine dev
viewer), `tests/<part>/`. Commands are in `CLAUDE.md`.

| Part | Where | What it does |
|---|---|---|
| SPHEREx data access | `backend/planetx/spherex/archive.py`, `cutouts.py` | SIA2 queries (DR1 + QR3), IRSA-only URL allow-list, timeouts/retries, disk cache capped at 2 GB, resampling onto one north-up grid |
| Motion engine | `backend/planetx/engine/` | detection → passes/windows → alignment → linking → astrometric fit (+ parallax) → variability (8b) |
| Archive fusion | `backend/planetx/observatories/` | refits motion with 2MASS/AllWISE/CatWISE/NEOWISE positions; also fetches 2MASS/WISE *images* for the evidence view (8c) |
| Catalogue check / classification | `backend/planetx/classify/` | names and classifies every object with Gaia, SIMBAD and SkyBoT (8d) |
| Shared field pipeline | `backend/planetx/pipeline.py` | `analyse_field`: one code path used by both the website and the survey |
| API | `backend/planetx/api/` | `/api/visits`, `/frames`, `/cutout.*`, **`/api/motion`** (background engine runs), **`/api/motion/evidence`** (+ `/surveys`), **`/api/candidates`**, `/api/survey/*` |
| "Is the motion real?" checks | `backend/planetx/api/evidence_checks.py` | 7 independent tests per mover (8e) |
| Survey crawler | `backend/planetx/survey/` | HEALPix crawl from the NEP, resumable SQLite queue, **`motion` processor**, **candidates table with scores** (8f) |
| Website | `frontend/` | SPHEREx all-sky globe, Gaia star field with animated motion, "Find what moves", evidence viewer, candidates panel, class filter (8g) |
| CI / deploy | `.github/workflows/ci.yml`, `frontend-pages.yml`; deploy draft in an agent worktree | 8h |

**Sessions that did this work:** Motion engine integration, Object movement photo verification
(evidence plus the final merge to `main`), Mesh filter for object classification (class filter),
UI updates and telescope imagery roadmap, ML model for movement detection (advice only, no code), and
Next steps after dataset links (backend API and survey).

**Still not done / not merged:**
- Deploy files (`Dockerfile`, `render.yaml`, `deploy/`, `docs/DEPLOY.md`) exist **uncommitted** in agent
  worktree `.claude/worktrees/agent-ad86a2ca8867f3b75`.
- An uncommitted engine rewrite (+394/−203 lines in `motion.py`) sits in `agent-a236c0753aeb333a3`.
- **GitHub Pages deploy fails** because Pages isn't enabled for the repo: Settings → Pages → Source =
  GitHub Actions, then re-run the workflow. Only the repo owner can change this setting.

## 8b. How the motion engine works (the physics) **[verified by reading the code + our own run]**

Code: `backend/planetx/engine/` (`frames.py` → `detect.py` → `motion.py`, plus `stack.py`). Entry point
`find_motion(frames)`. It takes SPHEREx frames of one patch, in any order and from any detector, and
returns tracked objects with fitted proper motion, a mover flag and a variable flag.

**Key idea:** everything is done in *sky coordinates* (tangent-plane offsets in arcsec from
the patch centre), not pixels. Frames from different detectors, rotations and dates never
need to be resampled onto a common grid, which would blur sub-pixel positions.

### Step 0: turn a FITS file into a clean frame (`frames.py`)
- Reads IMAGE (surface brightness in MJy/sr), VARIANCE (per-pixel noise²), FLAGS, the WCS, and MJD-AVG (the time).
- **Bad pixels** are found by *name*, read from the FLAGS header (MSKN/MSKM keywords), so bit
  numbers are never hard-coded: OVERFLOW, BLOOM, NONFUNC, MISSING_DATA, HOT, COLD → set to NaN.
- Deliberately **not** masked: SOURCE (every star sets it), TRANSIENT/SUR_ERROR (set on bright
  star cores). Masking them would delete the stars we want.
- **Saturated stars** (OVERFLOW/BLOOM) are kept as a separate mask. Their pixel values are useless, but
  the *shape* of the saturated patch is centred on the star, so it still gives a position.
- Reads the per-pixel wavelength from WCS-WAVE (e.g. 4.92 µm at Barnard's Star in one frame).

### Step 1: find stars in each frame (`detect.py`)
1. **Background:** sigma-clipped median and standard deviation of usable pixels.
2. **Noise map** from the VARIANCE plane, with a floor of half the clipped scatter because the variance can
   be optimistic in faint pixels.
3. **Significance map** = (pixel − background) / noise. Pixels above **5σ** are grouped into
   connected blobs. A blob of a single pixel is thrown out as a cosmic ray or hot pixel.
4. **Sub-pixel centroid:** flux-weighted mean position in a **3×3 box** around the peak.
   SPHEREx's PSF is about 1.5 px FWHM, so 3×3 holds most of the light without picking up neighbours.
5. **Position error** = FWHM / (2.355 × SNR), with a floor of **0.05 px** however bright the star is
   (an undersampled PSF and leftover distortion limit how well any single frame can do).
6. **Saturated stars** are placed at the geometric centre of their saturated patch, with a 0.5 px error
   and an unknown flux. Faint "detections" in a saturated star's halo are dropped as fragments.
7. Pixel position → RA/Dec through the WCS, including SIP distortion terms.

### Step 2: group frames into passes
SPHEREx revisits a spot about every 6 months, and each visit lasts up to about 7 weeks. A **gap of more than 60 days
starts a new pass**. At Barnard's Star that gives 3 passes (Aug–Oct 2025, Mar–May 2026, Aug–Sep 2026). Near the poles,
where coverage never stops, a long run is now cut into windows of 60 days or less (see "Engine upgrades" below).

### Step 3: align frames
Small pointing and WCS errors would look like motion. The frame with the most stars is the
reference. For every other frame, each star is matched to the nearest reference star within 3″, and the
frame is shifted by the **median** offset. The median means a few genuinely moving stars can't drag
the alignment. At least 3 matched stars are needed, otherwise the frame is left unshifted with a note.

### Step 4: merge each pass into one position per object
Within one pass a star moves well under a pixel, so detections within 0.7 px are clustered
(brightest first, at most one detection per frame) into a **PassSource**:
- inverse-variance-weighted mean position
- error = the larger of the formal error and the measured scatter
- saturated objects get a 0.2 px floor, tuned on Barnard's Star against Gaia
- a median flux **per detector**, since each detector sees a different wavelength
- sources seen in fewer than 50% of the frames that covered the spot are dropped as noise flickers

Averaging dozens of frames per pass is how 6″ pixels give sub-arcsecond positions.

### Step 5: link passes into tracks
- **Static link:** a source within about 2″ (or 3σ) of the same spot in another pass is an ordinary
  star or galaxy.
- **Mover link:** sources seen in only one pass, *where the other passes did cover that spot*
  (so "missing" really means it isn't there any more, not that it fell off the edge), get paired across passes if:
  - the jump is ≤ **15″/yr** × the time between passes (+3σ). Barnard's Star, the fastest star, is about 10.4″/yr.
  - the brightness is similar: within a factor of 3 on a shared detector.
  - chains are extended through later passes while **a straight line still fits** (χ² ≤ 4).
  - longer chains win, then shorter jumps, which reduces the chance of a coincidental pairing.
- A final "grow" step attaches leftover single-pass sources that continue an existing
  track in a straight line, in case a fast star was linked in two passes and missed in a third.

### Step 6: fit motion and decide "mover"
- **Weighted least-squares straight line** of position against time, separately for east and north →
  proper motion (µα*, µδ) in ″/yr, its error, and χ²/dof.
- **Systematic floor:** the static stars *should* have zero motion, so their apparent
  motions measure the pipeline's own error. Their 2-D magnitude follows a Rayleigh distribution, so
  floor = median / √(ln 4). That floor is added to every object's error.
  (Barnard field: about 0.2″/yr.)
- **Mover** = motion ≥ **5σ** (against its own error plus the floor) **and** a total shift of
  more than **1 pixel** over the baseline.

### Step 7: decide "variable"
For each detector, take the largest change in per-pass flux between passes, with error =
measurement ⊕ **5% calibration**. **Variable** = ratio ≥ 1.25 and significance ≥ 5σ.
Each detector is compared only with itself, because each detector sees a different wavelength.

### Engine upgrades since the first version (all on `main`) **[verified from commits]**
- **Time windows for the poles.** Passes used to split only at gaps of more than 60 days. Near the ecliptic poles
  SPHEREx comes back every week or two, so everything was one pass and nothing could be measured, and that is exactly
  where the crawler starts. Continuous coverage is now cut into equal windows of 60 days or less. That keeps the fastest
  linked motion under 0.4 px inside a window. Real pole field (RA 270, Dec +66.56): **before, 1 pass and nothing
  measured; after, 9 windows, 26 tracked objects, floor 0.21″/yr, no false movers.**
- **About 3–5× faster.** Profiling showed about 2/3 of the time went to astropy coordinate transforms per source pair, so
  the engine now does that maths in plain numpy and finds neighbours with a scipy k-d tree. Results are identical to 1e-9″.
  54 frames: 15 s → 4.5 s. 147 frames: 37 s → 8 s.
- **Parallax.** When a straight line fits badly, the engine adds a parallax term and keeps it only if it is
  physical: positive and significant. Eps Indi B: **parallax 0.36 ± 0.18″** (literature 0.28″).
- **Per-pass stacking** (`stack.py`, `find_motion(frames, stack=True)`). It co-adds each pass's 10–40 frames into one
  deeper image to reach faint objects like WISE 0855. It's a dev/research mode; the website and the survey don't use it.
- **Variability at matched wavelengths.** Each detector is a rainbow (linear variable filter), so brightness is
  compared only between detections at the *same wavelength*. Brightness epochs are whole survey visits, not windows.
- **Order independence.** `find_motion` sorts its input by time, so results don't depend on the order frames arrive in.

### Engine results on the 5 test targets (real SPHEREx data) **[verified by the engine sessions, in CLAUDE.md]**

| Target | Engine result | Reference | Verdict |
|---|---|---|---|
| **Barnard's Star** | **10.45″/yr, PA 358°** | Gaia 10.39″/yr, PA 356° | ✅ mover, matches Gaia |
| **Eps Indi B** | **4.67″/yr, PA 122°**, parallax 0.36 ± 0.18″ | literature 4.7″/yr, PA 122°, parallax 0.28″ | ✅ mover + parallax |
| **R Dra** | **variable**, flux ratio between visits 0.56–0.63 | Mira, P ≈ 245 d | ✅ variable, not a mover (dev tools, 252 frames) |
| **RW Aur** | static; the archive refutes a fake 15″/yr fit | young variable star | ✅ no false mover |
| **WISE 0855** | only in per-pass stacks: 8.9″/yr, PA 284°, **3.2σ** | about 8.1″/yr | ⚠️ "candidate", not a mover |

- **No false movers** on these fields.
- **On the website** (live, 54 frames, detectors 1–3), Barnard's Star comes out as a **9.91″/yr, PA 2°, 5.1σ mover**:
  281 tracks, 1 candidate.
- My own earlier run (old engine, 2026-11-14) gave Barnard 10.43 ± 1.77″/yr and missed the other four. The table
  above supersedes it.

**Pitch line:** "With only 3 visits and 6-arcsecond pixels, our engine measured Barnard's Star at
10.45″/yr against Gaia's 10.39. It also recovered Epsilon Indi B's motion *and its parallax*, and flagged R Draconis
as a variable, not a mover."

### Engine known problems (from CLAUDE.md; be honest if asked)
- Crowded fields still need care with the O(n²) linking pieces. Most of that was fixed by the k-d tree.
- **Stack mode** is order-sensitive. In time order: Eps Indi B 4.28″/yr (true 4.68), and WISE 0855 a 6.6σ mover. Both
  real-target stack tests are *strict expected failures* until this is fixed.
- The website samples about 76 frames per field, too few for R Dra's variability. It's flagged only with the dev tools' 252
  frames.
- A mover blending with a field star in one pass gets rejected.
- Constants were tuned on 5 targets. They should be re-derived from many static stars before trusting survey-wide significances.
- Asteroids moving *within* one visit aren't tracked yet.

## 8c. Multi-observatory fusion: "SPHEREx finds it, old surveys confirm it" **[verified by reading code]**

Code: `backend/planetx/observatories/` (`fusion.py`, `catalogs.py`, `sources.py`, `images.py`). Setting
`PLANETX_OBSERVATORIES` = `spherex` | `multi` (default) | `all`.

- **Why:** SPHEREx alone has 2–4 epochs over 1–2 years, so motion errors are a few tenths of an arcsec per year, and
  two unrelated sources can be paired into a fake mover. Older surveys saw the same sky **10–27
  years earlier with about 0.1″ positions**, so a match makes the baseline much longer.
- **Archives** (fetched from IRSA's Gator service, which answered in about 1.5 s while TAP was timing out at more than 90 s):
  - 2MASS (1997–2001)
  - AllWISE (2010–11)
  - CatWISE2020 (position at 2015.4)
  - NEOWISE single exposures (2013–2024): opt-in only, because near the poles a 20″ cone returns about 45,000 rows
- **Algorithm:**
  1. Walk back in time one archival epoch at a time, nearest first. Predict where the object
     was from the motion fitted so far, and accept the catalog source inside a gate of 3σ, between 2″ and 60″.
  2. Each accepted epoch tightens the prediction. Epochs that failed to match are retried.
  3. Refit over all epochs. **The error shrinks about 20× with a 2MASS match.**
- **Verdicts:**
  - **confirmed**: the archive lies on the track and the motion is significant
  - **stationary**
  - **refuted**: each end of the SPHEREx "track" has its own stationary archival source, so it was
    two different objects, not one mover
  - **unconfirmed**: no counterpart found (too faint, blended, or new). **This is where real unknown candidates come from.**
- Removes a common SPHEREx-minus-archive frame offset before fitting, and treats AllWISE, CatWISE and NEOWISE as one
  family, so they aren't counted as independent confirmations.

**Real result: the strongest number in the project** **[verified, CLAUDE.md]**
- Barnard's Star, SPHEREx only: (−0.48, +10.42) **± 1.77″/yr**.
- After fusion with 2MASS: (−0.820, +10.362) **± 0.029″/yr**. **Gaia: (−0.80, +10.36).**
- **About 60× more precise, matching Gaia to the hundredth of an arcsec per year.**
- Barnard field: 95 tracks → 83 stationary, 11 unconfirmed, **0 false movers**. RW Aur field: 50 stationary, 2 unconfirmed, 0 movers.
- Barnard has no usable AllWISE/CatWISE entry (saturated), so **2MASS (around 2000) does the work**: the star is about 4.5′ away in 2MASS images.
- False movers found during development, and the guard that now stops each one:
  - a faint bystander inside a loose search area → chance-alignment test, ranked by brightness
  - neighbours that 2MASS/WISE resolve but SPHEREx blends into one pixel → the motion must exceed a 6.2″ total shift
  - a single archival epoch → it confirms only if SPHEREx alone already sees ≥3σ

**Pitch line:** "A candidate only reaches the 'unknown' list after it survives SPHEREx's
own error budget *and* a check against 25 years of archival infrared data."

## 8d. Catalogue check: naming and classifying every object **[verified from code + commit]**

Code: `backend/planetx/classify/` (`rules.py` = pure decision rules, `catalogs.py` = thin fetchers, `check.py`).

- **Why it exists:** the engine and the archive refit say *whether* something moves, not *what* it is. On the
  Barnard field they presented **two false movers** next to Barnard's Star, and Gaia settled both:
  - a 1.4σ track that AllWISE and CatWISE "confirmed" at 2.0″/yr actually sits on a 7 mas/yr Gaia star. Its two
    "counterparts", 32″ and 20″ away, were two other slow Gaia stars that happened to line up.
  - a 3.5σ candidate hopped between two faint Gaia stars 8″ apart (a blend seen in different detectors).
- **How:** every catalogue position is moved to the SPHEREx date with its own proper motion first (Barnard's Star is
  110″ from its Gaia 2016 position today). The match radius is 4.5″ (about 0.7 px), and only Gaia stars brighter than
  G 19.5 count. Gaia and SIMBAD are fetched once per field. **SkyBoT** is asked only about movers no star explains,
  and only for positions measured within hours, because no asteroid stays inside one SPHEREx pixel longer than that.
  SPHEREx's own orbit shifts asteroid positions by up to 9.7″ at 1 au, and the code allows for that.
- **The six classes:** `known_star` | `known_asteroid` | `variable_star` | `artifact` | **`unknown_candidate`** | `static`.
- The numbers behind the thresholds come from the Barnard field: 281 tracked objects, 6,884 Gaia stars within 10′.
- A catalogue outage never fails a run: the classification is left empty, with a note.

## 8e. "Is the motion real?": the evidence checklist **[verified from code + commit]**

Clicking a mover opens an **Evidence** view: the real SPHEREx images of that patch in every pass, blinking, with the
measured positions marked, plus a last-minus-first difference image, position against time, and **2MASS and WISE images
of the same sky** from decades earlier (`/api/motion/evidence/surveys`).

The engine's verdict rests on one number (speed ÷ error). These **7 independent tests** look at other parts of the
data, so they fail for different reasons (`api/evidence_checks.py`):
1. **beyond_noise**: is the move bigger than how much the field's fixed stars appear to shift?
2. **track_line**: are all passes on one straight line at a steady speed?
3. **images_show_motion**: does the difference image show a dark-to-bright pair along the track, compared with
   random spots in the same patch?
4. **many_exposures**: is it found in many single exposures, so not a cosmic ray or glitch?
5. **detectors_agree**: does each detector array measure the move on its own?
6. **same_look**: is the brightness and colour the same in every pass, so one object and not two?
7. **no_stand_in**: was nothing else sitting at the new spot before, and nothing still at the old one?

**Live result on Barnard's Star: 7 of 7 decidable tests pass. The pixels alone show 9.3″ of motion against the
engine's 9.8″. Of 280 fixed stars in the field, 277 correctly *fail* "moved beyond the noise".**
RW Aur's fake 15″/yr, 20σ straight-line fit is explained as static: the middle pass covered the track and saw
nothing, and the archive refutes it.

**Pitch line:** "We don't ask you to trust a number. Click any mover and see the pixels, the 25-year-old
2MASS image, and seven independent tests."

## 8f. The survey: crawling the sky on its own **[verified from code + commit]**

- **`motion` processor** (`survey/motion.py`): each HEALPix patch (nside 64) is tiled into **64 fields of 0.2°**, and each
  is run through the same `analyse_field` pipeline as the website. Every object is kept once, from the field whose centre
  is nearest.
- **Candidates table** (`survey/candidates.py`, `GET /api/candidates`, the website's Candidates panel). Each row is scored
  from 0 to 1, so that **an uncatalogued, significant mover comes first**:

| Tier | Score |
|---|---|
| unknown_candidate + mover (≥5σ, no catalogue explains it) | 0.9 + up to 0.09 for significance |
| mover, catalogues not reachable | 0.7 |
| unknown_candidate + 3–5σ motion | 0.6 |
| uncatalogued variable | 0.5 |
| known asteroid / variable star / known star / artifact | 0.3 / 0.2 / 0.1 / 0 |

- Processing is at-least-once and safe to repeat: re-processing a patch replaces its rows in one transaction, so no duplicates.
- Leases with a heartbeat mean a crashed worker's patch goes back in the queue. Signals are handled so finished work is never lost.
- **For the pitch:** have the crawler running and quote "N patches, X candidates". Even "0 unknown candidates, all known
  movers recovered" is a strong, honest result.

## 8g. The website **[verified from commits / README]**

- Opens on the **SPHEREx DR1 all-sky cube** as a slowly turning orthographic globe, with a wavelength slider over the
  100 usable frames (0.75–5.0 µm). AllWISE, 2MASS and DSS2 are alternative backgrounds.
- **Gaia DR3 star field** on a canvas: stars sized by brightness, and **proper motion animated forward from 2016**.
  A "Movers" toggle fades slow stars and draws the fast ones' predicted paths. The camera flies along great circles.
- **"Find what moves"** runs the engine on the field you're looking at (about 54 frames, about 216 MB from IRSA the
  first time, 1–2 minutes). Engine motions are drawn in **cyan** next to Gaia's catalogued motion in **red**.
- **Plain language** (`plain.js`): compass directions instead of position angles, "crosses the full Moon in about 180
  years", and verdicts that never say more than "candidate".
- **Names for everything** (`names.js`): SIMBAD and Gaia merged into one row per place on the sky, with tracks matched
  to catalogues at the right date.
- **Class filter and legend** on the map, a Candidates panel, and the Evidence viewer (8e).
- Reduced-motion users get no spin and instant jumps. Shareable `?ra=&dec=&fov=` URLs.
- Design: Instrument Serif + IBM Plex Mono, black and white, with one signal colour for motion.

## 8h. Engineering quality: CI and deploy **[verified]**
- **GitHub Actions CI** on every PR and push to `main`:
  - backend: locked dependency check, lint for real bugs only, offline tests
  - frontend: lint, tests, build
  - live-network tests are kept out of CI on purpose, because IRSA/CDS 5xx errors would block merges for no reason
- **Offline-test guard:** `tests/conftest.py` stops offline tests from touching the network. Fake SPHEREx files come from
  `tests/synthetic.py`. 475 backend tests and 128 frontend tests.
- **Deploy plan** (drafted, not committed yet): frontend on GitHub Pages; API plus survey worker in **one Render
  container** with a 10 GB disk. They share one container because SQLite in WAL mode can't sit on a network disk, so a
  separate worker couldn't see the API's database.
- Security points: IRSA host and path allow-lists for every server-side download, size caps (including after unzipping),
  timeouts everywhere, a concurrency cap, and TLS verification kept on.

## 8i. How to stand out (advice from the "ML model" session) **[project advice]**
1. **Lead with the 60× number**: SPHEREx ± 1.77 → fused ± 0.029″/yr, matching Gaia. Other teams will only say "it moves".
2. **Show that we checked ourselves**: the false movers we caught and the guard for each. Possible addition: an
   **injection-recovery plot** (inject fake movers into real frames, plot the recovered fraction against speed and
   brightness). About half a day of work; `tests/synthetic.py` is a starting point.
3. **Show what the survey found**: N patches crawled from the NEP, X candidates, each with an evidence page.
4. **Use the spectrum**, which only SPHEREx has: 102 bands, where a methane/water dip marks a brown dwarf (WISE 0855).
5. **Make the demo hard to break**: pre-warm the cache for all 5 targets, put a QR code to a shareable URL on a slide,
   and record a backup video.
- **On ML:** use physics to *measure* motion (error bars, explainable, works for never-seen objects), and if anything use ML
  only to *reject artifacts*. That's the same split ZTF and Rubin use. It isn't needed for the hackathon.
- Don't add more features. Five targets with honest evidence beat twelve half-finished panels.

## 9. Demo targets (live demo slide) **[project]**

| Target | Shows | Expected result |
|---|---|---|
| Barnard's Star | fastest-moving star | clear motion, matches Gaia |
| WISE 0855−0714 | coldest known brown dwarf, about 8″/yr | motion; spectrum with molecular absorption |
| Epsilon Indi B | brown dwarf binary (unresolved) | combined system motion and spectrum |
| RW Aurigae | young variable star | brightness change, **not** motion |
| R Draconis | Mira, period about 245 days, inside the first survey region | variable, and caught by the crawler |

## 10. Engineering we did that judges might care about **[verified]**

These are the "we handled the real world" points:
- IRSA randomly answers **502/503 "Server busy"**, even for files that don't exist. We retry
  only on 5xx, connection errors and timeouts, with back-off. We never retry 4xx.
- IRSA sometimes returns **HTTP 200 with an HTML "busy" page** instead of FITS. We check the
  file signature (`SIMPLE`) and retry.
- SIA queries have **no built-in timeout** and a 10′ cone hung. We use 1′ cones plus `maxrec` limits.
- Deep fields near the NEP return **tens of thousands of rows** without `maxrec`.
- Cutout `size` is in **degrees** (confirmed: 0.1° gives 59×58 px). Each cutout is about **4 MB** even when tiny,
  because of the EPSF table, so we cache and keep requests small.
- Cutout layers: PRIMARY, IMAGE, FLAGS, VARIANCE, ZODI, EPSF, WCS-WAVE. We use the FLAGS layer to mask artifacts,
  VARIANCE for error bars, and WCS-WAVE for per-pixel wavelength.
- The main CDS hosts were down during testing. The tests run against each server separately, so we can
  see which host is broken, and the app falls back to mirrors.
- Python.org CPython builds have no CA bundle. We point at certifi's bundle and **keep TLS
  verification on** instead of disabling it.

## 11. Honest limitations (put on a slide; judges respect this)

- 3–4 visits per spot, and pixels are about 6″, so only fast movers (about 1″/yr or more, near the noise) are detectable outside
  the poles. **[project/verified math]**
- Unresolved binaries (Eps Indi Ba/Bb). **[project]**
- Bright stars saturate. Barnard's peak pixel was flagged, but the centroid still worked. **[verified]**
- A distant "Planet X" would be very faint. Whether it falls within SPHEREx's depth is uncertain. **[check]**
  Frame this as "the tool flags *candidates* for follow-up", not "we will find Planet Nine".
- Dependence on external services: IRSA lags new data by about a day, and the CDS outage happened during testing. **[project/verified]**
- WISE 0855, the coldest brown dwarf, is too faint for single SPHEREx frames. It shows up only in per-pass stacks, at 3.2σ,
  so we call it a "candidate". **[verified]**
- Website-sized samples (about 76 frames per field) are too small to flag R Dra's variability. The deeper dev-tool sample (252 frames) does flag it. **[verified]**
- Thresholds were tuned on 5 targets and should be re-calibrated on many static stars before survey-wide significances
  are trusted. An injection-recovery test would measure this. **[project]**
- Asteroids that move *within* one visit aren't tracked yet. Only motion between passes is. **[verified]**

## 12. Planet X angle (use carefully) **[check all]**

- "Planet Nine" is a hypothesized planet hundreds of AU out, proposed to explain the clustering of
  extreme trans-Neptunian orbits (Batygin & Brown, 2016).
- At hundreds of AU, most of its apparent motion over 6 months would come from **Earth's own
  orbit** (parallax). That is a large shift, many pixels, so motion is not the bottleneck. **Brightness is.**
- Pitch framing: our crawler is a general "anything that moves or changes" search. Planet X is the
  dream case. Brown dwarfs, asteroids and variables are the realistic yield.

## 13. Timeline / process (team slide) **[project]**

- Before the hackathon: data connections tested (done), Barnard's Star motion demo (done in tests), FastAPI and Aladin skeleton.
- Done by 2026-11-15, about 03:30: API, engine, archive fusion, catalogue classification, evidence checklist, survey
  processor + candidates, website with engine, CI. All merged on `main`. Built in parallel by about 8 Claude sessions in separate
  git worktrees and merged through PRs.
- Hours 18–22: crawler starts, **left running from about hour 20**, so the demo shows real survey output.
- Scope rule: after hour 24, only must-have features.

## 14. Sources / credits slide

- SPHEREx data: NASA / Caltech-IPAC IRSA. https://irsa.ipac.caltech.edu/data/SPHEREx/docs/overview_qr.html
- Gaia DR3: ESA/Gaia/DPAC
- CatWISE2020 (Marocco et al. 2021), 2MASS, SIMBAD and VizieR (CDS Strasbourg)
- SkyBoT (IMCCE), JPL SBIdent / Horizons
- Aladin Lite (CDS), astropy, astroquery, pyvo
- WISE 0855 motion: Luhman 2014; Kirkpatrick et al. 2019 **[check citations]**

---

## Open items to fill in as other work lands
- [x] Motion engine on the website (cyan = engine, red = Gaia)
- [x] Evidence view: real images per pass, difference image, 2MASS/WISE images, 7-test checklist
- [x] Survey processor + scored candidates list
- [ ] Screenshot/GIF of the evidence blink on Barnard's Star (the strongest visual)
- [ ] Before/after error-bar graphic: ±1.77 → ±0.029″/yr vs Gaia
- [ ] Number of patches the crawler has covered, and how many candidates by class (start the crawler and leave it running)
- [ ] Injection-recovery plot (optional, about half a day)
- [ ] One spectrum plot (WISE 0855 or Eps Indi B)
- [ ] R Dra light curve
- [ ] Deploy: enable GitHub Pages (Settings → Pages → GitHub Actions), commit the Render deploy files, then add the live URL + QR code
- [ ] Pre-warm the cache for all 5 targets before the demo
