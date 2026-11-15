# SPHEREx data API (frontend guide)

Base URL for local development: `http://127.0.0.1:8000` (run
`uv run uvicorn planetx.api.app:app --reload`). Interactive docs: `/docs`.
All angles are in degrees (ICRS), times are MJD (UTC) plus a `YYYY-MM-DD` date, and
wavelengths are in microns. Response shapes are in `backend/planetx/api/schemas.py`.

## Typical flow

1. `GET /api/frames?ra=&dec=&size_deg=` lists the frames for a position.
2. For each frame, load `image_url` (PNG) for display, or `data_url` (float JSON)
   for blink, stack or difference on the client.
3. All frames for one `(ra, dec, size_deg)` share **one** grid: north up, east left,
   ~6.15"/px. Pixel `(row, col)` is the same sky spot in every frame.

## Endpoints

### `GET /api/visits?ra=&dec=&radius_arcsec=`
SPHEREx exposures from `spherex_dr1` and `spherex_qr3` whose images cover the cone.
- `radius_arcsec`: default 10, range 0.1-300.
- Returns `VisitsResponse`. `visits` is sorted by `mjd`. `passes` groups visits into
  survey passes; a new pass starts after a gap of more than 30 days.
- `obs_id` is shared by the detectors of one exposure. `(obs_id, detector)` is unique,
  and so is `access_url` (use it as a React key).
- `wavelength_min_um`/`wavelength_max_um` cover the **whole detector's** band, not
  the band at the target.

```sh
curl 'http://127.0.0.1:8000/api/visits?ra=269.4485&dec=4.7394'
# Barnard's Star: 364 visits in 3 passes (2025-08..10, 2026-03..05, 2026-08..09)
```

### `GET /api/frames?ra=&dec=&size_deg=&detector=`
The visits within 10" of `(ra, dec)`, each with ready-made cutout URLs.
- `size_deg`: default 0.1, range 0.01-0.5. This is the side of the square grid.
- `detector`: optional, 1-6. Each detector covers one wavelength range (from SIA, in um):
  D1 0.75-1.12, D2 1.10-1.65, D3 1.63-2.43, D4 2.40-3.85, D5 3.81-4.43, D6 4.41-5.01.
- `image_url` and `data_url` are root-relative (`/api/cutout.png?...`) and already
  URL-encoded. Prefix them with the API origin.
- `wavelength_um` is always `null` here. The band at the target is known only once
  the cutout loads: read it from `CutoutData.wavelength_um`.
- **Cost**: each frame is a separate ~4 MB download from IRSA on first use. A
  position typically has about 360 frames (about 60 per detector). Load lazily, filter
  by `detector`, and keep at most about 4 requests in flight. The server allows 4
  concurrent IRSA downloads and answers **503 with `Retry-After`** when it is
  saturated. Repeat requests are served from the disk cache in about 50 ms.

```sh
curl 'http://127.0.0.1:8000/api/frames?ra=269.4485&dec=4.7394&size_deg=0.1&detector=3'
```

### `GET /api/cutout.png?url=&ra=&dec=&size_deg=&vmin=&vmax=`
One frame resampled onto the common grid, as an 8-bit grayscale + alpha PNG with an
asinh stretch.
- `url` must be an `access_url` from `/api/visits` or `/api/frames`. Any other URL
  gets a 400.
- **No-data or masked pixels are transparent** (alpha 0).
- By default each frame is stretched to its own 0.5-99.5 percentiles, so brightness
  is not comparable between frames. For a fair blink, pass the same `vmin`/`vmax`
  (in `bunit`, MJy/sr) for every frame, for example values taken from one frame's
  JSON.
- Responses carry `Cache-Control: public, max-age=86400`.

```sh
curl -o frame.png 'http://127.0.0.1:8000/api/cutout.png?url=https%3A%2F%2Firsa.ipac.caltech.edu%2Fibe%2Fdata%2Fspherex%2Fdr1%2Flevel2%2F2025W33_2C%2Fl2b-v27-2026-228%2F3%2Flevel2_2025W33_2C_0321_1D3_spx_l2b-v27-2026-228.fits&ra=269.4485&dec=4.7394&size_deg=0.1'
```

