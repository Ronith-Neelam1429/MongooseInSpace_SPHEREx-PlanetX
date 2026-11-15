# Survey-scale results: file contract

Analysis tools in `tools/` write JSON result files to `results/` (tracked in git, so the
deployed site can serve them). `GET /api/results/{name}` serves `results/{name}.json`, and the
website's Results page draws them. Each tool owns one file. Change a shape only by adding fields.

Every file has this header:

```json
{"name": "<file stem>", "version": 1, "generated_at": "2026-11-15T04:00:00Z",
 "command": "uv run python tools/<script>.py ...", "notes": ["plain-language caveats"], ...}
```

Units: arcsec/yr for proper motion, degrees for RA/Dec, um for wavelength, mJy for flux, MJD for time.

## `gaia_validation.json` (tools/gaia_validation.py)
SPHEREx-only engine motions against Gaia DR3 for every matched object in N fields.
- `fields`: `[{ra, dec, n_objects, n_matched}]`
- `objects`: `[{ra, dec, g_mag, snr, n_passes, spherex_pm_ra, spherex_pm_dec, spherex_pm_err,
  gaia_pm_ra, gaia_pm_dec, gaia_pm_err, engine_is_mover, truth}]`, where `truth` is
  `"real_mover" | "static" | "ambiguous"` from Gaia (static: Gaia total PM < 0.05"/yr at > 5 sigma precision;
  real_mover: Gaia PM > 3x the SPHEREx error and > 0.5"/yr)
- `summary`: `{n_objects, n_matched, residual_median, residual_mad, pull_std, confusion: {tp, fp, fn, tn},
  false_mover_rate, recovered_fraction}`

## `false_movers.json` (tools/false_mover_audit.py)
Every engine mover that Gaia (or a pixel inspection) shows is not really moving, with the cause.
- `movers`: `[{ra, dec, field_ra, field_dec, pm_total, significance, snr, truth, cause, evidence}]`, where `cause` is
  one of `"blend" | "saturated_halo" | "edge" | "flagged_pixels" | "low_snr" | "wavelength_centroid" |
  "parallax_misfit" | "chance_link" | "other"` (or null when real), `evidence` a short sentence
- `summary`: `{n_movers, n_fake, by_cause: {cause: count}}`

## `brown_dwarfs.json` (tools/brown_dwarf_search.py)
SPHEREx spectral indices for movers and reference stars.
- `objects`: `[{ra, dec, name, pm_total, kind, methane_depth, methane_err, water_depth, water_err,
  color_1_4um, snr, flag}]`, where `kind` is `"mover" | "reference_star" | "known_brown_dwarf"` and `flag`
  is `"suggests_brown_dwarf" | "star_like" | "too_faint" | "contaminated"`
- `summary`: `{n_objects, n_suggests_brown_dwarf}`

## `light_curve_r_dra.json` (tools/brown_dwarf_search.py --light-curve)
- `points`: `[{mjd, flux, flux_err, wavelength}]`, `period_days_fit`, `period_days_literature` (about 245)

## `injection_recovery.json` (tools/injection_recovery.py)
Fake movers planted into real SPHEREx frames.
- `grid`: `[{pm_arcsec_yr, mag_bin, n_injected, n_recovered, n_flagged_mover, recovered_fraction}]`
- `false_positives`: `{n_fields, n_static_injected, n_flagged_mover}`
- `summary`: `{pm_50, pm_90, mag_50}`: speed (arcsec/yr) and brightness where half/90% are recovered

## `survey_stats` (live, not a file)
`GET /api/results/survey_stats` reads the survey SQLite database: patches by status, objects measured, candidates by
class, rate of patches per hour.
