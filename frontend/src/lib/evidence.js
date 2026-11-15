import { separation } from './coords.js';
import { API_BASE, runMotion } from './services.js';

// The evidence behind one engine object (GET {API_BASE}/motion/evidence, see docs/API.md):
// fetching it, and the pure logic that turns it into a verdict in plain words and a chart.

const DAYS_PER_YEAR = 365.25;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const OBSERVATORY = { '2mass': '2MASS', allwise: 'AllWISE', catwise: 'CatWISE', neowise: 'NEOWISE' };
const CATALOG = { gaia: 'Gaia DR3', simbad: 'SIMBAD', skybot: 'SkyBoT' };
const COMPASS = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];

/** A re-run numbers its objects afresh; the same object is the one within this distance. */
const SAME_OBJECT_ARCSEC = 3;
const EVIDENCE_TIMEOUT_MS = 30000;

/** "2025-08-27" -> "Aug 2025". */
export function monthYear(date) {
  const [y, m] = date.split('-').map(Number);
  return `${MONTHS[m - 1]} ${y}`;
}

/** "2025-08-27" -> "27 Aug 2025". */
export function dayMonthYear(date) {
  return `${Number(date.slice(8, 10))} ${monthYear(date)}`;
}

/** The object in a (re-run) result that is the given one: the nearest, if it is close enough. */
export function sameObject(objects, object) {
  let best = null, bestSep = SAME_OBJECT_ARCSEC / 3600;
  for (const o of objects) {
    const sep = separation(o.ra, o.dec, object.ra, object.dec);
    if (sep <= bestSep) { best = o; bestSep = sep; }
  }
  return best;
}

/** A catalogued star and a tracked object are the same when within this distance (1.5 SPHEREx pixels). */
const TRACKED_MATCH_ARCSEC = 9;

/**
 * The engine objects of a run that its list of results leaves out (the static ones), in the
 * engine's order: listed on request so that any object's evidence can be opened.
 */
export function otherObjects(objects, shown) {
  const ids = new Set(shown.map((o) => o.id));
  return objects.filter((o) => !ids.has(o.id));
}

/** The tracked object of a run at a catalogued position (`ra`, `dec` today), or null. */
export function trackedAt(objects, ra, dec) {
  let best = null, bestSep = TRACKED_MATCH_ARCSEC / 3600;
  for (const o of objects) {
    const sep = separation(o.ra, o.dec, ra, dec);
    if (sep <= bestSep) { best = o; bestSep = sep; }
  }
  return best;
}

const isSame = (evidence, object) => separation(evidence.ra, evidence.dec, object.ra, object.dec) * 3600 < 0.01;

/** One evidence request: the parsed body, or null when the backend no longer keeps the run (404). */
async function requestEvidence(field, id, signal) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(new DOMException('Request timed out', 'TimeoutError')), EVIDENCE_TIMEOUT_MS);
  const onAbort = () => ctrl.abort(signal.reason);
  if (signal?.aborted) ctrl.abort(signal.reason);
  else signal?.addEventListener('abort', onAbort, { once: true });
  try {
    const url = `${API_BASE}/motion/evidence?ra=${field.ra.toFixed(6)}&dec=${field.dec.toFixed(6)}`
      + `&size_deg=${field.size_deg.toFixed(4)}&object=${id}`;
    const res = await fetch(url, { signal: ctrl.signal });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    if (!body || !Array.isArray(body.passes) || !body.passes.some((p) => p.point)) throw new Error('bad response');
    return body;
  } catch (err) {
    if (err?.name === 'TimeoutError') throw new Error('the request timed out', { cause: err });
    throw err;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }
}

/**
 * Evidence for `object` (a MotionObject) of the run on `field` ({ ra, dec, size_deg } as runMotion
 * returned it). The backend keeps the engine's data of only the newest few runs; when this one is
 * gone (or was re-run and renumbered), the run is requested again through runMotion, whose progress
 * goes to `onProgress`, and the object is looked up again by position.
 * Resolves to the MotionEvidence body (backend/planetx/api/schemas.py), or null without a backend.
 */
