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

/** Try each mirror in turn; `build(base)` returns [url, init]; `parse(res)` validates + parses. */
async function fromMirrors(key, mirrors, build, parse, { signal, timeoutMs = 15000 } = {}) {
  const start = preferred.get(key) || 0;
  const order = mirrors.map((_, i) => (start + i) % mirrors.length);
  let lastErr;
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
  throw new Error(`${key} unavailable (${lastErr?.message || 'network error'})`);
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
  const q = `SELECT TOP ${limit * 3} b.main_id, b.otype, d.description, b.ra, b.dec, b.pmra, b.pmdec, f.V, f.G, f.K,
    DISTANCE(POINT('ICRS', b.ra, b.dec), POINT('ICRS', ${ra}, ${dec})) AS dist
    FROM basic AS b LEFT JOIN otypedef AS d ON b.otype = d.otype LEFT JOIN allfluxes AS f ON b.oid = f.oidref
    WHERE CONTAINS(POINT('ICRS', b.ra, b.dec), CIRCLE('ICRS', ${ra}, ${dec}, ${searchR})) = 1
    ORDER BY dist`;
  const rows = await fromMirrors('SIMBAD', SIMBAD_TAP, tap(q), tapJson, { signal });
  return rows.map((r) => ({
    name: String(r.main_id).replace(/^NAME /, ''),
    otype: r.otype,
    type: r.description || r.otype,
    ra: num(r.ra), dec: num(r.dec),
    pmra: num(r.pmra), pmdec: num(r.pmdec),
    mag: num(r.V) ?? num(r.G) ?? num(r.K),
    band: r.V != null ? 'V' : r.G != null ? 'G' : r.K != null ? 'K' : null,
  })).filter((r) => r.ra !== null && r.dec !== null).map((r) => {
    const p = r.pmra != null && r.pmdec != null ? propagate(r.ra, r.dec, r.pmra, r.pmdec, years) : r;
    return { ...r, ra: p.ra, dec: p.dec, dist: separation(ra, dec, p.ra, p.dec) };
  }).filter((r) => r.dist <= radiusDeg).sort((a, b) => a.dist - b.dist).slice(0, limit);
}

/** Parse VizieR asu-tsv output (header row, units row, dashes row, data). */
export function parseVizierTsv(text) {
  const lines = text.split(/\r?\n/).filter((l) => l && !l.startsWith('#'));
  const dash = lines.findIndex((l) => /^-+(\t-+)*\s*$/.test(l));
  if (dash < 1) return [];
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
    '-out': 'Source,RA_ICRS,DE_ICRS,pmRA,pmDE,Plx,Gmag',
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
    plx: num(r.Plx), gmag: num(r.Gmag),
  })).filter((r) => Number.isFinite(r.ra) && Number.isFinite(r.dec));
}

/** Resolve an object name to coordinates with CDS Sesame (SIMBAD, NED, VizieR). */
export async function resolveName(name, { signal } = {}) {
  const path = `/viz-bin/nph-sesame/-oxp/SNV?${encodeURIComponent(name.trim())}`;
  return fromMirrors('Sesame', VIZIER, (base) => [base + path, {}], async (res) => {
    const xml = await res.text();
    const ra = xml.match(/<jradeg>\s*([-\d.eE+]+)/);
    const dec = xml.match(/<jdedeg>\s*([-\d.eE+]+)/);
    if (!ra || !dec) return null;
    const oname = xml.match(/<oname>\s*([^<]+)/);
    return { ra: Number(ra[1]), dec: Number(dec[1]), name: oname ? oname[1].trim().replace(/^NAME /, '') : name };
  }, { signal, timeoutMs: 12000 });
}

/**
 * SPHEREx visits for a position. Backend contract (proposed):
 *   GET {API_BASE}/visits?ra=<deg>&dec=<deg>
 *   -> { visits: [{ date: ISO string, obs_id?: string, band?: string, detector?: number }] }
 * Returns null when no backend is configured.
 */
export async function fetchVisits(ra, dec, { signal } = {}) {
  if (!API_BASE) return null;
  const t = withTimeout(signal, 60000);
  try {
    const res = await fetch(`${API_BASE}/visits?ra=${ra.toFixed(6)}&dec=${dec.toFixed(6)}`, { signal: t.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const j = await res.json();
    const visits = Array.isArray(j) ? j : j.visits;
    if (!Array.isArray(visits)) throw new Error('bad response');
    return visits;
  } finally {
    t.done();
  }
}
