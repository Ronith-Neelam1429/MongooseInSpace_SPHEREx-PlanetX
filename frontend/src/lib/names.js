// Names for what is on the map. The SIMBAD and Gaia lists loaded for a field are merged into one
// list of known objects, and each engine detection is matched against them so it can be shown
// with a name. All positions are compared at the same date: Gaia DR3 is epoch 2016.0, SIMBAD
// entries arrive already moved to today (see querySimbad), engine tracks carry their own dates.
import { epochNow, propagate, separation } from './coords.js';
import { lightYears, positionAngle } from './plain.js';

/** Match radius: one SPHEREx pixel (6.2″) plus room for centroid error. */
export const MATCH_ARCSEC = 7;
/** A SIMBAD entry and a Gaia star this close together are the same object. */
const SAME_OBJECT_ARCSEC = 2.5;
/** SIMBAD entries this close together are listed as one place (a star and its planets). */
const SAME_PLACE_ARCSEC = 1.5;
/** Faster than this (arcsec/yr) a catalogued star counts as a fast mover. */
export const FAST_ARCSEC_YR = 0.1;
const GAIA_EPOCH = 2016.0;
const ARCSEC = 3600;
const NO_MAG = 18; // an entry without a magnitude ranks as faint

export const mjdToYear = (mjd) => 2000 + (mjd - 51544.5) / 365.25;

const hasPm = (e) => e.pmra != null && e.pmdec != null;
const gaiaAt = (s, year) => (hasPm(s) ? propagate(s.ra, s.dec, s.pmra, s.pmdec, year - GAIA_EPOCH) : s);
const simbadAt = (o, year, nowYear) => (hasPm(o) ? propagate(o.ra, o.dec, o.pmra, o.pmdec, year - nowYear) : o);

/**
 * The catalogue entry an engine track most plausibly is: within MATCH_ARCSEC of the track on
 * average (each track point compared with the entry moved to that point's date), preferring
 * bright and near (half a magnitude per arcsecond). Returns { entry, sep } or null.
 */
function bestMatch(track, entries, at, mag) {
  let best = null;
  for (const e of entries) {
    const first = at(e, track[0].year);
    if (Math.abs(first.dec - track[0].dec) * ARCSEC > 3 * MATCH_ARCSEC) continue;
    let sum = separation(track[0].ra, track[0].dec, first.ra, first.dec);
    for (let i = 1; i < track.length; i++) {
      const p = at(e, track[i].year);
      sum += separation(track[i].ra, track[i].dec, p.ra, p.dec);
    }
    const sep = (sum / track.length) * ARCSEC;
    if (sep > MATCH_ARCSEC) continue;
    const score = (mag(e) ?? NO_MAG) + 0.5 * sep;
    if (!best || score < best.score) best = { entry: e, sep, score };
  }
  return best;
}

const simbadMatch = ({ entry: o, sep }) => ({
  catalog: 'simbad', name: o.name, kind: o.type || null, sep_arcsec: sep,
  pm_ra_cosdec: hasPm(o) ? o.pmra / 1000 : null, pm_dec: hasPm(o) ? o.pmdec / 1000 : null, magnitude: o.mag ?? null,
});

const gaiaMatch = ({ entry: s, sep }) => ({
  catalog: 'gaia', name: `Gaia DR3 ${s.id}`, kind: 'Star', sep_arcsec: sep,
  pm_ra_cosdec: hasPm(s) ? s.pmra / 1000 : null, pm_dec: hasPm(s) ? s.pmdec / 1000 : null, magnitude: s.gmag ?? null,
  distance_ly: lightYears(s.plx, s.plxErr),
});

/**
 * Who an engine object is: { name, matches, checked }. `matches` has the shape of the API's
 * KnownMatch (best first; motions in arcsec/yr). When the backend ran its own catalogue check
 * (it sets `classification`), or sent a name, that is used as is. Otherwise the object is matched
 * here against the field's lists, given as { simbad, gaia } with null for a list that is not
 * loaded. A SIMBAD name is preferred; then "Gaia DR3 <id>"; a null name with `checked` true means
 * uncatalogued, with `checked` false that no list was there to look in.
 */
