// The telescope pictures of one object, oldest first: 2MASS (~2000), WISE (~2010) and SPHEREx
// (2025 on). Pure logic: which SPHEREx exposures to show, where a catalogued star was on each
// date, and the views the picture viewer steps through. The fetching is at the bottom.
import { propagate } from './coords.js';
import { fromOffsets, passSpan, surveyMarks, toOffsets } from './evidence.js';
import { mjdToYear } from './names.js';
import { API_BASE } from './services.js';

/** SPHEREx pixel of the aligned cutouts (backend planetx.spherex.cutouts.PIXEL_SCALE_ARCSEC). */
export const SPHEREX_PIXEL_ARCSEC = 6.15;
// The backend's limits on a cutout's side (/api/cutout.png size_deg).
const CUTOUT_MIN_DEG = 0.01;
const CUTOUT_MAX_DEG = 0.5;
// Exposures more than this many days apart belong to different passes (the backend's PASS_GAP_DAYS).
const PASS_GAP_DAYS = 30;
// Near the ecliptic poles SPHEREx comes by on most orbits, so there are no gaps: a "pass" there
// is cut into windows of this length.
const PASS_MAX_DAYS = 90;
// One exposure per pass, at most this many: each first-time cutout is ~4 MB from IRSA.
export const MAX_SPHEREX_PICTURES = 4;
// Detector 3 (1.6-2.4 um) sees about what 2MASS Ks sees, so the pictures compare well.
const PREFERRED_DETECTOR = 3;
// The patch is planned for the oldest survey: 2MASS observed 1997-2001.
const PLAN_YEAR = 1999.0;
const MARGIN_ARCSEC = 75; // sky around the track on each side
const MIN_SIDE_ARCSEC = 150;
export const MAX_SIDE_ARCSEC = 900; // /api/motion/evidence/surveys limit
const GAIA_EPOCH = 2016.0;
const FETCH_TIMEOUT_MS = 90000;

export const DETECTOR_BAND = { 1: '0.75–1.1 µm', 2: '1.1–1.65 µm', 3: '1.6–2.4 µm', 4: '2.4–3.85 µm', 5: '3.8–4.4 µm', 6: '4.4–5.0 µm' };
const SURVEY_NAME = { '2mass': '2MASS', wise: 'WISE' };

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const dmy = (date) => `${Number(date.slice(8, 10))} ${MONTHS[Number(date.slice(5, 7)) - 1]} ${date.slice(0, 4)}`;
const my = (date) => `${MONTHS[Number(date.slice(5, 7)) - 1]} ${date.slice(0, 4)}`;

/**
 * SPHEREx exposures (Frame from GET /api/frames) grouped into passes, time-ordered: a new pass
 * starts after a gap of more than `gapDays`, or once a pass spans `maxDays`.
 */
export function groupPasses(frames, { gapDays = PASS_GAP_DAYS, maxDays = PASS_MAX_DAYS } = {}) {
  const sorted = frames.filter((f) => Number.isFinite(f.mjd)).sort((a, b) => a.mjd - b.mjd);
  const passes = [];
  for (const f of sorted) {
    const g = passes[passes.length - 1];
    if (!g || f.mjd - g[g.length - 1].mjd > gapDays || f.mjd - g[0].mjd > maxDays) passes.push([f]);
    else g.push(f);
  }
  return passes;
}

/** At most `max` items of a time-ordered list, evenly spread and always keeping the first and last. */
export function spread(list, max) {
  if (list.length <= max) return list;
  if (max <= 1) return list.slice(0, max);
  const picked = new Set();
  for (let i = 0; i < max; i++) picked.add(Math.round((i * (list.length - 1)) / (max - 1)));
  return [...picked].map((i) => list[i]);
}

/**
 * The exposures to show: one per pass (on the detector nearest the preferred one that the pass
 * has, nearest the middle of the pass), at most `max` passes, time-ordered. Each comes back with
 * `pass` = { start, end, n } (dates of the pass and how many exposures it holds).
 */
export function pickFrames(frames, { detector = PREFERRED_DETECTOR, max = MAX_SPHEREX_PICTURES } = {}) {
  const picked = groupPasses(frames).map((g) => {
    const far = (f) => (f.detector == null ? Infinity : Math.abs(f.detector - detector));
    const nearest = Math.min(...g.map(far));
    const mid = (g[0].mjd + g[g.length - 1].mjd) / 2;
    const best = g.filter((f) => far(f) === nearest)
      .reduce((a, b) => (Math.abs(b.mjd - mid) < Math.abs(a.mjd - mid) ? b : a));
    return { ...best, pass: { start: g[0].date, end: g[g.length - 1].date, n: g.length } };
  });
  return spread(picked, max);
}

const hasPm = (e) => e && Number.isFinite(e.pmra) && Number.isFinite(e.pmdec);

/**
 * How a catalogued object (a known-object row, see knownObjects in names.js) moves: a position
 * at an epoch (decimal year) and a proper motion in mas/yr. Gaia positions are epoch 2016.0;
 * SIMBAD entries arrive already moved to today (`nowYear`). No motion listed: it stays put.
 */
