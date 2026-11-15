// The SPHEREx spectrum of one object: what to ask the backend for, and what the chart draws.
import { knownMotion } from './pictures.js';
import { ticks } from './evidence.js';
import { mjdToYear } from './names.js';

export const X_RANGE = [0.72, 5.1]; // µm; SPHEREx covers 0.75-5.0
export const X_TICKS = [0.75, 1, 1.5, 2, 3, 4, 5];
const MOLECULE = { CH4: 'methane', H2O: 'water' };

/**
 * The position and motion to measure an engine object at: { ra, dec, pmra, pmdec, epoch } (mas/yr,
 * decimal year). Only objects the engine accepted as moving carry their motion (a static object's
 * fitted slope is noise); the archival refit is preferred when it confirmed the motion.
 */
export function engineTarget(o) {
  const target = { ra: o.ra, dec: o.dec };
  if (o.kind === 'static') return target;
  const a = o.archive;
  const fused = a?.verdict === 'confirmed' && a.pm_ra_cosdec != null && a.pm_dec != null;
  const east = fused ? a.pm_ra_cosdec : o.pm_ra_cosdec;
  const north = fused ? a.pm_dec : o.pm_dec;
  if (!Number.isFinite(east) || !Number.isFinite(north)) return target;
  return { ...target, pmra: east * 1000, pmdec: north * 1000, epoch: mjdToYear(o.ref_mjd) };
}

/** The same for a catalogued object (a known-object row, see knownObjects in names.js). */
export function knownTarget(row, nowYear) {
  const m = knownMotion(row, nowYear);
  return { ra: m.ra, dec: m.dec, pmra: m.pmra, pmdec: m.pmdec, epoch: m.year };
}

/**
 * Everything the chart draws, in data units (x: wavelength in µm on a log axis, y: mJy).
 *   bands:    combined bands { wavelength, lo, hi, flux, err }
 *   points:   single exposures { wavelength, flux, err, outlier }
 *   features: shaded windows { key, label, molecule, lo, hi, detected }
 * The y range starts at zero (or below it when noise dips there) and covers the bands with their
 * errors; single exposures far outside it are clipped, not allowed to squash the spectrum.
 */
export function spectrumChart(result) {
  const bands = result.bands.map((b) => ({
    wavelength: b.wavelength_um, lo: b.lo_um, hi: b.hi_um, flux: b.flux_mjy, err: b.flux_err_mjy, n: b.n_used,
  }));
  const points = result.points.map((p) => ({
    wavelength: p.wavelength_um, flux: p.flux_mjy, err: p.flux_err_mjy, outlier: p.outlier,
  }));
  const highs = bands.map((b) => b.flux + b.err);
  const lows = bands.map((b) => b.flux - b.err);
  let hi = highs.length ? Math.max(...highs) : 1;
  let lo = Math.min(0, ...lows);
  if (!(hi > lo)) hi = lo + 1;
  const pad = (hi - lo) * 0.08;
  hi += pad;
  if (lo < 0) lo -= pad;
  const features = result.features.map((f) => ({
    key: f.key, label: f.label, molecule: MOLECULE[f.molecule] || f.molecule, lo: f.lo_um, hi: f.hi_um, detected: f.detected,
  }));
  return { x: X_RANGE, y: [lo, hi], yTicks: ticks(lo, hi, 4), bands, points, features };
}

/** A feature's measurement in words: "34% ± 7% deeper than the continuum (4.4σ)". */
export function featureText(f) {
  if (f.depth == null) return 'not measured (those bands were not observed, or too faint)';
  const pct = (v) => `${Math.round(v * 100)}%`;
  const sig = f.significance != null ? ` (${f.significance.toFixed(1)}σ)` : '';
  if (f.depth < 0) return `no dip: ${pct(-f.depth)} ± ${pct(f.depth_err)} above the continuum${sig}`;
  return `${pct(f.depth)} ± ${pct(f.depth_err)} below the continuum${sig}`;
}

/** Short progress line while the backend measures. */
export function progressText(job) {
  if (!job || job.stage === 'queued') return 'Waiting for the server…';
  if (job.stage === 'listing' || !job.frames_total) return 'Listing the SPHEREx exposures of this object…';
  return `Measuring exposure ${job.frames_done} of ${job.frames_total}…`;
}
