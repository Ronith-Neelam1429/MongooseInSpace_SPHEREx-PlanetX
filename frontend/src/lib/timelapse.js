// The time-lapse of a catalogued star: real images of the same square of sky from 2MASS (~2000),
// AllWISE (~2010) and SPHEREx (one exposure per pass, 2025 on), with the star's catalogued motion
// marked on each. Independent of the motion engine: it needs only the star's position and proper
// motion. Endpoints: GET /frames and GET /motion/evidence/surveys (docs/API.md).
import { epochNow, propagate, snapCentre } from './coords.js';
import { toOffsets } from './evidence.js';
import { mjdToYear } from './names.js';
import { API_BASE, plainError } from './services.js';

/** The patch reaches back to 2MASS: it observed 1997-2001. */
const OLDEST_YEAR = 1999.5;
/** Sky kept around the star's path on each side, arcsec: room to see the field stars it passes. */
const MARGIN_ARCSEC = 90;
const MIN_SIDE_ARCSEC = 180;
const MAX_SIDE_ARCSEC = 900; // the surveys endpoint's limit
/** SPHEREx's band closest to 2MASS Ks (2.2 µm), so the images look alike. */
export const SPHEREX_DETECTOR = 3;
/** A new SPHEREx pass starts after a gap this long (as the API's /visits). */
const PASS_GAP_DAYS = 30;
const MAX_TRIES = 4;
const TIMEOUT_MS = 120000; // first-time cutouts are ~4 MB each from IRSA

/** Where a known-object row (lib/names.js) is at `year`; rows without a motion stay put. */
export function rowAt(row, year) {
  if (row.pmra == null || row.pmdec == null) return { ra: row.ra, dec: row.dec };
  return propagate(row.ra, row.dec, row.pmra, row.pmdec, year - row.epoch);
}

/**
 * The square of sky for the time-lapse: centred between where the star was in 1999.5 and where it
 * is now, wide enough for both. { ra, dec, side (arcsec), span (arcsec moved since 1999.5) }.
 * A path longer than the largest patch is centred on today's position (the old one falls outside).
 */
export function timelapsePatch(row, nowYear = epochNow()) {
  const then = rowAt(row, OLDEST_YEAR);
  const now = rowAt(row, nowYear);
  const d = toOffsets(now.ra, now.dec, then.ra, then.dec);
  const span = Math.hypot(d.east, d.north);
  const want = span + 2 * MARGIN_ARCSEC;
  const side = Math.round(Math.min(MAX_SIDE_ARCSEC, Math.max(MIN_SIDE_ARCSEC, want)));
  const centre = snapCentre(want > MAX_SIDE_ARCSEC ? now : midpoint(then, now));
  return { ra: centre.ra, dec: centre.dec, side, span };
}

function midpoint(a, b) {
  // Fine for the few arcminutes a star moves: average on the sphere via unit vectors.
  const v = [a, b].map(({ ra, dec }) => {
    const r = (ra * Math.PI) / 180, d = (dec * Math.PI) / 180;
    return [Math.cos(d) * Math.cos(r), Math.cos(d) * Math.sin(r), Math.sin(d)];
  });
  const [x, y, z] = [0, 1, 2].map((i) => v[0][i] + v[1][i]);
  const ra = (Math.atan2(y, x) * 180) / Math.PI;
  return { ra: (ra + 360) % 360, dec: (Math.atan2(z, Math.hypot(x, y)) * 180) / Math.PI };
}

/**
 * One SPHEREx frame per survey pass: the frame nearest the middle of the pass. `frames` are the
 * API's Frame objects (any order). Returns them oldest first, each with `pass` (1-based) and
 * `n_pass` (frames in that pass).
 */
export function framePerPass(frames) {
  const sorted = [...frames].sort((a, b) => a.mjd - b.mjd);
  const passes = [];
  for (const f of sorted) {
    const last = passes[passes.length - 1];
    if (last && f.mjd - last[last.length - 1].mjd <= PASS_GAP_DAYS) last.push(f);
    else passes.push([f]);
  }
  return passes.map((p, i) => {
    const mid = (p[0].mjd + p[p.length - 1].mjd) / 2;
    const best = p.reduce((a, b) => (Math.abs(b.mjd - mid) < Math.abs(a.mjd - mid) ? b : a));
    return { ...best, pass: i + 1, n_pass: p.length };
  });
}

/**
 * Marks for one image of the patch, in arcsec from the patch centre (east, north): `at` where
 * the catalogue puts the star on the image's date, `path` its positions at every image's date
 * (for the track), and whether `at` lies on the image.
 */