### `GET /api/cutout.json?url=&ra=&dec=&size_deg=`
The same frame as float pixels (`CutoutData`): `width`, `height`, `ra`, `dec`,
`pixel_scale_arcsec`, `mjd`, `wavelength_um` (band centre at the target),
`bunit`, `data_b64`.

`data_b64` is base64 of little-endian float32 values, row-major, `height` rows of
`width` values. **Row 0 is the top (north) and column 0 the left (east)**, the same
layout as canvas `ImageData`. `NaN` means no data or a masked pixel.

```js
async function loadCutout(apiOrigin, dataUrl) {
  const c = await (await fetch(apiOrigin + dataUrl)).json();
  const bytes = Uint8Array.from(atob(c.data_b64), ch => ch.charCodeAt(0));
  // Float32Array uses the platform byte order; every browser platform is little-endian.
  const pixels = new Float32Array(bytes.buffer);  // length = c.width * c.height
  const at = (row, col) => pixels[row * c.width + col];  // NaN = no data
  return { ...c, pixels, at };
}
// Difference of two frames on the shared grid: a[i] - b[i], skip NaN.
```

### `GET /api/motion?ra=&dec=&size_deg=`
Runs the motion engine on SPHEREx frames of the position and returns what moves or
changes brightness (`MotionResponse`). The run is a background job, so **poll**: the
first call starts it and answers `status: "running"`; repeat the same call every few
seconds until `status` is `"done"` (with `result`) or `"failed"` (with `error`).
- `size_deg`: default 0.1, range 0.03-0.2. Frames reach 1.5x this (the raw cutout,
  shared with `/api/cutout.*`), so objects can lie a little outside the square.
- The position is snapped to 0.001 deg; nearby requests share one run. Results are
  kept for an hour. A failed run is reported for 10 s, then asking again retries it. A run nobody polls for 30 s is dropped.
- While running, `stage` is `queued | listing | downloading | analysing | checking`
  and `frames_done / frames_total` counts downloads.
- The engine samples detectors 1-3, up to 6 frames per pass and detector (about 54
  frames, ~4 MB each from IRSA on first use: one to two minutes; seconds once cached).
- `result.objects` lists every tracked object, movers first. `kind` is `mover`,
  `candidate` (3-5 sigma in SPHEREx alone) or `static`; `is_variable` marks brightness
  changes. Motions are in arcsec/yr (east, north). `track` has one position per pass.
  `archive` is the refit with 2MASS/AllWISE/CatWISE positions and its verdict
  (set `PLANETX_OBSERVATORIES=spherex` to skip the catalogs).
