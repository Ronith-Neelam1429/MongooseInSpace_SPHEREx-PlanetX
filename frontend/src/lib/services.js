import { epochNow, propagate, separation } from './coords.js';
import { profileQuery } from './profile.js';

// Client-side access to SIMBAD, VizieR (Gaia DR3), Sesame and the project backend.
// CDS hosts mirrors; we try them in order and remember whichever answered last.

const SIMBAD_TAP = [
  'https://simbad.cds.unistra.fr/simbad/sim-tap/sync',
  'https://simbad.cfa.harvard.edu/simbad/sim-tap/sync',
];
const VIZIER = ['https://vizier.cds.unistra.fr', 'https://vizier.cfa.harvard.edu'];

export const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '');

const preferred = new Map();

function withTimeout(signal, ms) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(new DOMException('Request timed out', 'TimeoutError')), ms);
  const onAbort = () => ctrl.abort(signal.reason);
  if (signal) {
    if (signal.aborted) ctrl.abort(signal.reason);
    else signal.addEventListener('abort', onAbort, { once: true });
  }
  return { signal: ctrl.signal, done: () => { clearTimeout(timer); signal?.removeEventListener('abort', onAbort); } };
}

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  const t = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(t); reject(signal.reason); }, { once: true });
});

/**
 * Try each mirror in turn, twice (public services fail transiently), starting with the one that
 * answered last. `build(base)` returns [url, init]; `parse(res)` validates and parses.
 */
async function fromMirrors(key, mirrors, build, parse, { signal, timeoutMs = 15000, rounds = 2 } = {}) {
  const start = preferred.get(key) || 0;
  const order = mirrors.map((_, i) => (start + i) % mirrors.length);
  let lastErr;
  for (let round = 0; round < rounds; round++) {
    if (round) await sleep(800, signal);
    for (const i of order) {
      if (signal?.aborted) throw signal.reason;
      const t = withTimeout(signal, timeoutMs);
      try {
        const [url, init] = build(mirrors[i]);
        const res = await fetch(url, { ...init, signal: t.signal });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const out = await parse(res);
        preferred.set(key, i);
        return out;
      } catch (err) {
        if (signal?.aborted) throw signal.reason;
        lastErr = err;
      } finally {
        t.done();
      }
    }
  }
  const reason = lastErr?.name === 'TimeoutError' ? 'timed out' : lastErr?.message || 'network error';
  throw new Error(`no ${key} mirror answered (${reason})`);
}

function tap(query) {
  return (base) => [base, {
    method: 'POST',
    body: new URLSearchParams({ REQUEST: 'doQuery', LANG: 'ADQL', FORMAT: 'json', QUERY: query }),
  }];
}

async function tapJson(res) {
  const j = await res.json();
  if (!j || !Array.isArray(j.metadata) || !Array.isArray(j.data)) throw new Error('bad TAP response');
  const cols = j.metadata.map((m) => m.name);
  return j.data.map((row) => Object.fromEntries(cols.map((c, i) => [c, row[i]])));
}

const num = (x) => (x === null || x === undefined || x === '' ? null : Number(x));

// The fastest stars move up to ~10"/yr, so search a little wider and filter after propagating.
const PM_MARGIN_DEG = 0.09;

/**
 * Known objects from SIMBAD within radiusDeg of (ra, dec), nearest first. SIMBAD positions are
 * epoch J2000; objects with proper motion are moved to the current epoch before filtering.
 */
