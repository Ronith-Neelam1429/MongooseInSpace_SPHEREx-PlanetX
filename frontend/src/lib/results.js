// The Results page's data: fetching the survey-scale result files (docs/RESULTS.md) and shaping
// them into what the charts draw, plus the wording. Pure functions apart from the fetchers.
import { API_BASE, apiJson } from './services.js';
import { classLabel } from './plain.js';

export const RESULT_NAMES = ['gaia_validation', 'false_movers', 'brown_dwarfs', 'light_curve_r_dra', 'injection_recovery'];

// A count in words; a missing number (a result file without that field) shows as a dash.
const fmt = (x) => (Number.isFinite(x) ? x.toLocaleString('en-US') : '–');
const finite = (...xs) => xs.every(Number.isFinite);

/**
 * Which result files the server has: { name: { available, generated_at, error } }, or null when
 * no backend is configured or it does not offer /results yet (404).
 */
export async function fetchResultsIndex({ signal } = {}) {
  if (!API_BASE) return null;
  let j;
  try {
    j = await apiJson('/results', { signal });
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
  if (!Array.isArray(j?.results)) throw new Error('bad response');
  return Object.fromEntries(j.results.map((r) => [r.name, r]));
}

/** One result file, or null when it has not been run yet (404). */
export async function fetchResult(name, { signal } = {}) {
  if (!RESULT_NAMES.includes(name)) throw new Error(`unknown result ${name}`);
  try {
    return await apiJson(`/results/${name}`, { signal });
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
}

/** The survey's live numbers (GET /results/survey_stats), or null when unavailable. */
export async function fetchSurveyStats({ signal } = {}) {
  if (!API_BASE) return null;
  try {
    return await apiJson('/results/survey_stats', { signal });
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
}

/** "14 Nov 2026, 22:00 UTC" for a result's generated_at. */
export function generatedText(iso) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return null;
  const day = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' });
  return `${day}, ${time} UTC`;
}

/** About `n` round tick values (steps of 1, 2 or 5 times a power of ten) covering [lo, hi]. */
export function niceTicks(lo, hi, n = 4) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / n;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((k) => k * p).find((s) => s >= raw);
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toPrecision(12));
  return out;
}

// ---------- survey progress ----------

/**
 * The survey's numbers for the page: patch counts, objects, candidates by class (largest first)
 * and the recent rate. The rate is patches finished per hour over the last day, counted only from
 * the survey's first finished patch (a survey started two hours ago is not averaged over 24).
 */
