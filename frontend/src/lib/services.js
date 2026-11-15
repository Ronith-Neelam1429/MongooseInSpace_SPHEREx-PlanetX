import { decimalYear, propagate, separation } from './coords.js';

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
  const years = decimalYear() - 2000;
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
 * GET a JSON document from the PlanetX API; rejects with `HTTP <status>` (also set as `err.status`),
 * or "the request timed out" after `timeoutMs`. An abort through `signal` rejects with its reason.
 */
export async function apiJson(path, { signal, timeoutMs = 30000 } = {}) {
  const t = withTimeout(signal, timeoutMs);
  try {
    const res = await fetch(`${API_BASE}${path}`, { signal: t.signal });
    if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { status: res.status });
    return await res.json();
  } catch (err) {
    if (err?.name === 'TimeoutError') throw new Error('the request timed out', { cause: err });
    throw err;
  } finally {
    t.done();
  }
}

/**
 * How often SPHEREx has imaged a position (GET {API_BASE}/visits, `VisitsResponse` in the API):
 * { count, passes: [{ start, end, n_visits }] } with YYYY-MM-DD dates, one entry per survey pass.
 * Returns null when no backend is configured.
 */
export async function fetchVisits(ra, dec, { signal } = {}) {
  if (!API_BASE) return null;
  const j = await apiJson(`/visits?ra=${ra.toFixed(6)}&dec=${dec.toFixed(6)}`, { signal, timeoutMs: 60000 });
  if (!Array.isArray(j?.passes) || !Number.isFinite(j.n_visits)) throw new Error('bad response');
  return { count: j.n_visits, passes: j.passes };
}

/**
 * Candidates found by the offline survey (GET {API_BASE}/candidates), most interesting first:
 * { total, candidates: [{ id, ra, dec, name, classification, pm_total, position_angle_deg, score, found, ... }] }.
 * `classification` narrows the list to one class. Returns null when the list cannot be had: no
 * backend configured, or a backend that does not serve the route yet (404).
 */
export async function fetchCandidates({ limit = 100, offset = 0, classification, sort, signal } = {}) {
  if (!API_BASE) return null;
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (classification) params.set('classification', classification);
  if (sort && sort !== 'score') params.set('sort', sort);
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
 * Run the PlanetX motion engine on SPHEREx frames of a position (GET {API_BASE}/motion).
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
    if (job.status === 'failed') throw new Error(job.error || 'the run failed');
    if (job.status !== 'running') throw new Error('bad response');
    onProgress?.(job);
    await sleep(MOTION_POLL_MS, signal);
  }
}