export async function querySimbad(ra, dec, radiusDeg, { limit = 200, signal } = {}) {
  const searchR = radiusDeg + PM_MARGIN_DEG;
  const years = epochNow() - 2000;
  const q = `SELECT TOP ${limit * 3} b.main_id, b.otype, d.description, d.path, b.ra, b.dec, b.pmra, b.pmdec, f.V, f.G, f.K,
    DISTANCE(POINT('ICRS', b.ra, b.dec), POINT('ICRS', ${ra}, ${dec})) AS dist
    FROM basic AS b LEFT JOIN otypedef AS d ON b.otype = d.otype LEFT JOIN allfluxes AS f ON b.oid = f.oidref
    WHERE CONTAINS(POINT('ICRS', b.ra, b.dec), CIRCLE('ICRS', ${ra}, ${dec}, ${searchR})) = 1
    ORDER BY dist`;
  const rows = await fromMirrors('SIMBAD', SIMBAD_TAP, tap(q), tapJson, { signal });
  return rows.map((r) => ({
    name: String(r.main_id).replace(/^NAME /, '').replace(/\s+/g, ' '),
    otype: r.otype,
    type: r.description || r.otype,
    path: r.path || null, // place in SIMBAD's type tree, e.g. "* > Ev* > AB* > Mi*"
    ra: num(r.ra), dec: num(r.dec),
    pmra: num(r.pmra), pmdec: num(r.pmdec),
    mag: num(r.V) ?? num(r.G) ?? num(r.K),
    band: r.V != null ? 'V' : r.G != null ? 'G' : r.K != null ? 'K' : null,
  })).filter((r) => r.ra !== null && r.dec !== null).map((r) => {
    const p = r.pmra != null && r.pmdec != null ? propagate(r.ra, r.dec, r.pmra, r.pmdec, years) : r;
    return { ...r, ra: p.ra, dec: p.dec, dist: separation(ra, dec, p.ra, p.dec) };
  }).filter((r) => r.dist <= radiusDeg).sort((a, b) => a.dist - b.dist).slice(0, limit);
}

/**
 * Parse VizieR asu-tsv output (header row, units row, dashes row, data). An empty result still
 * carries a '#RESOURCE' block; anything else (error or outage pages) is rejected.
 */
export function parseVizierTsv(text) {
  const lines = text.split(/\r?\n/).filter((l) => l && !l.startsWith('#'));
  const dash = lines.findIndex((l) => /^-+(\t-+)*\s*$/.test(l));
  if (dash < 1) {
    if (/^#RESOURCE=/m.test(text) && !lines.length) return [];
    throw new Error('unexpected VizieR response');
  }
  const cols = lines[0].split('\t').map((c) => c.trim());
  return lines.slice(dash + 1).map((l) => {
    const v = l.split('\t');
    return Object.fromEntries(cols.map((c, i) => [c, (v[i] ?? '').trim()]));
  });
}

/** Gaia DR3 sources (epoch 2016.0) within radiusDeg, brightest first. */
export async function queryGaia(ra, dec, radiusDeg, { limit = 1500, signal } = {}) {
  const params = new URLSearchParams({
    '-source': 'I/355/gaiadr3',
    '-c': `${ra.toFixed(6)} ${dec >= 0 ? '+' : ''}${dec.toFixed(6)}`,
    '-c.rd': radiusDeg.toFixed(5),
    '-out': 'Source,RA_ICRS,DE_ICRS,pmRA,pmDE,Plx,e_Plx,Gmag',
    '-out.max': String(limit),
    '-sort': 'Gmag',
  });
  const rows = await fromMirrors('VizieR', VIZIER,
    (base) => [`${base}/viz-bin/asu-tsv?${params}`, {}],
    async (res) => parseVizierTsv(await res.text()),
    { signal, timeoutMs: 25000 });
  return rows.map((r) => ({
    id: r.Source,
    ra: num(r.RA_ICRS), dec: num(r.DE_ICRS),
    pmra: num(r.pmRA), pmdec: num(r.pmDE),
    plx: num(r.Plx), plxErr: num(r.e_Plx), gmag: num(r.Gmag),
  })).filter((r) => Number.isFinite(r.ra) && Number.isFinite(r.dec));
}

/** Resolve an object name to coordinates with CDS Sesame (SIMBAD, NED, VizieR). */
export async function resolveName(name, { signal } = {}) {
  const path = `/viz-bin/nph-sesame/-oxp/SNV?${encodeURIComponent(name.trim())}`;
  return fromMirrors('Sesame', VIZIER, (base) => [base + path, {}], async (res) => {
    const xml = await res.text();
    if (!/<Sesame/i.test(xml)) throw new Error('unexpected Sesame response');
    const ra = xml.match(/<jradeg>\s*([-\d.eE+]+)/);
    const dec = xml.match(/<jdedeg>\s*([-\d.eE+]+)/);
    if (!ra || !dec) return null;
    const oname = xml.match(/<oname>\s*([^<]+)/);
    return { ra: Number(ra[1]), dec: Number(dec[1]), name: oname ? oname[1].trim().replace(/^NAME /, '') : name };
  }, { signal, timeoutMs: 12000 });
}

/**
 * GET a JSON document from the AstroSearchX API; rejects with `HTTP <status>` (also set as `err.status`),
 * or "the request timed out" after `timeoutMs`. An abort through `signal` rejects with its reason.
 */
export async function apiJson(path, { signal, timeoutMs = 30000 } = {}) {
  const t = withTimeout(signal, timeoutMs);
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, { signal: t.signal });
  } catch (err) {
    t.done();
    if (err?.name === 'TimeoutError') throw new Error('the request timed out', { cause: err });
    if (signal?.aborted || err?.name === 'AbortError') throw err;
    // fetch() rejects with a bare TypeError ("Failed to fetch") when no answer can be read:
    // the server is down or restarting, or it does not allow this page's origin (CORS).
    throw Object.assign(new Error(unreachable(), { cause: err }), { network: true });
  }
  try {
    if (!res.ok) {
      const detail = await res.json().then(detailText, () => null);
      const message = res.status === 422
        ? `the server refused the request (HTTP 422${detail ? `: ${detail}` : ''})`
        : `HTTP ${res.status}${detail ? `: ${detail}` : ''}`;
      throw Object.assign(new Error(message), { status: res.status });
    }
    return await res.json();
  } catch (err) {
    if (err?.name === 'TimeoutError') throw new Error('the request timed out', { cause: err });
    throw err;
  } finally {
    t.done();
  }
}

