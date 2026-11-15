import { separation } from './coords.js';
import { API_BASE, apiJson, runMotion } from './services.js';

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
  const path = `/motion/evidence?ra=${field.ra.toFixed(6)}&dec=${field.dec.toFixed(6)}`
    + `&size_deg=${field.size_deg.toFixed(4)}&object=${id}`;
  let body;
  try {
    body = await apiJson(path, { signal, timeoutMs: EVIDENCE_TIMEOUT_MS });
  } catch (err) {
    if (err?.status === 404) return null;
    throw err;
  }
  if (!body || !Array.isArray(body.passes) || !body.passes.some((p) => p.point)) throw new Error('bad response');
  return body;
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

/**
 * The proof test that needs the archive and catalogue checks of the motion run (on `object`,
 * not in the evidence): do other telescopes agree with what SPHEREx measured?
 * Same shape as the backend's EvidenceCheck: { key, title, status, summary, details }.
 */
export function surveysCheck(object) {
  const title = 'Do other telescopes agree?';
  const archive = archiveMotion(object);
  const catalog = catalogMotion(object);
  const verdict = object.archive?.verdict;
  const movedHere = object.kind === 'mover' || object.kind === 'candidate';
  const statuses = [];
  const sentences = [];
  const details = [];

  if (verdict === 'refuted') {
    statuses.push('fail');
    sentences.push('Older catalogues show a separate, fixed source at each SPHEREx position: two neighbours, not one moving object.');
  } else if (verdict === 'confirmed' && archive) {
    statuses.push('pass');
    sentences.push(`Positions from ${listText(archive.surveys)}, going back ${archive.years.toFixed(0)} years, lie on the same track: ${archive.pm.toFixed(2)}″ per year.`);
  } else if (verdict === 'stationary' && archive) {
    statuses.push(movedHere ? 'fail' : 'pass');
    sentences.push(`Positions from ${listText(archive.surveys)}, going back ${archive.years.toFixed(0)} years, show no motion${movedHere ? ', unlike SPHEREx' : ', as SPHEREx found'}.`);
  }
  if (archive) details.push(`${listText(archive.surveys)} + SPHEREx: ${archive.pm.toFixed(2)} ± ${archive.err.toFixed(2)}″ per year over ${archive.years.toFixed(1)} years`);

  if (catalog) {
    const off = Math.hypot(catalog.pmEast - object.pm_ra_cosdec, catalog.pmNorth - object.pm_dec);
    const agree = off <= 2 * object.pm_err;
    statuses.push(agree ? 'pass' : 'fail');
    sentences.push(agree
      ? `${catalog.catalog} lists ${catalog.name} moving ${catalog.pm.toFixed(2)}″ per year to the ${compass(catalog.pmEast, catalog.pmNorth)}, matching SPHEREx's ${object.pm_total.toFixed(2)} ± ${object.pm_err.toFixed(2)} within its error.`
      : `${catalog.catalog} lists ${catalog.name} moving ${catalog.pm.toFixed(2)}″ per year; SPHEREx measured ${object.pm_total.toFixed(2)} ± ${object.pm_err.toFixed(2)}, ${(off / object.pm_err).toFixed(1)} times its error away.`);
    details.push(`${catalog.catalog}: ${catalog.pmEast.toFixed(3)}″/yr east, ${catalog.pmNorth.toFixed(3)}″/yr north`);
    details.push(`SPHEREx: ${object.pm_ra_cosdec.toFixed(3)}″/yr east, ${object.pm_dec.toFixed(3)}″/yr north`);
  }

  if (!statuses.length) {
    return {
      key: 'other_surveys', title, status: 'unknown', details,
      summary: 'No older survey or catalogue settles its motion, so SPHEREx stands alone here. That is expected for a new candidate.',
    };
  }
  const status = statuses.includes('fail') ? 'fail' : 'pass';
  return { key: 'other_surveys', title, status, summary: sentences.join(' '), details };
}

/** Every proof test for `object`: the backend's (from the images and positions), then the surveys'. */
export function proofChecks(object, evidence) {
  return [...(evidence.checks || []), surveysCheck(object)];
}