export async function fetchEvidence(field, object, { signal, onProgress } = {}) {
  if (!API_BASE) return null;
  const direct = await requestEvidence(field, object.id, signal);
  if (direct && isSame(direct, object)) return direct;

  onProgress?.({ stage: 'queued', frames_done: 0, frames_total: 0 });
  const rerun = await runMotion(field.ra, field.dec, field.size_deg, { signal, onProgress });
  const again = rerun && sameObject(rerun.objects, object);
  if (!again) throw new Error('the engine did not find this object again when it re-ran the field');
  const evidence = await requestEvidence(rerun.field, again.id, signal);
  if (!evidence || !isSame(evidence, again)) throw new Error('the engine no longer holds this run');
  return evidence;
}

const arcsec = (v, digits = 1) => `${v.toFixed(digits)}″`;

function pixelsText(px) {
  if (px < 0.1) return 'under a tenth of a SPHEREx pixel';
  const text = px.toFixed(1);
  return `${text} SPHEREx pixel${text === '1.0' ? '' : 's'}`;
}

function listText(items) {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/** Compass word for a direction on the sky from its (east, north) components. */
export function compass(east, north) {
  const pa = (Math.atan2(east, north) * 180 / Math.PI + 360) % 360;
  return COMPASS[Math.round(pa / 45) % 8];
}

/** The first catalogue match that carries a proper motion, or null. */
export function catalogMotion(object) {
  const m = (object.matches || []).find((x) => x.pm_ra_cosdec != null && x.pm_dec != null);
  if (!m) return null;
  return {
    name: m.name, catalog: CATALOG[m.catalog] || m.catalog,
    pmEast: m.pm_ra_cosdec, pmNorth: m.pm_dec, pm: Math.hypot(m.pm_ra_cosdec, m.pm_dec),
  };
}

/** The archival refit, when positions from other surveys entered it; else null. */
export function archiveMotion(object) {
  const a = object.archive;
  if (!a || a.pm_ra_cosdec == null || a.pm_dec == null) return null;
  const surveys = (a.observatories || []).filter((x) => x !== 'spherex').map((x) => OBSERVATORY[x] || x);
  if (!surveys.length) return null;
  return {
    surveys, pmEast: a.pm_ra_cosdec, pmNorth: a.pm_dec, pm: Math.hypot(a.pm_ra_cosdec, a.pm_dec),
    err: a.pm_err, years: a.baseline_yr,
  };
}

/**
 * What the measurements say, as numbers: how far the object went between its first and last
 * measured pass, and how far the field's other objects (fixed stars) seem to go meanwhile.
 */
export function measure(object, evidence) {
  const seen = evidence.passes.filter((p) => p.point);
  const first = seen[0], last = seen[seen.length - 1];
  const east = last.point.east_arcsec - first.point.east_arcsec;
  const north = last.point.north_arcsec - first.point.north_arcsec;
  const shift = Math.hypot(east, north);
  return {
    first, last, east, north, shift,
    pixels: shift / evidence.spherex_pixel_arcsec,
    noise: last.field_scatter ? last.field_scatter.median_shift_arcsec : null,
    stars: last.field_scatter ? last.field_scatter.n_stars : 0,
    seen: seen.length,
    passes: evidence.passes.length,
    // Passes that covered the track and saw nothing (older backends do not send them).
    missing: (evidence.missing_passes || []).map((i) => evidence.passes[i]).filter(Boolean),
    span: `between ${monthYear(first.point.date)} and ${monthYear(last.point.date)}`,
  };
}

/** Why a pass that saw nothing on the track rules out one moving object (the engine's own check). */
function missingText(missing) {
  const which = listText(missing.map((p) => `pass ${p.index + 1} (${passSpan(p)})`));
  const them = missing.length === 1 ? 'That pass’s images cover' : 'Those passes’ images cover';
  return `${which[0].toUpperCase()}${which.slice(1)} saw nothing on the track. ${them} the line between the other positions, where one moving object would have been seen, so these positions belong to different objects.`;
}

/** A significant straight-line fit through positions of different objects is not a measured speed. */
function lineNotMotion(object) {
  return `The ${object.pm_total.toFixed(1)}″ per year is only the slope of a line through those positions, not a speed: the engine does not count it as motion.`;
}

/** "Aug – Oct 2025", "Sep 2025" or "Dec 2025 – Jan 2026" for a pass's first and last exposure. */
export function passSpan(p) {
  const a = monthYear(p.start), b = monthYear(p.end);
  if (a === b) return a;
  return a.slice(-4) === b.slice(-4) ? `${a.slice(0, 3)} – ${b}` : `${a} – ${b}`;
}

function brightnessLine(v) {
  const m = /^P(\d+)\/P(\d+) = ([\d.]+)$/.exec(v.change);
  const what = m
    ? `In pass ${m[1]} it was ${m[3]} times as bright as in pass ${m[2]}`
    : `Its brightness changed by a factor of ${v.ratio.toFixed(2)} (${v.change})`;
  return `${what}, comparing frames at matching wavelengths (${v.band}): ${v.significance.toFixed(1)} times the measurement error.`;
}

/**
 * The verdict in plain words: { tone, headline, lines }. `tone` is 'moved' | 'candidate' | 'still'
 * | 'refuted', plus `variable: true` when the brightness changed. Everything is said as a
 * measurement with its error; an unconfirmed mover is a "candidate".
 */
export function describe(object, evidence) {
  const m = measure(object, evidence);
  const archive = archiveMotion(object);
  const catalog = catalogMotion(object);
  const verdict = object.archive?.verdict;
  const sigma = object.significance;
  const times = `${sigma.toFixed(1)} times the measurement error`;
  const speed = `${object.pm_total.toFixed(2)}″ per year`;
  const noise = m.noise == null ? null
    : `The ${m.stars} other objects in this field, nearly all fixed stars, appear to shift by about ${arcsec(m.noise)} over the same time: that is the measurement noise.`;
  const lines = [];
  let tone, headline;

  if (object.kind === 'mover' && (verdict !== 'confirmed' || (m.pixels >= 1 && sigma >= 5))) {
    tone = 'moved';
    headline = 'It moved.';
    lines.push(`Moved ${arcsec(m.shift)} to the ${compass(m.east, m.north)} (${pixelsText(m.pixels)}) ${m.span}.`);
    if (noise) lines.push(noise);
    lines.push(`Its measured speed, ${speed}, is ${times}.`);
    if (verdict === 'confirmed' && archive) {
      lines.push(`Positions from ${listText(archive.surveys)}, going back ${archive.years.toFixed(0)} years, lie on the same track: ${archive.pm.toFixed(2)}″ per year.`);
    }
  } else if (object.kind === 'mover') {
    // Listed as a mover only because decades of archival positions show it; SPHEREx alone cannot.
    tone = 'moved';
    headline = 'A slow mover, shown by older surveys.';
    lines.push(`In SPHEREx alone it shifted ${arcsec(m.shift)} (${pixelsText(m.pixels)}) ${m.span}, ${times}: too little to see in these images.`);
    if (noise) lines.push(noise);
    if (archive) {
      lines.push(`Positions from ${listText(archive.surveys)}, going back ${archive.years.toFixed(0)} years, show a steady ${archive.pm.toFixed(2)}″ per year. That is why the engine lists it as a mover.`);
    }
  } else if (object.kind === 'candidate') {
    tone = 'candidate';
    headline = 'Candidate: it may have moved.';
    lines.push(`Appears to have moved ${arcsec(m.shift)} to the ${compass(m.east, m.north)} (${pixelsText(m.pixels)}) ${m.span}.`);
    if (noise) lines.push(noise);
    lines.push(`Its measured speed, ${speed}, is ${times}: above the noise, but short of the 5 the engine asks for. It stays a candidate until another pass or another survey confirms it.`);
  } else if (verdict === 'refuted') {
    tone = 'refuted';
    headline = 'Not a moving object.';
    lines.push(`The SPHEREx positions differ by ${arcsec(m.shift)} (${pixelsText(m.pixels)}) ${m.span}, but older catalogues show a separate, fixed source at each position. The engine linked two neighbours; nothing moved.`);
    if (m.missing.length) lines.push(missingText(m.missing));
    if (sigma >= 3) lines.push(lineNotMotion(object));
  } else {
    tone = 'still';
    const settled = verdict === 'stationary' && archive;
    const quiet = sigma < 3 || (m.noise != null && m.shift <= 3 * m.noise);
    headline = settled ? 'It did not move.' : quiet ? 'No motion detected.' : 'No reliable motion.';
    lines.push(`Its position changed by ${arcsec(m.shift)} (${pixelsText(m.pixels)}) ${m.span}, ${times}.`);
    if (noise) lines.push(noise);
    if (settled) {
      lines.push(`Positions from ${listText(archive.surveys)}, going back ${archive.years.toFixed(0)} years, show no motion either.`);
    } else if (!quiet) {
      if (m.missing.length) {
        lines.push(missingText(m.missing));
        lines.push(lineNotMotion(object));
      } else if (m.seen < m.passes) lines.push('It was not found in every pass, so these positions may belong to different objects.');
      else if (m.pixels < 1) lines.push('That is less than one SPHEREx pixel, too little for the engine to call it a mover.');
      else if (m.seen < 3) lines.push('Only two passes saw it. Two points always fit a line, so the engine asks for more before calling it a mover.');
      else lines.push('The engine did not accept it as a mover.');
    }
  }

  if (catalog && tone !== 'refuted') {
    const off = Math.hypot(catalog.pmEast - object.pm_ra_cosdec, catalog.pmNorth - object.pm_dec);
    const agree = off <= 2 * object.pm_err
      ? 'The two agree within the measurement error.'
      : `They differ by ${off.toFixed(2)}″ per year, ${(off / object.pm_err).toFixed(1)} times the measurement error.`;
    lines.push(`${catalog.catalog} lists ${catalog.name} at ${catalog.pm.toFixed(2)}″ per year to the ${compass(catalog.pmEast, catalog.pmNorth)}; SPHEREx measured ${object.pm_total.toFixed(2)} ± ${object.pm_err.toFixed(2)} to the ${compass(object.pm_ra_cosdec, object.pm_dec)}. ${agree}`);
  }

  const variable = Boolean(object.is_variable && object.variability);
  if (variable) {
    if (tone === 'still') headline = 'Its brightness changed.';
    lines.push(brightnessLine(object.variability));
    if (object.variability.caution) lines.push(`Caution: ${object.variability.caution}.`);
  }
  return { tone, variable, headline, lines };
}

/** Round tick values covering [lo, hi] in steps of 1, 2 or 5 x 10^n, about `target` of them. */
export function ticks(lo, hi, target = 4) {
  const raw = (hi - lo) / target;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((k) => k * pow).find((s) => s >= raw);
  const out = [];
  // "+ 0" turns -0 (from rounding up a small negative) into 0, which would print as "-0".
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.round(v / step) * step + 0);
  return out;
}