/** Why no answer could be read from the API, for people. */
function unreachable() {
  let host = API_BASE;
  try { host = new URL(API_BASE, globalThis.location?.href).host; } catch { /* keep API_BASE as written */ }
  const origin = globalThis.location?.origin;
  return `could not reach the AstroSearchX server at ${host}: it is down or restarting, or it does not allow `
    + `${origin ? `this page (${origin})` : 'this page'} in PLANETX_CORS_ORIGINS`;
}

/**
 * A server's error reason fit to show people. Upstream services (IRSA behind a proxy) answer
 * failures with an HTML page, which the backend passes on inside its reason; that page is cut
 * down to its <title> ("HTTP 502 (Proxy Error)"), and the rest is kept to one short line.
 */
export function plainError(text, max = 160) {
  let s = String(text ?? '');
  const html = s.search(/<(!doctype|html|head|body)\b/i);
  if (html >= 0) {
    const title = /<title>\s*([^<]*?)\s*<\/title>/i.exec(s.slice(html))?.[1].replace(/^\d{3}\s+/, '');
    s = s.slice(0, html).replace(/[\s:]+$/, '') + (title ? ` (${title})` : '');
  }
  s = s.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  return s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s;
}

/** The `detail` of a FastAPI error body in words: a string, or the first validation error. */
export function detailText(body) {
  const d = body?.detail;
  if (typeof d === 'string') return plainError(d);
  if (Array.isArray(d) && d[0]?.msg) {
    const field = Array.isArray(d[0].loc) ? d[0].loc[d[0].loc.length - 1] : null;
    return field ? `${field}: ${d[0].msg}` : d[0].msg;
  }
  return null;
}

/**
 * How often SPHEREx has imaged a position (GET {API_BASE}/visits, `VisitsResponse` in the API):
 * { count, passes: [{ start, end, n_visits }], visits: [{ date, detector, wavelength_min_um, wavelength_max_um }] }
 * with YYYY-MM-DD dates, one pass entry per survey pass.
 * Returns null when no backend is configured.
 */