export function identify(o, lists, nowYear = epochNow()) {
  if (o.classification != null || o.name || o.matches?.length) {
    return { name: o.name || o.matches?.[0]?.name || null, matches: o.matches || [], checked: true };
  }
  const { simbad, gaia } = lists;
  if (!simbad && !gaia) return { name: null, matches: [], checked: false };
  const points = o.track?.length ? o.track : [{ ra: o.ra, dec: o.dec, mjd: o.ref_mjd }];
  const track = points.map((t) => ({ ra: t.ra, dec: t.dec, year: mjdToYear(t.mjd) }));
  const s = bestMatch(track, simbad || [], (e, year) => simbadAt(e, year, nowYear), (e) => e.mag);
  const g = bestMatch(track, gaia || [], gaiaAt, (e) => e.gmag);
  if (s && g) {
    const p = gaiaAt(g.entry, nowYear);
    const same = separation(s.entry.ra, s.entry.dec, p.ra, p.dec) * ARCSEC <= SAME_OBJECT_ARCSEC;
    if (same) return { name: s.entry.name, matches: [simbadMatch(s), gaiaMatch(g)], checked: true };
    // Two different objects inside the pixel: the nearer one is the detection.
    const near = s.sep <= g.sep ? simbadMatch(s) : gaiaMatch(g);
    return { name: near.name, matches: [near], checked: true };
  }
  const only = s ? simbadMatch(s) : g ? gaiaMatch(g) : null;
  return { name: only?.name ?? null, matches: only ? [only] : [], checked: true };
}

/**
 * Who an engine object is, wherever it was opened from: the identity its list row already carries
 * (`listed`, rows of { o, who }), else matched now against the field's lists (as identify). A
 * static object opened from a catalogued star's card is in no open list, but keeps its name.
 */
export function identityOf(o, listed, lists, nowYear = epochNow()) {
  return listed.find((r) => r.o === o)?.who || identify(o, lists, nowYear);
}

/** Name to show for an identified engine object. */
export function displayName(identity) {
  return identity.name || (identity.checked ? 'Uncatalogued object' : 'Unnamed object');
}

function knownRow(o, s, nowYear, also = []) {
  const src = s && hasPm(s) ? s : o && hasPm(o) ? o : null;
  const now = s ? gaiaAt(s, nowYear) : o;
  return {
    key: o ? `simbad:${o.name}` : `gaia:${s.id}`,
    name: o ? o.name : `Gaia DR3 ${s.id}`,
    type: o ? o.type : 'Star',
    simbad: o || null,
    gaia: s || null,
    also, // other SIMBAD entries at the same position
    ra: now.ra, dec: now.dec, // today
    pm: src ? Math.hypot(src.pmra, src.pmdec) / 1000 : null, // arcsec/yr
    // The motion itself (mas/yr, east and north) from `ra`, `dec` at `epoch`, for lib/timelapse.js.
    pmra: src ? src.pmra : null, pmdec: src ? src.pmdec : null, epoch: nowYear,
    pa: src ? positionAngle(src.pmra, src.pmdec) : null,
    mag: o?.mag ?? s?.gmag ?? null,
    band: o?.mag != null ? o.band : s?.gmag != null ? 'G' : null,
    distanceLy: s ? lightYears(s.plx, s.plxErr) : null,
  };
}

const isFast = (row) => row.pm != null && row.pm >= FAST_ARCSEC_YR;

/**
 * One list of the known objects in a field, one row per place on the sky: SIMBAD entries (those
 * at one position, such as a star and its planets, share a row led by the brightest), each joined
 * with its Gaia star when there is one (for the motion and distance), plus fast Gaia stars SIMBAD
 * does not list. Fast movers come first, then the brightest. Returns { rows, of(kind, obj) },
 * where `of` gives the row for a SIMBAD entry or a Gaia star picked on the map (an unlisted Gaia
 * star gets a row of its own).
 */