/**
 * Everything the position chart draws, in data units (x: MJD, y: arcsec from the first measured
 * position). East and north share one y range, which always reaches one SPHEREx pixel either side
 * of zero: an object that stayed put is then visibly small against the pixel, not magnified noise.
 *   points:  one per measured pass { index, mjd, date, east, north, sigma }
 *   band:    [{ mjd, sigma }] how far fixed stars seem to shift (per axis), zero at the first pass
 *   lines:   [{ kind: 'fit' | 'archive' | 'catalog', label, east: [y0, y1], north: [y0, y1] }]
 *            at the ends of the x range; every line passes through the fitted position
 */
export function motionChart(object, evidence) {
  const seen = evidence.passes.filter((p) => p.point);
  const points = seen.map((p) => ({
    index: p.index, mjd: p.point.mjd, date: p.point.date,
    east: p.point.east_arcsec, north: p.point.north_arcsec, sigma: p.point.sigma_arcsec,
  }));
  const t0 = points[0].mjd, t1 = points[points.length - 1].mjd;
  const pad = Math.max((t1 - t0) * 0.08, 10);
  const x = [t0 - pad, t1 + pad];

  const { fit } = evidence;
  const through = (pmEast, pmNorth) => {
    const at = (t, v0, pm) => v0 + (pm * (t - fit.ref_mjd)) / DAYS_PER_YEAR;
    return { east: x.map((t) => at(t, fit.east_arcsec, pmEast)), north: x.map((t) => at(t, fit.north_arcsec, pmNorth)) };
  };
  // A static object's fitted line is drawn too (it is what the engine computed), but not called a motion.
  const fitLabel = object.kind === 'static' ? 'SPHEREx straight-line fit (not accepted as motion)' : 'SPHEREx fit';
  const lines = [{ kind: 'fit', label: fitLabel, ...through(fit.pm_east, fit.pm_north) }];
  const archive = archiveMotion(object);
  if (archive) lines.push({ kind: 'archive', label: `with ${listText(archive.surveys)}`, ...through(archive.pmEast, archive.pmNorth) });
  const catalog = catalogMotion(object);
  if (catalog) lines.push({ kind: 'catalog', label: `${catalog.catalog} catalogue`, ...through(catalog.pmEast, catalog.pmNorth) });

  const band = [{ mjd: t0, sigma: 0 }];
  for (const p of seen.slice(1)) if (p.field_scatter) band.push({ mjd: p.point.mjd, sigma: p.field_scatter.sigma_arcsec });

  const pixel = evidence.spherex_pixel_arcsec;
  let lo = -pixel, hi = pixel;
  const reach = (v) => { lo = Math.min(lo, v); hi = Math.max(hi, v); };
  for (const p of points) for (const v of [p.east, p.north]) { reach(v - p.sigma); reach(v + p.sigma); }
  // Lines count only within the measured span: far outside it they would squash the data.
  for (const l of lines) {
    for (const series of [l.east, l.north]) {
      const slope = (series[1] - series[0]) / (x[1] - x[0]);
      reach(series[0] + slope * (t0 - x[0]));
      reach(series[0] + slope * (t1 - x[0]));
    }
  }
  const margin = (hi - lo) * 0.08;
  return { x, y: [lo - margin, hi + margin], pixel, points, band: band.length > 1 ? band : null, lines };
}

/**
 * Brightness per pass and detector band, relative to each band's first measured pass:
 * [{ band, points: [{ index, mjd, value, err }] }], bands with at least two passes only.
 */
export function brightnessSeries(evidence) {
  const bands = new Map();
  for (const p of evidence.passes) {
    for (const f of p.point?.fluxes || []) {
      if (!(f.flux > 0)) continue;
      if (!bands.has(f.band)) bands.set(f.band, []);
      bands.get(f.band).push({ index: p.index, mjd: p.point.mjd, flux: f.flux, err: f.flux_err });
    }
  }
  const out = [];
  for (const [band, rows] of bands) {
    if (rows.length < 2) continue;
    const ref = rows[0].flux;
    out.push({ band, points: rows.map((r) => ({ index: r.index, mjd: r.mjd, value: r.flux / ref, err: r.err / ref })) });
  }
  return out.sort((a, b) => a.band.localeCompare(b.band));
}