export function knownMotion(row, nowYear) {
  const g = row.gaia, o = row.simbad;
  if (g && hasPm(g)) return { ra: g.ra, dec: g.dec, year: GAIA_EPOCH, pmra: g.pmra, pmdec: g.pmdec, source: 'Gaia DR3' };
  if (o && hasPm(o)) return { ra: o.ra, dec: o.dec, year: nowYear, pmra: o.pmra, pmdec: o.pmdec, source: 'SIMBAD' };
  return { ra: row.ra, dec: row.dec, year: nowYear, pmra: 0, pmdec: 0, source: null };
}

/** Where an object moving by `motion` (knownMotion) was at decimal year `year`. */
export function positionAt(motion, year) {
  return propagate(motion.ra, motion.dec, motion.pmra, motion.pmdec, year - motion.year);
}

/**
 * The square of sky every picture of a catalogued object shows: centred between where it was in
 * 1999 and where it is now, so the oldest and newest positions are both on it. { ra, dec, side }
 * with `side` in arcsec.
 */
export function picturePatch(motion, nowYear) {
  const then = positionAt(motion, PLAN_YEAR);
  const now = positionAt(motion, nowYear);
  const d = toOffsets(now.ra, now.dec, then.ra, then.dec);
  const centre = fromOffsets(now.ra, now.dec, d.east / 2, d.north / 2);
  const span = Math.hypot(d.east, d.north);
  const side = Math.round(Math.min(MAX_SIDE_ARCSEC, Math.max(MIN_SIDE_ARCSEC, span + 2 * MARGIN_ARCSEC)));
  return { ...centre, side };
}

/** The `size_deg` to ask /api/frames and /api/cutout.png for, for a patch `side` arcsec wide. */
export const cutoutSizeDeg = (side) => Number(Math.min(CUTOUT_MAX_DEG, Math.max(CUTOUT_MIN_DEG, side / 3600)).toFixed(5));

/**
 * Side in arcsec of the SPHEREx cutout the backend renders for `sizeDeg`: an odd number of
 * pixels so the centre is a pixel centre (planetx.spherex.cutouts.grid_wcs).
 */
export function cutoutSideArcsec(sizeDeg) {
  const n = Math.max(3, Math.round((sizeDeg * 3600) / SPHEREX_PIXEL_ARCSEC)) | 1;
  return n * SPHEREX_PIXEL_ARCSEC;
}

/** MJD halfway between two YYYY-MM-DD dates (a pass in which the object was not measured). */
const dateMjd = (start, end) => (Date.parse(start) + Date.parse(end)) / 2 / 86400000 + 40587;

const inside = (p, side) => Math.abs(p.east) < side / 2 && Math.abs(p.north) < side / 2;

/**
 * The views of a catalogued object, oldest first: each archive image and each picked SPHEREx
 * exposure, with a ring where the catalogue motion puts the object on that date and a cross
 * where it is in the newest picture. All share the patch's centre, so they blink in register.
 * `surveys` is a SurveyImages body (or null), `frames` the picked SPHEREx frames (or null),
 * `cutoutUrl(frame)` gives a frame's image address.
 */
export function knownViews({ motion, patch, surveys, frames, cutoutUrl }) {
  const at = (mjd) => {
    const p = positionAt(motion, mjdToYear(mjd));
    return toOffsets(patch.ra, patch.dec, p.ra, p.dec);
  };
  const moving = motion.pmra !== 0 || motion.pmdec !== 0;
  const views = [];
  for (const im of surveys?.images || []) {
    const name = SURVEY_NAME[im.survey] || im.survey;
    views.push({
      key: `survey:${im.survey}`, telescope: name, band: im.label, date: im.date, mjd: im.mjd,
      button: `${name} ${im.date.slice(0, 4)}`, when: dmy(im.date), note: im.survey === 'wise' ? 'mean date of its exposures' : null,
      src: `data:image/png;base64,${im.image_png_b64}`, side: surveys.patch_arcsec, pixel: im.pixel_arcsec, at: at(im.mjd),
    });
  }
  const sizeDeg = cutoutSizeDeg(patch.side);
  for (const f of frames || []) {
    views.push({
      key: `spherex:${f.obs_id}:${f.detector}`, telescope: 'SPHEREx',
      band: f.detector ? `D${f.detector} · ${DETECTOR_BAND[f.detector] || ''}`.replace(/ · $/, '') : 'single exposure',
      date: f.date, mjd: f.mjd, button: `SPHEREx ${my(f.date)}`, when: dmy(f.date),
      src: cutoutUrl(f), side: cutoutSideArcsec(sizeDeg), pixel: SPHEREX_PIXEL_ARCSEC, at: at(f.mjd),
      note: f.pass ? `one of ${f.pass.n} exposures in the pass of ${passSpan(f.pass)}` : null,
    });
  }
  views.sort((a, b) => a.mjd - b.mjd);
  const last = views[views.length - 1];
  for (const v of views) {
    v.marks = { ring: inside(v.at, v.side) ? v.at : null, ref: moving && last && v !== last ? last.at : null, tone: 'catalogue' };
    v.outside = !inside(v.at, v.side);
  }
  return views;
}