export function surveySummary(stats, now = Date.now()) {
  if (!stats) return null;
  const p = stats.patches || {};
  const done = p.done || 0;
  const hours = (stats.last_day || []).map((h) => ({ start: new Date(h.start), done: h.done, failed: h.failed }));
  const doneLastDay = hours.reduce((s, h) => s + h.done, 0);
  let rate = null;
  if (doneLastDay > 0 && hours.length) {
    const windowStart = hours[0].start.getTime();
    const first = stats.first_finished_at ? new Date(stats.first_finished_at).getTime() : windowStart;
    const spanH = (now - Math.max(windowStart, first)) / 3.6e6;
    rate = doneLastDay / Math.max(spanH, 1);
  }
  const classes = Object.entries(stats.by_classification || {})
    .map(([key, count]) => ({ key, label: key === 'unchecked' ? 'Not checked against catalogues' : classLabel(key), count }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
  return {
    available: Boolean(stats.available),
    total: stats.total_patches || 0,
    done,
    failed: p.failed || 0,
    inProgress: p.in_progress || 0,
    fraction: stats.total_patches ? done / stats.total_patches : 0,
    objects: stats.objects_measured || 0,
    candidates: stats.candidates_total || 0,
    classes,
    hours,
    doneLastDay,
    rate,
    lastFinished: stats.last_finished_at ? new Date(stats.last_finished_at) : null,
  };
}

/** "4.2 patches an hour" with digits that suit the size. */
export function rateText(rate) {
  if (rate == null) return 'none finished in the last day';
  const v = rate >= 10 ? String(Math.round(rate)) : rate >= 1 ? rate.toFixed(1) : rate.toFixed(2);
  return `${v} ${v === '1.0' ? 'patch' : 'patches'} an hour`;
}

// ---------- does SPHEREx agree with Gaia? ----------

/**
 * Symmetric log: linear within ±c, logarithmic beyond, so 0 and 10″/yr fit one axis.
 * symlog(c) = log10(2); symlog(0) = 0; odd in v.
 */
export function symlog(v, c = 0.01) {
  return Math.sign(v) * Math.log10(1 + Math.abs(v) / c);
}

/** One point per object with both motions measured: totals, per-component residuals and pulls. */
export function gaiaPoints(doc) {
  const out = [];
  for (const o of doc?.objects || []) {
    const { spherex_pm_ra: sra, spherex_pm_dec: sdec, spherex_pm_err: se, gaia_pm_ra: gra, gaia_pm_dec: gdec } = o;
    if (!finite(sra, sdec, se, gra, gdec) || !(se > 0)) continue;
    const ge = Number.isFinite(o.gaia_pm_err) && o.gaia_pm_err > 0 ? o.gaia_pm_err : 0;
    const err = Math.hypot(se, ge);
    out.push({
      gaia: Math.hypot(gra, gdec),
      spherex: Math.hypot(sra, sdec),
      err: se,
      gMag: o.g_mag,
      truth: o.truth,
      mover: Boolean(o.engine_is_mover),
      resid: [sra - gra, sdec - gdec],
      pull: [(sra - gra) / err, (sdec - gdec) / err],
    });
  }
  return out;
}

/**
 * Counts of `values` in `bins` equal bins over [lo, hi), plus how many fell below or above (NaN
 * values are ignored). Each bin: { lo, hi, count }.
 */
export function histogram(values, lo, hi, bins) {
  const w = (hi - lo) / bins;
  const out = Array.from({ length: bins }, (_, i) => ({ lo: lo + i * w, hi: lo + (i + 1) * w, count: 0 }));
  let under = 0, over = 0, n = 0;
  for (const v of values) {
    if (!Number.isFinite(v)) continue;
    n++;
    if (v < lo) under++;
    else if (v >= hi) over++;
    else out[Math.min(bins - 1, Math.floor((v - lo) / w))].count++;
  }
  return { bins: out, under, over, n, width: w };
}

/** The unit Gaussian scaled to a histogram of n values in bins of `width`: [{ x, y }]. */
export function gaussianCurve(n, width, lo, hi, steps = 80) {
  const k = (n * width) / Math.sqrt(2 * Math.PI);
  return Array.from({ length: steps + 1 }, (_, i) => {
    const x = lo + ((hi - lo) * i) / steps;
    return { x, y: k * Math.exp(-(x * x) / 2) };
  });
}

/** Standard deviation of the finite values (null for fewer than two). */
export function stdDev(values) {
  const v = values.filter(Number.isFinite);
  if (v.length < 2) return null;
  const m = v.reduce((s, x) => s + x, 0) / v.length;
  return Math.sqrt(v.reduce((s, x) => s + (x - m) ** 2, 0) / (v.length - 1));
}

const pct = (k, n) => {
  const p = (100 * k) / n;
  return p === 0 || p >= 10 ? `${Math.round(p)}%` : `${p.toFixed(1)}%`;
};

/**
 * The confusion matrix in words, from Gaia's point of view. Returns { still, moving } sentences
 * (null when that row is empty) and the 2x2 counts.
 */
export function confusionText(confusion) {
  const { tp = 0, fp = 0, fn = 0, tn = 0 } = confusion || {};
  const nStill = fp + tn;
  const nMoving = tp + fn;
  return {
    still: nStill
      ? `Of ${fmt(nStill)} star${nStill === 1 ? '' : 's'} Gaia says are still, we wrongly called ${fmt(fp)} moving (${pct(fp, nStill)}).`
      : null,
    moving: nMoving
      ? `Of ${fmt(nMoving)} star${nMoving === 1 ? '' : 's'} Gaia says move fast enough for SPHEREx to see, we caught ${fmt(tp)} and missed ${fmt(fn)} (${pct(tp, nMoving)} caught).`
      : null,
    counts: { tp, fp, fn, tn },
  };
}

// ---------- which motions are fake? ----------

/** Each cause of a fake motion, in plain words. Keys are docs/RESULTS.md's `cause` values. */
export const CAUSES = {
  blend: { label: 'Two stars in one pixel', sentence: 'Two stars sit so close that SPHEREx sees one blob, and the blob’s centre shifts between visits as their light mixes differently.' },
  saturated_halo: { label: 'Glare of a bright star', sentence: 'A very bright star floods its neighbourhood with light; faint spots in that glare jump around and look like they move.' },
  edge: { label: 'Edge of the image', sentence: 'Part of the star fell off the edge of the picture in one visit, so its measured centre was pulled inward.' },
  flagged_pixels: { label: 'Damaged pixels', sentence: 'Some of its pixels were marked faulty (hot, cold or overflowing) in one visit, which skews where it seems to be.' },
  low_snr: { label: 'Too faint to pin down', sentence: 'The star is so faint that its position is mostly noise, and noise sometimes lines up into a fake track.' },
  wavelength_centroid: { label: 'Colour shift', sentence: 'Each SPHEREx picture is taken in a different colour; a pair of stars of different colours seems to shift as the colour changes.' },
  parallax_misfit: { label: 'Near star’s yearly wobble', sentence: 'A nearby star wobbles back and forth over the year as Earth orbits the Sun; with only a few visits that wobble was read as motion.' },
  chance_link: { label: 'Wrong stars joined up', sentence: 'Two unrelated faint stars, one in each visit, happened to line up and were joined into one “moving” object.' },
  other: { label: 'Not explained yet', sentence: 'We have not worked out why these look like they move; they are kept out of the candidate list until we have.' },
};

/**
 * Fake movers grouped by cause, most common first, each with its plain sentence and up to
 * `examples` of its movers' evidence lines.
 */
export function causeRows(doc, examples = 2) {
  const fake = (doc?.movers || []).filter((m) => m.cause);
  const groups = new Map();
  for (const m of fake) {
    const key = CAUSES[m.cause] ? m.cause : 'other';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(m);
  }
  // A summary without the movers list (or with more than it lists) still gives the counts.
  const counted = doc?.summary?.by_cause || {};
  for (const key of Object.keys(counted)) if (!groups.has(CAUSES[key] ? key : 'other')) groups.set(CAUSES[key] ? key : 'other', []);
  const rows = [...groups.entries()].map(([cause, movers]) => ({
    cause,
    ...CAUSES[cause],
    count: Math.max(movers.length, counted[cause] || 0),
    examples: [...new Set(movers.map((m) => m.evidence).filter(Boolean))].slice(0, examples),
  }));
  return rows.sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

// ---------- what can we find? ----------

/** Cell edges halfway between sorted centres (geometric for log axes), extended by half a step at each end. */
export function edges(centres, log = false) {
  const t = log ? centres.map(Math.log) : centres;
  if (t.length === 1) return log ? [centres[0] / Math.SQRT2, centres[0] * Math.SQRT2] : [centres[0] - 0.5, centres[0] + 0.5];
  const mid = t.slice(1).map((v, i) => (v + t[i]) / 2);
  const out = [t[0] - (t[1] - t[0]) / 2, ...mid, t[t.length - 1] + (t[t.length - 1] - t[t.length - 2]) / 2];
  return log ? out.map(Math.exp) : out;
}

/**
 * The injection grid as a speed x brightness heatmap: sorted speeds and magnitudes, a cell per
 * pair (null where none was injected), and the 50% and 90% recovery lines: per magnitude, the
 * slowest speed at which the recovered fraction reaches the level, interpolated in log speed.
 */
export function injectionHeatmap(doc) {
  const grid = (doc?.grid || []).filter((g) => finite(g.pm_arcsec_yr, g.mag_bin) && g.pm_arcsec_yr > 0);
  const speeds = [...new Set(grid.map((g) => g.pm_arcsec_yr))].sort((a, b) => a - b);
  const mags = [...new Set(grid.map((g) => g.mag_bin))].sort((a, b) => a - b);
  const at = new Map(grid.map((g) => [`${g.pm_arcsec_yr}|${g.mag_bin}`, g]));
  const cells = [];
  for (const mag of mags) {
    for (const pm of speeds) {
      const g = at.get(`${pm}|${mag}`);
      if (!g) continue;
      const f = Number.isFinite(g.recovered_fraction) ? g.recovered_fraction
        : g.n_injected > 0 ? g.n_recovered / g.n_injected : null;
      cells.push({ pm, mag, fraction: f, injected: g.n_injected, recovered: g.n_recovered });
    }
  }
  const line = (level) => mags.map((mag) => ({ mag, pm: crossing(cells.filter((c) => c.mag === mag), level) }));
  return { speeds, mags, cells, lines: { 50: line(0.5), 90: line(0.9) } };
}

/** Slowest speed at which a row's recovered fraction first reaches `level` (log interpolation), or null. */
export function crossing(row, level) {
  const pts = row.filter((c) => Number.isFinite(c.fraction)).sort((a, b) => a.pm - b.pm);
  for (let i = 0; i < pts.length; i++) {
    if (pts[i].fraction < level) continue;
    if (i === 0) return pts[0].pm;
    const a = pts[i - 1], b = pts[i];
    const t = (level - a.fraction) / (b.fraction - a.fraction);
    return Math.exp(Math.log(a.pm) + t * (Math.log(b.pm) - Math.log(a.pm)));
  }
  return null;
}

/** The injection summary in a sentence: how fast and how bright a mover must be to be found. */
export function injectionText(doc) {
  const s = doc?.summary || {};
  const parts = [];
  if (Number.isFinite(s.pm_50)) parts.push(`half of the planted movers are found once they move ${s.pm_50}″ a year`);
  if (Number.isFinite(s.pm_90)) parts.push(`nine in ten at ${s.pm_90}″ a year`);
  let text = parts.length ? `${parts.join(', and ')}` : null;
  if (text && Number.isFinite(s.mag_50)) text += `; below magnitude ${s.mag_50} (fainter) most are lost`;
  return text ? `${text[0].toUpperCase()}${text.slice(1)}.` : null;
}

/** False alarms among planted still stars, in words. */
export function falsePositiveText(fp) {
  if (!fp || !(fp.n_static_injected > 0)) return null;
  const k = fp.n_flagged_mover || 0;
  return `Of ${fmt(fp.n_static_injected)} planted stars that do not move, ${fmt(k)} ${k === 1 ? 'was' : 'were'} wrongly flagged as moving (${pct(k, fp.n_static_injected)}).`;
}

// ---------- only SPHEREx can do this ----------

export const SPECTRAL_KINDS = {
  reference_star: { label: 'Ordinary stars', order: 0 },
  mover: { label: 'Our moving candidates', order: 1 },
  known_brown_dwarf: { label: 'Known brown dwarfs', order: 2 },
};

/** Methane and water depths per object with both measured, grouped by kind (drawing order: stars first). */
export function spectralPoints(doc) {
  return (doc?.objects || [])
    .filter((o) => finite(o.methane_depth, o.water_depth) && SPECTRAL_KINDS[o.kind])
    .map((o) => ({
      kind: o.kind,
      name: o.name || null,
      methane: o.methane_depth,
      water: o.water_depth,
      methaneErr: Number.isFinite(o.methane_err) ? o.methane_err : 0,
      waterErr: Number.isFinite(o.water_err) ? o.water_err : 0,
      flag: o.flag,
      pm: o.pm_total,
    }))
    .sort((a, b) => SPECTRAL_KINDS[a.kind].order - SPECTRAL_KINDS[b.kind].order);
}

/** "2 of our 12 moving candidates have both dips, which suggests a brown dwarf." */
export function spectralText(points) {
  const movers = points.filter((p) => p.kind === 'mover');
  const cool = movers.filter((p) => p.flag === 'suggests_brown_dwarf').length;
  const known = points.filter((p) => p.kind === 'known_brown_dwarf');
  const knownCool = known.filter((p) => p.flag === 'suggests_brown_dwarf').length;
  const parts = [];
  if (known.length) parts.push(`${fmt(knownCool)} of ${fmt(known.length)} known brown dwarfs show both dips`);
  if (movers.length) parts.push(`${fmt(cool)} of our ${fmt(movers.length)} moving candidates do too, which suggests ${cool === 1 ? 'it is a brown dwarf' : 'brown dwarfs'}`);
  return parts.length ? `${parts.join('; ')}.` : null;
}

const MJD_UNIX0 = 40587;
export const mjdToDate = (mjd) => new Date((mjd - MJD_UNIX0) * 864e5);

/**
 * R Dra's light curve: points by date, and folded on the fitted period (phase 0-1) when there is
 * one. Returns null without points.
 */
export function lightCurve(doc) {
  const points = (doc?.points || [])
    .filter((p) => finite(p.mjd, p.flux))
    .map((p) => ({ mjd: p.mjd, flux: p.flux, err: Number.isFinite(p.flux_err) ? p.flux_err : 0, wavelength: p.wavelength }))
    .sort((a, b) => a.mjd - b.mjd);
  if (!points.length) return null;
  const period = Number.isFinite(doc.period_days_fit) && doc.period_days_fit > 0 ? doc.period_days_fit : null;
  const t0 = points[0].mjd;
  const folded = period ? points.map((p) => ({ ...p, phase: (((p.mjd - t0) / period) % 1 + 1) % 1 })) : null;
  const fluxes = points.map((p) => p.flux);
  return {
    points,
    folded,
    period,
    literature: Number.isFinite(doc.period_days_literature) ? doc.period_days_literature : null,
    ratio: Math.min(...fluxes) > 0 ? Math.max(...fluxes) / Math.min(...fluxes) : null,
  };
}

// ---------- the precision headline ----------

/**
 * Barnard's Star, the same SPHEREx track measured two ways (docs/presentation-notes.md section 8b
 * and CLAUDE.md "Multi-observatory motion"): SPHEREx alone (3 passes over a year) and refitted with
 * 2MASS, AllWISE and CatWISE positions going back 25 years. Gaia DR3 is the reference. Motions in
 * arcsec/yr, totals with their 1-sigma error.
 */
export const PRECISION = {
  target: 'Barnard’s Star',
  spherex: { pm: 10.43, err: 1.77, label: 'SPHEREx alone', detail: '3 visits over one year' },
  fused: { pm: Math.hypot(-0.82, 10.36), err: 0.029, label: 'SPHEREx + older surveys', detail: 'with 2MASS, AllWISE and CatWISE, 25 years' },
  gaia: { pm: Math.hypot(-0.80, 10.36), label: 'Gaia DR3', detail: 'the reference' },
};

/** How many times smaller the fused error is: 1.77 / 0.029 = 61. */
export const precisionGain = (p = PRECISION) => p.spherex.err / p.fused.err;