export function marksFor(row, patch, year, years) {
  const off = (y) => {
    const p = rowAt(row, y);
    return toOffsets(patch.ra, patch.dec, p.ra, p.dec);
  };
  const at = off(year);
  const half = patch.side / 2;
  return {
    at,
    path: years.map(off),
    inside: Math.abs(at.east) < half && Math.abs(at.north) < half,
  };
}

/** How far the star moved between two years, in words: "4.5′" or "8.2″". */
export function shiftText(arcsec) {
  if (arcsec >= 60) return `${(arcsec / 60).toFixed(1)}′`;
  return `${arcsec >= 10 ? arcsec.toFixed(0) : arcsec.toFixed(1)}″`;
}

// ---------- requests ----------

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  const t = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(t); reject(signal.reason); }, { once: true });
});

/**
 * fetch() that retries while the API says it is busy (503, honouring Retry-After) and times out.
 * Rejects with `HTTP <status>` on any other failure.
 */
async function fetchRetry(url, { signal } = {}) {
  for (let attempt = 1; ; attempt++) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(new DOMException('Request timed out', 'TimeoutError')), TIMEOUT_MS);
    const onAbort = () => ctrl.abort(signal.reason);
    if (signal?.aborted) ctrl.abort(signal.reason);
    else signal?.addEventListener('abort', onAbort, { once: true });
    let res;
    try {
      res = await fetch(url, { signal: ctrl.signal });
    } catch (err) {
      if (err?.name === 'TimeoutError') throw new Error('the request timed out', { cause: err });
      throw err;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
    }
    if (res.ok) return res;
    if (res.status === 503 && attempt < MAX_TRIES) {
      const wait = Number(res.headers.get('Retry-After'));
      await sleep((Number.isFinite(wait) && wait > 0 ? wait : 3) * 1000, signal);
      continue;
    }
    let detail = '';
    try { detail = (await res.json())?.detail; } catch { /* not JSON */ }
    throw Object.assign(new Error(typeof detail === 'string' && detail ? plainError(detail) : `HTTP ${res.status}`), { status: res.status });
  }
}

/** An API path like "/api/cutout.png?..." as a full URL on the API's origin. */
export function apiUrl(path, base = API_BASE) {
  return new URL(path, new URL(base, globalThis.location?.href ?? 'http://localhost/')).href;
}

/** 2MASS and AllWISE images of the patch (SurveyImages in the API), oldest first. */
export async function fetchArchive(patch, { signal } = {}) {
  // `grey=noise` is ignored by the API; it only keeps browsers from reusing day-cached images
  // rendered with the older percentile grey levels.
  const res = await fetchRetry(
    `${API_BASE}/motion/evidence/surveys?ra=${patch.ra.toFixed(6)}&dec=${patch.dec.toFixed(6)}&side_arcsec=${patch.side}&grey=noise`,
    { signal },
  );
  const body = await res.json();
  if (!Array.isArray(body?.images) || !Array.isArray(body.unavailable)) throw new Error('bad response');
  return body;
}

/** SPHEREx frames of the patch, one per pass, on the patch's grid (Frame objects with `pass`). */
export async function fetchSpherexFrames(patch, { signal } = {}) {
  const size = Math.max(0.01, patch.side / 3600);
  const url = (detector) => `${API_BASE}/frames?ra=${patch.ra.toFixed(6)}&dec=${patch.dec.toFixed(6)}&size_deg=${size.toFixed(5)}${detector ? `&detector=${detector}` : ''}`;
  let body = await (await fetchRetry(url(SPHEREX_DETECTOR), { signal })).json();
  // Another detector is better than none when this one missed the spot.
  if (Array.isArray(body?.frames) && body.frames.length === 0) body = await (await fetchRetry(url(null), { signal })).json();
  if (!Array.isArray(body?.frames)) throw new Error('bad response');
  return framePerPass(body.frames);
}

/**
 * One SPHEREx cutout as float pixels (CutoutData in the API): { width, height, pixels }, row 0
 * north, column 0 east, NaN = no data. Floats rather than the PNG: the PNG's percentile stretch
 * clips faint stars, and measuring needs the real values.
 */
export async function fetchFrameData(frame, { signal } = {}) {
  const res = await fetchRetry(apiUrl(frame.data_url), { signal });
  const body = await res.json();
  if (!(body?.width > 0 && body?.height > 0) || typeof body.data_b64 !== 'string') throw new Error('bad response');
  return { width: body.width, height: body.height, pixels: decodeFloats(body.data_b64, body.width * body.height) };
}

/** base64 of little-endian float32 values (every browser is little-endian) as a Float32Array. */
export function decodeFloats(b64, count) {
  const bin = atob(b64);
  if (bin.length !== count * 4) throw new Error('bad response');
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Float32Array(bytes.buffer);
}

/** Decimal year of a frame or archive image (both carry `mjd`). */
export const yearOf = (image) => mjdToYear(image.mjd);
