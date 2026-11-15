# Presentation notes: SPHEREx PlanetX

Running collection of material for the pitch. Last updated 2026-11-14 (about 19:45: sessions overview, engine and fusion sections added).

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

This is the strongest proof-of-concept slide.

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

## 8a. Who is building what (status as of 2026-11-14, about 19:45)

| Piece | Branch / folder | How it's built | Status |
|---|---|---|---|
| Data connection tests | `setup/data-connections` (main folder) | live pytest against IRSA, Gaia, VizieR, SIMBAD, SkyBoT, SBIdent, HiPS | PR #1 open; 12 SPHEREx and 14 catalog tests pass |
| Backend API | `feature/backend-api` → `planetx-worktrees/api` | FastAPI: `/api/visits`, `/api/frames`, `/api/cutout.png`, `/api/cutout.json`; every visit resampled onto one north-up grid | PR #2 open. Barnard's Star has **364 visits in 3 passes**, and on a live frame it landed **0.87 px** from its predicted position |
| Survey crawler | `feature/survey-queue` → `planetx-worktrees/survey` | NESTED HEALPix order spiraling out from the NEP; SQLite queue with **leases + heartbeat** so a crashed worker's patch goes back in the queue; CLI `init/status/run/requeue-failed`; `/api/survey/status`, `/api/survey/coverage` (MOC for Aladin) | PR #3 open; 102 unit tests. It only does real work once the engine is plugged in |
| Combined check | API + survey merged | | **222 offline tests and 6 live tests pass**; all 7 endpoints respond |
| Sky map frontend | `frontend/sky-explorer` | React + Vite + Aladin Lite; click to explore, shareable URL, info panel | pushed, no PR yet |
| **Motion engine** | `feature/motion-engine` → `planetx-worktrees/engine` | pure numpy/astropy/scipy, no network (section 8b) | about 750 lines, **not committed yet**, no engine unit tests yet |
| **Multi-observatory fusion** | `feature/multi-observatory` → `planetx-worktrees/observatories` | refines SPHEREx motions with 2MASS / AllWISE / CatWISE / NEOWISE positions (section 8c) | about 900 lines plus a 477-line test file, **not committed yet** |

Engineering detail worth a sentence: the cutout cache is capped (2 GB, least-recently-used files removed first), every request has a timeout, and cutout URLs
are restricted to IRSA so the server can't be used to fetch arbitrary URLs.

## 8b. How the motion engine works (the physics) **[verified by reading the code + our own run]**

Code: `src/planetx/engine/` (`frames.py` → `detect.py` → `motion.py`). Entry point
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
starts a new pass**. At Barnard's Star that gives 3 passes (Aug–Oct 2025, Mar–May 2026, Aug–Sep 2026).

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

### Known limits (engine doc string + our run)
- A fast asteroid moving *within* one visit is not handled yet (only motion between passes).
- Blended pairs collapse to one source (acceptable at 6″).
- Parallax is not modelled.

### Engine results on our test targets: our own offline run, 2026-11-14 **[verified]**
Run on the engine's cached cutouts (`.cache/dev`) with default settings. The engine is still in
development, so these numbers will change.

| Target | Frames / passes | Result |
|---|---|---|
| **Barnard's Star** | 163 frames, 3 passes, all 6 detectors | **Mover found: 10.43 ± 1.77″/yr** (µα* −0.48, µδ +10.42), 5.9σ, χ²/dof 0.99. **Gaia: 10.39″/yr (−0.80, +10.36).** About 40 s for the run |
| WISE 0855 | 33 frames, 3 passes, detectors 5–6 | **not detected yet**: nothing within 30″ of the target |
| R Dra | 54 frames, 3 passes, detectors 1–3 | nearest object 19″ off and not flagged variable. Probably saturation (no flux) or cutout centring; needs work |
| RW Aur | 54 frames, 3 passes, detectors 1–3 | found within 1″, static (correct), **not flagged variable** (flux ratio 1.18, low significance) |
| Eps Indi B | 13 frames, **1 pass** cached | cannot measure motion with one pass |

**Pitch line:** "From SPHEREx alone, with 6-arcsecond pixels and 3 visits, our engine measured
Barnard's Star moving at 10.4″/yr, the same as Gaia, and flagged it as the most significant mover in the field."

**Honest status:** the Barnard demo works. WISE 0855, R Dra and RW Aur still need tuning. Don't promise these
in the pitch until they're re-run.

## 8c. Multi-observatory fusion: "SPHEREx finds it, old surveys confirm it" **[verified by reading code]**

Code: `src/planetx/observatories/` (`fusion.py`, `irsa.py`, `sources.py`). Setting
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

**Pitch line:** "A candidate only reaches the 'unknown' list after it survives SPHEREx's
own error budget *and* a check against 25 years of archival infrared data."

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

## 12. Planet X angle (use carefully) **[check all]**

- "Planet Nine" is a hypothesized planet hundreds of AU out, proposed to explain the clustering of
  extreme trans-Neptunian orbits (Batygin & Brown, 2016).
- At hundreds of AU, most of its apparent motion over 6 months would come from **Earth's own
  orbit** (parallax). That is a large shift, many pixels, so motion is not the bottleneck. **Brightness is.**
- Pitch framing: our crawler is a general "anything that moves or changes" search. Planet X is the
  dream case. Brown dwarfs, asteroids and variables are the realistic yield.

## 13. Timeline / process (team slide) **[project]**

- Before the hackathon: data connections tested (done), Barnard's Star motion demo (done in tests), FastAPI and Aladin skeleton.
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
- [ ] Screenshot or GIF of the time-lapse player on Barnard's Star
- [ ] Number of patches the crawler has covered, and how many candidates by class
- [ ] One spectrum plot (WISE 0855 or Eps Indi B)
- [ ] R Dra light curve
- [ ] Deploy URL plus an example shareable link