- `motion_verdict` (`MotionVerdict`) says whether the motion is real, from every check at once
  (see [Motion verdict](#motion-verdict)). Read it, not `kind`, to tell real motion from fake.
- One run executes at a time; **503 with `Retry-After`** when more than 4 are waiting.
- Jobs live in the API process's memory: run a single worker process.

```sh
curl 'http://127.0.0.1:8000/api/motion?ra=269.4485&dec=4.7394'
# Barnard's Star: mover, 9.9"/yr at PA 2 deg from 54 frames in 3 passes
```

### `GET /api/motion/evidence?ra=&dec=&size_deg=&object=`
The proof behind one engine object (`MotionEvidence` in `schemas.py`): the same small
patch of sky around it in every survey pass (real SPHEREx frames of that pass, aligned and
median-combined, one shared grey scale, north up, east left, PNGs as base64), a
last-minus-first difference image, its measured position per pass with errors, the
straight-line fit, and how far the field's fixed stars appear to shift (the noise).
Also:
- `passes[].exposures`: up to 6 single exposures per pass (`EvidenceExposure`), shared out over
  detectors and spread over the pass, whether or not they detected the object, each with the
  engine's detection in it. Same centre and grey scale, `exposure_px` pixels on a side. Empty
  when the engine ran on per-pass stacks. `passes[].n_cover`: exposures covering the object.
- `checks`: independent tests that the motion is real and is one object (`EvidenceCheck`,
  built in `planetx/api/evidence_checks.py`), each `pass | weak | fail | unknown` with a plain
  summary and its numbers: `beyond_noise` (move vs. fixed stars' scatter), `track_line` (one
  straight track), `images_show_motion` (dark-to-bright pair in the difference image, read as a
  shift from the pixels alone and checked against random places and a cross-track control),
  `many_exposures`, `detectors_agree` (each detector's own shift), `same_look` (brightness and
  colour per band), `no_stand_in` (no other source at its old or new spots). The frontend adds
  `other_surveys` from the run's archive and catalogue checks.
- `ra`, `dec`, `size_deg`: the same values as the finished `/api/motion` run.
- `object`: `MotionObject.id` from that run.
- 404 when the run's engine data is no longer kept (only the newest 4 runs keep it). The
  404 also starts the field again, so request `/api/motion` until done, then retry. The
  frontend does this itself.
- `missing_passes`: passes whose frames covered the fitted track but found nothing there.
  The engine then takes the positions for different objects, so such an object is never a
  mover, however large and significant its fitted slope.
- `motion_verdict`: the object's `MotionObject.motion_verdict` after the `checks` (null when
  the run's verdicts are not known). The image tests never overrule a catalogued star or
  asteroid. A decisive failure (`no_stand_in`, `same_look`, `beyond_noise`) or two other
  failures make a SPHEREx-only motion `fake_motion`, one other failure makes it `unsure`, and a
  decisive failure drops an older-survey confirmation to `unsure`.

## Motion verdict

`MotionVerdict` (built in `planetx/classify/reliability.py`, pure rules over what the run
already measured) is on every `MotionObject`, `MotionEvidence` and `SurveyCandidate`:

```json
{ "verdict": "fake_motion", "certainty": null, "cause": "chance_link", "decided_by": "catalogue",
  "reason": "Each SPHEREx position falls on a different Gaia star, none moving anywhere near 7.78″ a year: the track hops between fixed stars." }
```

- `verdict`: `moving` (real motion) | `not_moving` (nothing beyond noise) | `fake_motion` (SPHEREx
  shows motion, the evidence says it is not real) | `unsure` (motion is claimed, the evidence
  cannot settle it).
- `certainty` (moving only): `confirmed` (a catalogue or older surveys agree) | `candidate`
  (SPHEREx alone, or one older survey family). A candidate is never called a discovery.
- `cause`: for `fake_motion` always, for `unsure` when one is suspected; the causes of
  `docs/RESULTS.md`: `blend | saturated_halo | edge | flagged_pixels | low_snr |
  wavelength_centroid | parallax_misfit | chance_link | other`.
- `decided_by`: `spherex | archive | catalogue | evidence_checks`.
- There is no probability: nothing measured yet says how often each rule is right.

Rules, first match wins (an object needs an apparent motion first: the engine's mover flag, a
motion class other than static, an archival confirmation or one-family candidate, a SPHEREx
motion of 5 sigma or more even under a pixel of total shift, or 3 sigma over 3+ passes with a
pixel of shift):

| Evidence | Verdict | Cause |
|---|---|---|
| no apparent motion | not_moving | |
| a Gaia/SIMBAD star's own motion follows every pass | moving, confirmed | |
| older surveys: a fixed source at each position (`refuted`) | fake_motion | chance_link (blend with a blended pass) |
| a different slow Gaia star under each pass | fake_motion | chance_link |
| a pass covered its path and saw nothing (`missing_passes`) | fake_motion | chance_link |
| CatWISE lists nothing moving like it where its track puts it in 2015 (`absent`) | fake_motion when SPHEREx saw it at ≥ 2.8 µm in 2+ clean detections; unsure when saturated / in a bright star's halo or seen only at shorter wavelengths | the SPHEREx cause, else chance_link |
| older surveys saw it in the same place (`stationary`) | fake_motion | the SPHEREx cause below, else other |
| it sits on one slow Gaia star whose motion it matches | fake_motion | the SPHEREx cause below, else other |
| archival "confirmation" from other stars, no SPHEREx motion | fake_motion | chance_link |
| a known asteroid at one of its positions | moving, confirmed | |
| older surveys confirm (`confirmed`) | moving, confirmed; a candidate when Gaia was not checked and SPHEREx < 5 sigma; unsure when saturated or in a bright star's halo | |
| next to a slow Gaia star with a similar motion | unsure | blend |
| SPHEREx only, saturated or within 150″ of a very bright star | fake_motion once Gaia was asked, unsure otherwise | saturated_halo |
| SPHEREx only, a pass it needs is blended / mostly flagged pixels / faint / parallax with ≤ 3 passes | unsure | blend / flagged_pixels / low_snr / parallax_misfit |
| one older survey family alone shows it | moving, candidate | |
| SPHEREx only, an engine mover | moving, candidate | |
| SPHEREx only, 5+ sigma but under a pixel of total shift (too little time between visits) | unsure; moving, candidate when another track moves the same way along the same path (one mover split in two, as Eps Indi B was) | |
| SPHEREx only, 2 passes, or 3-5 sigma | unsure | |

### `GET /api/motion/evidence/surveys?ra=&dec=&side_arcsec=`
The same sky in older surveys (`SurveyImages`): 2MASS Ks (1997-2001) and AllWISE W1 (mean
date ~2010), from IRSA (SIA2 `twomass_allsky` / `wise_allwise`, IBE cutouts), north up,
east left, centred on `ra`, `dec`. Independent of any run: the frontend centres it between
the object's SPHEREx position and where its motion puts it in 1999, and marks both.
- `side_arcsec`: 30-900. Native pixels (2MASS 1", WISE 1.375"), coarser beyond 601 px.
- Grey levels come from each image's own sky and noise: black is 2 sigma below the sky, white
  80 sigma above it, with the asinh stretch of `/api/cutout.png`. Faint stars stay well above the
  noise instead of being clipped, and the grey values can be turned back into linear ones.
- Where one survey image does not cover the patch, images of the same night are pieced
  together (each on a common zero sky level); pixels none covers are transparent.
- Always 200: a survey that cannot give an image (no coverage, IRSA down) is listed in
  `unavailable` with a reason, and asked again only after 2 minutes. Cached on disk under
  `PLANETX_CACHE_DIR/surveys` (200 MB, least recently used out); `Cache-Control` one day
  when both images are there.

```sh
curl 'http://127.0.0.1:8000/api/motion/evidence/surveys?ra=269.4492&dec=4.7297&side_arcsec=432'
# Barnard's Star: 2MASS 2000-05-28 shows it ~4.5' south of its SPHEREx position
```

### `GET /api/spectrum?ra=&dec=&pmra=&pmdec=&epoch=`
A SPHEREx spectrum of one object, measured on the fly (`SpectrumResponse`). A background
job like `/api/motion`: **poll** the same URL until `status` is `"done"` or `"failed"`
(`stage`: `queued | listing | measuring`, `frames_done / frames_total`).
- `pmra`, `pmdec`: proper motion in mas/yr (RA component includes cos dec), default 0,
  at most 20,000. `epoch`: Julian year of `ra`/`dec` (2016.0 for Gaia DR3), default now;
  ignored without a motion. The aperture follows the object to each exposure's date.
- The position is not snapped (the aperture must sit on the object); repeat requests within
  0.036" share one run. Results are kept for an hour; a run nobody polls for 30 s is dropped.
- Exposures: every one already in the cutout cache at this position, topped up to 12 per
  detector with new downloads spread over time (~72 x 4 MB on first use, about a minute;
  seconds afterwards; asking again later downloads nothing new). Each exposure sees the
  object at its own wavelength, so ~12 per detector fill about half of its 17 bands.
- Photometry (`planetx/spherex/spectrum.py`): 1.5-pixel (9.2") aperture, background from a
  4-8 pixel annulus, aperture correction from the file's EPSF, wavelength from WCS-WAVE,
  flux density in **mJy** (MJy/sr x pixel solid angle), errors from VARIANCE plus 2% in
  quadrature. An exposure with any `PHOTOMETRY_BAD_FLAGS` (or OVERFLOW) pixel in the
  aperture is left out: `counts.saturated` for OVERFLOW/BLOOM/NONLINEAR, else `counts.masked`.
  `result.saturated` is true when most exposures were saturated (Barnard's Star: all).
- `result.points`: one per measured exposure; `result.bands`: the 102-band grid (6 detectors x
  17 log-spaced bands), robustly combined, only bands with data.
- `result.features`: depth of water (1.4, 1.9, 2.7 um) and methane (1.6-1.7, 2.2-2.4,
  3.3 um) absorption against a power-law continuum through two windows on the flux peaks
  (or a Rayleigh-Jeans slope from one window when the other is missing or not significant).
  `detected` means depth >= 0.15 at >= 3 sigma; `null` when the bands were not measured.
  `suggestion`/`summary` say what the features *suggest* (`brown_dwarf` for methane plus
  water), never what the object is. All passes are combined (a variable mixes its states)
  and a neighbour within ~10" adds its light.

```sh
curl 'http://127.0.0.1:8000/api/spectrum?ra=331.04415&dec=-56.78284&pmra=3982&pmdec=-2467&epoch=2000'
# Eps Indi B: T-dwarf shape (J/H/K peaks, deep 3.3 um methane, 4 um peak): "suggests a cool brown dwarf"
```

## Errors

| Status | Meaning |
|-------:|---------|
| 400 | `url` is not an allowed IRSA SPHEREx file URL, or `vmax <= vmin` |
| 404 | IRSA has no cutout for this file at this position (e.g. outside the file) |
| 422 | Parameter missing or out of range (FastAPI validation body) |
| 502 | IRSA failed after retries (`detail` gives the reason) |
| 503 | Too many IRSA downloads in flight, or too many motion or spectrum runs waiting; retry after `Retry-After` seconds |

## Pixel masking

The display products set to NaN only the pixels flagged NONFUNC, MISSING_DATA, HOT
or COLD, plus non-finite pixels. IRSA's photometry tool rejects more flags
(`PHOTOMETRY_BAD_FLAGS` in `planetx/cutouts.py`, which include BLOOM and SUR_ERROR).
Those flags also cover the cores of bright stars such as Barnard's Star, so they are
not applied to the images. The detection pipeline should apply them.

## Survey candidates

The offline crawler (`uv run python -m planetx.survey run --processor motion`) runs the
same pipeline as `/api/motion` over every survey patch (HEALPix nside 64, ~0.92 deg).
A patch is tiled into 64 fields of 0.2 deg centred on its nside-512 sub-pixels; an
object seen by several overlapping fields is kept once, from the field whose centre
is nearest. The fields' frames come from one raw cutout per SPHEREx visit for the whole
patch (~25 MB each, about 50 per patch near the NEP), cropped locally; each field still
gets the per-field sample (`pick_visits`) and the same engine run. It stores the notable objects (movers, motion candidates and brightness
variables) in the `candidates` table of the survey database (`PLANETX_DB`).
Re-processing a patch replaces its rows, so ids are not stable across runs.

### `GET /api/candidates?limit=100&offset=0&classification=&sort=score&hide_fake=false`
`SurveyCandidates` in `schemas.py`, best score first (ties by id):

```json
{ "total": 1,
  "candidates": [{ "id": 1, "ra": 269.45, "dec": 4.74, "name": "Barnard's Star",
    "classification": "known_star", "kind": "mover", "is_variable": false,
    "pm_total": 10.4, "pm_ra_cosdec": -0.5, "pm_dec": 10.4, "significance": 6.1,
    "position_angle_deg": 357.0, "score": 0.13, "nside": 64, "ipix": 23456,
    "found": "2026-11-14", "field_ra": 269.4727, "field_dec": 4.7053 }] }
```

- `sort`: `score` (default) or `date` (newest survey run first, then best score).
- `motion_verdict`: is the motion real ([Motion verdict](#motion-verdict)). The survey stores
  it with each row (columns `verdict`, `verdict_certainty`, `verdict_cause`, `verdict_reason`,
  `verdict_decided_by`; a writable store adds them to an older table). Rows stored before
  verdicts were get one from their stored columns, with less detail (no saturation, blend or
  Gaia checks), when read.
- `hide_fake=true` leaves out rows whose verdict is `fake_motion` (stored or judged from the
  columns), so `total` and the pages agree with the filter.
- `kind` is the engine's motion class (`mover | candidate | static`, the last for variables).
- `field_ra`/`field_dec`: centre of the 0.2 deg field the object was measured in.
  `/api/motion?ra=<field_ra>&dec=<field_dec>&size_deg=0.2` repeats the survey's run there: a
  full run (about 54 cutouts of ~4 MB unless the server has them cached), so the website starts
  it only when asked and then opens the candidate's card. `ref_mjd` is when `ra`/`dec` hold;
  move the position by the motion to compare it with a run whose reference date differs.
- `limit` 1-500 (default 100), `offset` >= 0. `classification` is one of
  `known_star | known_asteroid | variable_star | artifact | unknown_candidate | static`
  or empty for all (anything else is 422). Stored objects whose catalogue check could
  not run have `classification: null` and appear only in the unfiltered list.
- Motions are the SPHEREx fit in arcsec/yr, as `MotionObject` reports them; `found`
  is the UTC date of the run that stored the row.
- An empty list (`total: 0`), not an error, while the survey database or its
  candidates table does not exist. The API only reads the database.
- `score` (0-1) = tier + up to 0.09 for significance (linear from 3 to 10 sigma: the
  motion's for movers and candidates, the brightness change's for variables):

  | Tier | Object |
  |-----:|--------|
  | 0.9 | uncatalogued mover (`unknown_candidate`, significant motion) |
  | 0.7 | mover, catalogue check did not run |
  | 0.6 | uncatalogued 3-5 sigma motion candidate |
  | 0.5 | uncatalogued brightness variable |
  | 0.4 | motion candidate, catalogue check did not run |
  | 0.3 | known asteroid; variable whose catalogue check did not run |
  | 0.2 | catalogued variable star |
  | 0.1 | catalogued star whose proper motion explains the motion |
  | 0.0 | artifact |

```sh
curl 'http://127.0.0.1:8000/api/candidates?limit=20&classification=unknown_candidate'
```

### `GET /api/candidates/{id}/thumbnail.png`
A small SPHEREx picture centred on one candidate (0.03 deg, ~18 px, grey + alpha, north up,
east left): the newest detector 1-3 exposure of the engine's visit sample for that field, cut
from the field's raw cutout (the same cutout an `/api/motion` run on the field uses, so either
fills the cache for the other). A first request costs one ~4 MB download per exposure tried
(up to 3, skipping exposures IRSA has no cutout of); repeats come from the cutout cache.
- At most 2 render at once: **503 with `Retry-After: 3`** beyond that (and 503 when IRSA
  downloads are saturated). 404 for an unknown id (ids change when a patch is re-processed)
  or when no exposure has data there; 422 for an id outside 1..2^63-1; 502 when IRSA fails.
- `Cache-Control` one hour. Load these lazily: the website asks only for rows in view, two at once.

## Survey progress and coverage

### `GET /api/survey/status`
`SurveyStatus`: patch counts by status (`pending | in_progress | done | failed`), `total`,
`nside`/`moc_order`, and the crawl frontier (`current_rank`, its centre and its distance from
the north ecliptic pole). All zero before the queue exists.

### `GET /api/survey/coverage?status=done&limit=`
The patches of one status as an Aladin Lite MOC JSON, `{"<order>": [NESTED ipix, ...]}`
(order = log2(nside), ipix ascending), for `A.MOCFromJSON`:
- `status`: `done` (default), `failed` (given up after retries), or `queued` (in progress or
  waiting). Anything else is 422.
- `limit` 1-50000 (optional): only that many, the next the crawl reaches first (lowest rank).
  Use it with `queued`, which is nearly the whole sky early on; the website draws the next 300.
- An empty list while no patch has that status, or before the queue exists.

```sh
curl 'http://127.0.0.1:8000/api/survey/coverage?status=queued&limit=300'
```