export function knownObjects(simbad, gaia, nowYear = epochNow()) {
  const close = (a, b, limit) => Math.abs(a.dec - b.dec) * ARCSEC <= limit && separation(a.ra, a.dec, b.ra, b.dec) * ARCSEC <= limit;
  const places = []; // { o, also }
  for (const o of [...simbad].sort((a, b) => (a.mag ?? 99) - (b.mag ?? 99))) {
    const host = places.find((p) => close(p.o, o, SAME_PLACE_ARCSEC));
    if (host) host.also.push(o);
    else places.push({ o, also: [] });
  }
  // A Gaia star joins one place only: the nearest.
  const stars = gaia.map((s) => ({ s, p: gaiaAt(s, nowYear) }));
  const owner = new Map();
  for (const place of places) {
    let near = null;
    for (const g of stars) {
      if (!close(place.o, g.p, SAME_OBJECT_ARCSEC)) continue;
      const sep = separation(place.o.ra, place.o.dec, g.p.ra, g.p.dec);
      if (!near || sep < near.sep) near = { s: g.s, sep };
    }
    if (near && !(owner.get(near.s)?.sep <= near.sep)) owner.set(near.s, { place, sep: near.sep });
  }
  const starOf = new Map([...owner].map(([s, { place }]) => [place, s]));
  const bySimbad = new Map();
  const byGaia = new Map();
  const rows = places.map((place) => {
    const row = knownRow(place.o, starOf.get(place), nowYear, place.also);
    for (const o of [place.o, ...place.also]) bySimbad.set(o, row);
    if (row.gaia) byGaia.set(row.gaia, row);
    return row;
  });
  for (const s of gaia) {
    if (byGaia.has(s) || !hasPm(s) || Math.hypot(s.pmra, s.pmdec) / 1000 < FAST_ARCSEC_YR) continue;
    const row = knownRow(null, s, nowYear);
    byGaia.set(s, row);
    rows.push(row);
  }
  rows.sort((a, b) => (isFast(b) - isFast(a)) || (isFast(a) ? b.pm - a.pm : 0) || ((a.mag ?? 99) - (b.mag ?? 99)));
  return {
    rows,
    of(kind, obj) {
      if (kind === 'simbad') return bySimbad.get(obj) || knownRow(obj, null, nowYear);
      return byGaia.get(obj) || knownRow(null, obj, nowYear);
    },
  };
}

const nameKey = (s) => s.toLowerCase().replace(/[−–]/g, '-').replace(/[^a-z0-9+-]/g, '');
/** A searched object this far from the patch centre (arcsec) is still taken to be the target. */
const TARGET_ARCSEC = 20;

/**
 * The row of the object a search named: the row whose name (or a name sharing its place) is the
 * patch's label, else the nearest row within TARGET_ARCSEC of the centre. Null when none fits.
 */
export function targetRow(rows, sel) {
  const want = nameKey(sel.label || '');
  if (want) {
    const named = rows.find((r) => nameKey(r.name) === want || r.also.some((o) => nameKey(o.name) === want));
    if (named) return named;
  }
  let best = null;
  for (const r of rows) {
    const sep = separation(sel.ra, sel.dec, r.ra, r.dec) * ARCSEC;
    if (sep <= TARGET_ARCSEC && (!best || sep < best.sep)) best = { r, sep };
  }
  return best?.r ?? null;
}

/** The few known objects worth an always-visible label: fast movers, then the brightest named ones. */
export function notableKnown(rows, max = 5) {
  const fast = rows.filter(isFast);
  const bright = rows.filter((r) => !isFast(r) && r.simbad && r.mag != null && r.mag <= 12);
  return [...fast, ...bright].slice(0, max);
}

// A selection is { kind: 'engine', obj: <MotionObject> } or { kind: 'known', obj: <known row> }.

/** Whether two selections are the same object. */
export function sameObject(a, b) {
  if (!a || !b || a.kind !== b.kind) return false;
  return a.kind === 'engine' ? a.obj === b.obj : a.obj.key === b.obj.key;
}

/**
 * What the map tracks for a selection: the object its marker was drawn from (a Gaia star follows
 * the played motion) and a position for when that marker is not on screen. An engine object sits
 * where its latest pass saw it.
 */
export function mapTarget(sel) {
  if (sel.kind === 'engine') {
    const last = sel.obj.track[sel.obj.track.length - 1] || sel.obj;
    return { kind: 'engine', obj: sel.obj, ra: last.ra, dec: last.dec };
  }
  const row = sel.obj;
  return { kind: row.gaia ? 'gaia' : 'simbad', obj: row.gaia || row.simbad, ra: row.ra, dec: row.dec };
}