export async function fetchVisits(ra, dec, { signal } = {}) {
  if (!API_BASE) return null;
  const j = await apiJson(`/visits?ra=${ra.toFixed(6)}&dec=${dec.toFixed(6)}`, { signal, timeoutMs: 60000 });
  if (!Array.isArray(j?.passes) || !Number.isFinite(j.n_visits)) throw new Error('bad response');
  // Each visit cut to what the coverage chart uses (lib/coverage.js): when, and on which detector.
  const visits = (Array.isArray(j.visits) ? j.visits : []).map((v) => ({
    date: v.date, detector: v.detector ?? null, wavelength_min_um: v.wavelength_min_um ?? null, wavelength_max_um: v.wavelength_max_um ?? null,
  }));
  return { count: j.n_visits, passes: j.passes, visits };
}

/**
 * Candidates found by the offline survey (GET {API_BASE}/candidates), most interesting first:
 * { total, candidates: [{ id, ra, dec, name, classification, pm_total, position_angle_deg, score, found, ... }] }.
 * `classification` narrows the list to one class; `hideFake` leaves out those whose motion
 * verdict is fake motion (`motion_verdict.verdict`). Returns null when the list cannot be had: no
 * backend configured, or a backend that does not serve the route yet (404).
 */
export async function fetchCandidates({ limit = 100, offset = 0, classification, sort, hideFake, signal } = {}) {
  if (!API_BASE) return null;
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (classification) params.set('classification', classification);
  if (sort && sort !== 'score') params.set('sort', sort);
  if (hideFake) params.set('hide_fake', 'true');
  let j;
  try {
    j = await apiJson(`/candidates?${params}`, { signal });
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
  if (!Array.isArray(j?.candidates)) throw new Error('bad response');
  return { total: Number.isFinite(j.total) ? j.total : j.candidates.length, candidates: j.candidates };
}

/**
 * A small SPHEREx picture of a survey candidate (GET {API_BASE}/candidates/{id}/thumbnail.png),
 * cut from the frames the survey already downloaded; null without a backend.
 */
export function candidateThumbnailUrl(id) {
  return API_BASE ? `${API_BASE}/candidates/${encodeURIComponent(id)}/thumbnail.png` : null;
}

/** How many of the patches still to search are drawn: the crawl's next stretch, not the whole sky. */
export const QUEUED_SHOWN = 300;

/**
 * The offline survey so far: { status, coverage: { done, failed, queued } }. `status` is the API's
 * SurveyStatus (patch counts and how far the crawl has got). Each coverage layer is an Aladin MOC
 * JSON ({ "<order>": [ipix, ...] }), or null when it has no patch: finished, given up on after
 * retries, and the next QUEUED_SHOWN the crawl will search (the one in progress first).
 * Returns null without a backend.
 */
export async function fetchSurvey({ signal } = {}) {
  if (!API_BASE) return null;
  const [status, done, failed, queued] = await Promise.all([
    apiJson('/survey/status', { signal }),
    apiJson('/survey/coverage', { signal }),
    apiJson('/survey/coverage?status=failed', { signal }),
    apiJson(`/survey/coverage?status=queued&limit=${QUEUED_SHOWN}`, { signal }),
  ]);
  if (!status?.counts) throw new Error('bad response');
  return { status, coverage: { done: mocOrNull(done), failed: mocOrNull(failed), queued: mocOrNull(queued) } };
}

function mocOrNull(moc) {
  if (!moc || typeof moc !== 'object' || Array.isArray(moc)) throw new Error('bad response');
  const empty = Object.values(moc).every((cells) => !Array.isArray(cells) || cells.length === 0);
  return empty ? null : moc;
}

const MOTION_POLL_MS = 2500;

/**
 * Run the AstroSearchX motion engine on SPHEREx frames of a position (GET {API_BASE}/motion).
 * The backend works in the background (it downloads tens of frames on first use), so this polls
 * until the run ends. `onProgress` gets { stage, frames_done, frames_total } on every poll.
 * Resolves to the MotionResult (see backend/planetx/api/schemas.py) plus `field: { ra, dec, size_deg }`,
 * the field as the backend analysed it (pass it back to ask about this run's objects), or null when
 * no backend is configured; rejects with the backend's reason when the run fails.
 */
export async function runMotion(ra, dec, sizeDeg, { signal, onProgress } = {}) {
  if (!API_BASE) return null;
  const url = `${API_BASE}/motion?ra=${ra.toFixed(6)}&dec=${dec.toFixed(6)}&size_deg=${sizeDeg.toFixed(4)}`;
  for (;;) {
    const t = withTimeout(signal, 30000);
    let job;
    try {
      const res = await fetch(url, { signal: t.signal });
      if (res.status === 503) throw new Error('the engine is busy with other fields; try again shortly');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      job = await res.json();
    } finally {
      t.done();
    }
    if (job.status === 'done' && job.result) {
      return { ...job.result, field: { ra: job.ra, dec: job.dec, size_deg: job.size_deg } };
    }
    if (job.status === 'failed') throw new Error(job.error ? plainError(job.error) : 'the run failed');
    if (job.status !== 'running') throw new Error('bad response');
    onProgress?.(job);
    await sleep(MOTION_POLL_MS, signal);
  }
}

/**
 * Measure a SPHEREx spectrum of one object (GET {API_BASE}/spectrum), polling like runMotion: the
 * backend fetches tens of exposures on first use. `target` is { ra, dec, pmra, pmdec, epoch } with
 * motions in mas/yr and `epoch` the decimal year of ra/dec (motion and epoch optional). `onProgress`
 * gets { stage, frames_done, frames_total }. Resolves to the SpectrumResult
 * (backend/planetx/api/schemas.py), or null when no backend is configured; rejects with the
 * backend's reason when the run fails.
 */
export async function runSpectrum(target, { signal, onProgress } = {}) {
  if (!API_BASE) return null;
  const { ra, dec, pmra = 0, pmdec = 0, epoch } = target;
  const moving = Number.isFinite(pmra) && Number.isFinite(pmdec) && (pmra !== 0 || pmdec !== 0);
  let url = `${API_BASE}/spectrum?ra=${ra.toFixed(6)}&dec=${dec.toFixed(6)}`;
  if (moving) url += `&pmra=${pmra.toFixed(1)}&pmdec=${pmdec.toFixed(1)}&epoch=${(epoch ?? epochNow()).toFixed(3)}`;
  for (;;) {
    const t = withTimeout(signal, 30000);
    let job;
    try {
      const res = await fetch(url, { signal: t.signal });
      if (res.status === 503) throw new Error('the server is busy with other spectra; try again shortly');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      job = await res.json();
    } finally {
      t.done();
    }
    if (job.status === 'done' && job.result) return job.result;
    if (job.status === 'failed') throw new Error(job.error ? plainError(job.error) : 'the measurement failed');
    if (job.status !== 'running') throw new Error('bad response');
    onProgress?.(job);
    await sleep(MOTION_POLL_MS, signal);
  }
}

/**
 * Everything known about one object for its card's "About this object" section
 * (GET {API_BASE}/object/profile, `ObjectProfile` in backend/planetx/api/schemas.py). `params` is
 * { ra, dec, gaia_id, name, pmra, pmdec, pm_err, mag } with motions in mas/yr, all but ra/dec
 * optional (see profileQuery in lib/profile.js). Resolves to the profile, whose fields may each be
 * null, or null when no backend is configured; rejects without a usable position.
 */
export async function fetchProfile(params, { signal } = {}) {
  if (!API_BASE) return null;
  const query = profileQuery(params);
  if (!query) throw new Error('no position to look up');
  const j = await apiJson(`/object/profile?${query}`, { signal, timeoutMs: 60000 });
  if (!j || typeof j !== 'object' || Array.isArray(j)) throw new Error('bad response');
  return j;
}