/** How the tests came out: { pass, weak, fail, unknown, decided } (decided = all but unknown). */
export function proofTally(checks) {
  const tally = { pass: 0, weak: 0, fail: 0, unknown: 0 };
  for (const c of checks) tally[c.status in tally ? c.status : 'unknown'] += 1;
  return { ...tally, decided: checks.length - tally.unknown };
}

/** "2025-08-27 14:03" -> "27 Aug 14:03" (the year is in the row's label). */
export function dayMonthTime(time) {
  return `${Number(time.slice(8, 10))} ${MONTHS[Number(time.slice(5, 7)) - 1]} ${time.slice(11, 16)}`;
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

// ---------- older surveys (GET {API_BASE}/motion/evidence/surveys, see docs/API.md) ----------

const D2R = Math.PI / 180;
const ARCSEC_RAD = D2R / 3600;
// Requested sky around the object's track: a few SPHEREx pixels beyond both ends.
const SURVEY_MARGIN_ARCSEC = 75;
export const SURVEY_MAX_ARCSEC = 900; // the backend's limit
// The request is planned for the oldest survey: 2MASS observed 1997-2001.
const SURVEY_PLAN_MJD = 51179; // 1999.0
const SURVEY_TIMEOUT_MS = 90000; // two IRSA searches and cutouts, each retried on 5xx

/** Sky position at (east, north) arcsec from (ra0, dec0) on the tangent plane (as the evidence grids). */
export function fromOffsets(ra0, dec0, east, north) {
  const xi = east * ARCSEC_RAD, eta = north * ARCSEC_RAD, d0 = dec0 * D2R;
  const ra = ra0 + Math.atan2(xi, Math.cos(d0) - eta * Math.sin(d0)) / D2R;
  const dec = Math.asin((Math.sin(d0) + eta * Math.cos(d0)) / Math.sqrt(1 + xi * xi + eta * eta)) / D2R;
  return { ra: ((ra % 360) + 360) % 360, dec };
}

/** (east, north) arcsec of (ra, dec) on the tangent plane at (ra0, dec0): the inverse of fromOffsets. */
export function toOffsets(ra0, dec0, ra, dec) {
  const d0 = dec0 * D2R, d = dec * D2R, dra = (ra - ra0) * D2R;
  const cosc = Math.sin(d0) * Math.sin(d) + Math.cos(d0) * Math.cos(d) * Math.cos(dra);
  return {
    east: (Math.cos(d) * Math.sin(dra)) / cosc / ARCSEC_RAD,
    north: (Math.cos(d0) * Math.sin(d) - Math.sin(d0) * Math.cos(d) * Math.cos(dra)) / cosc / ARCSEC_RAD,
  };
}

/**
 * The motion used to say where the object was at an older survey's date, best first: the
 * refit with older catalogues, a catalogued proper motion, then the SPHEREx fit. A static
 * object is not moved (its fitted slope is not a motion). { pmEast, pmNorth, err, source }.
 */
export function predictionMotion(object) {
  if (object.kind === 'static') return { pmEast: 0, pmNorth: 0, err: 0, source: null };
  const archive = archiveMotion(object);
  if (archive && archive.err != null) {
    return { pmEast: archive.pmEast, pmNorth: archive.pmNorth, err: archive.err, source: `the motion measured with ${listText(archive.surveys)}` };
  }
  const catalog = catalogMotion(object);
  if (catalog) return { pmEast: catalog.pmEast, pmNorth: catalog.pmNorth, err: 0, source: `the ${catalog.catalog} proper motion` };
  return { pmEast: object.pm_ra_cosdec, pmNorth: object.pm_dec, err: object.pm_err, source: 'the SPHEREx motion' };
}

/**
 * Where the object was at `mjd`, in the evidence patch's frame (arcsec), with its 1-sigma error.
 * `motion` is an object (MotionObject) or its predictionMotion.
 */
export function predictAt(motion, evidence, mjd) {
  const m = 'pmEast' in motion ? motion : predictionMotion(motion);
  const years = (mjd - evidence.fit.ref_mjd) / DAYS_PER_YEAR;
  return {
    east: evidence.fit.east_arcsec + m.pmEast * years,
    north: evidence.fit.north_arcsec + m.pmNorth * years,
    err: Math.abs(m.err * years), years, source: m.source,
  };
}

/**
 * The square of sky to request from the older surveys for an object (or its predictionMotion): centred between the object's SPHEREx
 * position and where it was in 1999, so one image holds both. { ra, dec, side, east, north }
 * with (east, north) the centre in the evidence patch's frame.
 */
export function surveyPatch(motion, evidence) {
  const now = { east: evidence.fit.east_arcsec, north: evidence.fit.north_arcsec };
  const then = predictAt(motion, evidence, SURVEY_PLAN_MJD);
  const east = (now.east + then.east) / 2, north = (now.north + then.north) / 2;
  const span = Math.hypot(then.east - now.east, then.north - now.north);
  const side = Math.min(SURVEY_MAX_ARCSEC, Math.max(evidence.patch_arcsec, span + 2 * SURVEY_MARGIN_ARCSEC));
  return { ...fromOffsets(evidence.patch_ra, evidence.patch_dec, east, north), side: Math.round(side), east, north };
}

const SURVEY_MIN_ARCSEC = 30; // the backend's limits on side_arcsec

/**
 * Throws a clear error, before any request, for a patch the backend would refuse: a position or
 * side that is not a number (an object without a usable position), or a side out of range.
 */
export function checkPatch(patch, minSide, maxSide) {
  const { ra, dec, side } = patch || {};
  if (![ra, dec, side].every(Number.isFinite) || Math.abs(dec) > 90) {
    throw new Error(`this object has no usable position (ra ${ra}, dec ${dec}, side ${side})`);
  }
  if (side < minSide || side > maxSide) throw new Error(`the patch is ${side}″ wide; the server takes ${minSide}–${maxSide}″`);
}

/**
 * 2MASS and WISE images of a square patch ({ ra, dec, side } with `side` in arcsec): a
 * SurveyImages body (backend/planetx/api/schemas.py), or null without a backend.
 */
export async function fetchPatchSurveys(patch, { signal } = {}) {
  if (!API_BASE) return null;
  checkPatch(patch, SURVEY_MIN_ARCSEC, SURVEY_MAX_ARCSEC);
  const body = await apiJson(
    `/motion/evidence/surveys?ra=${patch.ra.toFixed(6)}&dec=${patch.dec.toFixed(6)}&side_arcsec=${patch.side}`,
    { signal, timeoutMs: SURVEY_TIMEOUT_MS },
  );
  if (!body || !Array.isArray(body.images) || !Array.isArray(body.unavailable)) throw new Error('bad response');
  return body;
}

/**
 * 2MASS and WISE images around the object moving by `motion` (as surveyPatch) with the requested
 * patch added, or null without a backend. Throws on a failed request: the images are an extra, so
 * callers keep showing the SPHEREx evidence.
 */
export async function fetchSurveyImages(motion, evidence, { signal } = {}) {
  const patch = surveyPatch(motion, evidence);
  const body = await fetchPatchSurveys(patch, { signal });
  return body && { ...body, patch };
}

/**
 * The marks on one older survey image, in arcsec from that image's centre (east, north):
 * `then` where the object was at the image's date (with its 1-sigma error), `now` its SPHEREx
 * position, `years` between the two, and `inside` whether `then` lies on the image.
 */
export function surveyMarks(object, evidence, surveys, image) {
  const shift = (p) => {
    const sky = fromOffsets(evidence.patch_ra, evidence.patch_dec, p.east, p.north);
    return toOffsets(surveys.ra, surveys.dec, sky.ra, sky.dec);
  };
  const predicted = predictAt(object, evidence, image.mjd);
  const then = { ...shift(predicted), err: predicted.err };
  const now = shift({ east: evidence.fit.east_arcsec, north: evidence.fit.north_arcsec });
  const half = surveys.patch_arcsec / 2;
  return {
    then, now, years: -predicted.years, source: predicted.source,
    inside: Math.abs(then.east) < half && Math.abs(then.north) < half,
  };
}

/** A round scale-bar length, about a fifth of `side` arcsec. */
export function scaleBar(side) {
  return [10, 20, 30, 60, 120, 180, 300].reduce((best, v) => (Math.abs(v - side / 5) < Math.abs(best - side / 5) ? v : best));
}