/**
 * The views of an engine object, oldest first: the archive images (2MASS, WISE) with where its
 * motion puts it then, and the SPHEREx image of each pass with where the engine measured it.
 * Extras (not blinked): the difference image. `surveys` is the fetchSurveyImages answer or null.
 */
export function engineViews(object, evidence, surveys) {
  const side = evidence.patch_arcsec;
  const pos = (point) => ({ east: point.east_arcsec, north: point.north_arcsec });
  const first = evidence.passes.find((p) => p.point);
  const views = [];
  for (const im of surveys?.images || []) {
    const mark = surveyMarks(object, evidence, surveys, im);
    const name = SURVEY_NAME[im.survey] || im.survey;
    views.push({
      key: `survey:${im.survey}`, telescope: name, band: im.label, date: im.date, mjd: im.mjd,
      button: `${name} ${im.date.slice(0, 4)}`, when: dmy(im.date), note: im.survey === 'wise' ? 'mean date of its exposures' : null,
      src: `data:image/png;base64,${im.image_png_b64}`, side: surveys.patch_arcsec, pixel: im.pixel_arcsec,
      at: mark.then, outside: !mark.inside, source: mark.source,
      marks: {
        ring: mark.inside ? mark.then : null, err: mark.inside ? mark.then.err : 0,
        ref: mark.now, line: mark.source ? [mark.then, mark.now] : null, tone: 'measured',
      },
    });
  }
  for (const p of evidence.passes) {
    const date = p.point?.date ?? p.start;
    views.push({
      key: `pass:${p.index}`, telescope: 'SPHEREx', band: `median of ${p.n_frames} exposures`, date,
      mjd: p.point?.mjd ?? dateMjd(p.start, p.end), button: `SPHEREx ${my(date)}`, when: passSpan(p),
      src: `data:image/png;base64,${p.image_png_b64}`, side, pixel: evidence.patch_pixel_arcsec,
      at: p.point ? pos(p.point) : null, outside: false, missed: !p.point,
      note: `pass ${p.index + 1} of ${evidence.passes.length}`,
      marks: { ring: p.point ? pos(p.point) : null, ref: first ? pos(first.point) : null, tone: 'measured' },
    });
  }
  const ordered = views.sort((x, y) => x.mjd - y.mjd);
  const [a, b] = evidence.difference_passes;
  const extras = [{
    key: 'difference', telescope: 'SPHEREx', band: 'difference', button: 'Difference',
    when: `${passSpan(evidence.passes[b])} minus ${passSpan(evidence.passes[a])}`,
    src: `data:image/png;base64,${evidence.difference_png_b64}`, side, pixel: evidence.patch_pixel_arcsec,
    marks: {
      ring: evidence.passes[b].point ? pos(evidence.passes[b].point) : null,
      was: evidence.passes[a].point ? pos(evidence.passes[a].point) : null, tone: 'measured',
    },
  }];
  return { views: ordered, extras };
}

// ---------- fetching (GET /api/frames, /api/motion/evidence/surveys) ----------

function withTimeout(signal, ms) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(new DOMException('Request timed out', 'TimeoutError')), ms);
  const onAbort = () => ctrl.abort(signal.reason);
  if (signal?.aborted) ctrl.abort(signal.reason);
  else signal?.addEventListener('abort', onAbort, { once: true });
  return { signal: ctrl.signal, done: () => { clearTimeout(timer); signal?.removeEventListener('abort', onAbort); } };
}

async function getJson(path, signal) {
  const t = withTimeout(signal, FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_BASE}${path}`, { signal: t.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    if (err?.name === 'TimeoutError') throw new Error('the request timed out', { cause: err });
    throw err;
  } finally {
    t.done();
  }
}

/** 2MASS and WISE images of a patch ({ ra, dec, side }): a SurveyImages body, or null without a backend. */
export async function fetchPatchSurveys(patch, { signal } = {}) {
  if (!API_BASE) return null;
  const body = await getJson(`/motion/evidence/surveys?ra=${patch.ra.toFixed(6)}&dec=${patch.dec.toFixed(6)}&side_arcsec=${patch.side}`, signal);
  if (!body || !Array.isArray(body.images) || !Array.isArray(body.unavailable)) throw new Error('bad response');
  return body;
}

/** The SPHEREx exposures to show for a patch (pickFrames of GET /api/frames), or null without a backend. */
export async function fetchPatchFrames(patch, { signal } = {}) {
  if (!API_BASE) return null;
  const body = await getJson(`/frames?ra=${patch.ra.toFixed(6)}&dec=${patch.dec.toFixed(6)}&size_deg=${cutoutSizeDeg(patch.side)}`, signal);
  if (!body || !Array.isArray(body.frames)) throw new Error('bad response');
  return pickFrames(body.frames);
}

/** Absolute address of a frame's PNG: its `image_url` is a path on the API's host ("/api/cutout.png?..."). */
export function frameImageUrl(frame, base = API_BASE, page = globalThis.location?.href) {
  return new URL(frame.image_url, new URL(base, page)).href;
}
