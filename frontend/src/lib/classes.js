// What kind of thing each marker on the map is, so the map can be filtered to just the kinds a
// visitor cares about (most often: whatever moves). Every object falls into exactly one class;
// the first that applies wins, so a fast-moving variable star counts as a fast star.
//
// SIMBAD types are read from their place in SIMBAD's own type hierarchy (otypedef.path, e.g.
// "* > Ev* > AB* > Mi*" for a Mira), so candidate types ("Mi?") and new subtypes land in the right
// class without a hand-kept list of every code.
import { FAST_ARCSEC_YR } from './names.js';

// Colours as rgb triples for the canvas; the CSS custom properties in index.css match.
export const SIGNAL = [255, 77, 46]; // catalogued fast motion (--signal)
export const MEASURED = [92, 225, 255]; // motion the engine measured in SPHEREx frames (--measured)
export const VARIABLE = [255, 196, 64]; // changes brightness (--variable)
export const GALAXY = [178, 148, 255]; // galaxies, nebulae, clusters (--galaxy)
export const PLAIN = [244, 244, 242]; // ordinary stars (--fg)
export const OTHER = [143, 143, 140]; // everything else (--dim)

/**
 * The classes, in the order the filter lists them: what moves first. `mark` names the glyph the
 * legend draws, matching the map: dot (Gaia star), ring (measured track), dashed ring
 * (candidate), diamond (variable), square (catalogue entry with no Gaia star).
 */
export const CLASSES = [
  { id: 'candidate', label: 'Candidates', rgb: MEASURED, mark: 'dashed', hint: 'Motion AstroSearchX measured that no catalogue explains yet. A candidate, not a discovery.' },
  { id: 'mover', label: 'Measured movers', rgb: MEASURED, mark: 'ring', hint: 'Objects AstroSearchX saw move between SPHEREx passes' },
  { id: 'fast', label: 'Fast stars', rgb: SIGNAL, mark: 'dot', hint: `Catalogued stars moving faster than ${FAST_ARCSEC_YR}″ per year: good first targets` },
  { id: 'variable', label: 'Variables', rgb: VARIABLE, mark: 'diamond', hint: 'Stars that change brightness (and engine results that did)' },
  { id: 'star', label: 'Stars', rgb: PLAIN, mark: 'dot', hint: 'Ordinary stars with little or no motion' },
  { id: 'galaxy', label: 'Galaxies & nebulae', rgb: GALAXY, mark: 'square', hint: 'Galaxies, quasars, clouds, nebulae and star clusters: far away, they never move' },
  { id: 'other', label: 'Other', rgb: OTHER, mark: 'square', hint: 'Radio, X-ray, infrared and unclassified sources, likely artifacts, and fake motion' },
];
export const CLASS = Object.fromEntries(CLASSES.map((c) => [c.id, c]));

/** Filters: which classes are shown ({ [id]: boolean }). */
export const SHOW_ALL = Object.fromEntries(CLASSES.map((c) => [c.id, true]));
/** Only what moves, or might: the usual place to start looking. */
export const SHOW_MOVING = Object.fromEntries(CLASSES.map((c) => [c.id, ['candidate', 'mover', 'fast'].includes(c.id)]));

export const isMovingOnly = (filter) => CLASSES.every((c) => filter[c.id] === SHOW_MOVING[c.id]);
export const isAll = (filter) => CLASSES.every((c) => filter[c.id]);

// Segments of a SIMBAD type path that mean the brightness varies: variable stars and their
// subtypes elsewhere in the tree (pulsating, eclipsing, eruptive, young stars, cataclysmic).
const VARIABLE_TYPES = new Set([
  'V*', 'Ce*', 'cC*', 'RR*', 'Mi*', 'LP*', 'RV*', 'WV*', 'dS*', 'SX*', 'gD*', 'bC*', 'EB*', 'El*',
  'BY*', 'RS*', 'CV*', 'No*', 'Or*', 'TT*', 'RC*', 'a2*', 'SN*',
]);
// Roots of the hierarchy (and two star-tree leaves) for things that are extended or far away.
const EXTENDED_ROOTS = new Set(['G', 'GrG', 'ClG', 'PCG', 'SCG', 'IG', 'PaG', 'PoG', 'ISM', 'Cl*', 'As*', 'PoC']);
const EXTENDED_TYPES = new Set(['PN', 'PN?', 'HH']);

const fastMas = (pmra, pmdec) => pmra != null && pmdec != null && Math.hypot(pmra, pmdec) / 1000 >= FAST_ARCSEC_YR;

/** Class of a SIMBAD entry ({ otype, path, pmra, pmdec } with motions in mas/yr). */
export function simbadClass(o) {
  if (fastMas(o.pmra, o.pmdec)) return 'fast';
  const otype = o.otype || '';
  const path = o.path ? o.path.split('>').map((s) => s.trim()).filter(Boolean) : [otype];
  if (otype === 'var' || path.some((s) => VARIABLE_TYPES.has(s))) return 'variable';
  if (EXTENDED_TYPES.has(otype) || EXTENDED_ROOTS.has(path[0])) return 'galaxy';
  if (path[0] === '*') return 'star';
  // No hierarchy to go on (an unknown code): star-like codes carry a '*'.
  if (!o.path && otype.includes('*')) return 'star';
  return 'other';
}

/** Class of a Gaia star on its own ({ pmra, pmdec } in mas/yr). */
export const gaiaClass = (s) => (fastMas(s.pmra, s.pmdec) ? 'fast' : 'star');

/**
 * Class of a known-object row (see knownObjects in names.js): fast when it moves fast (by SIMBAD's
 * motion or its Gaia star's), else by its SIMBAD type, else a plain star.
 */
export function knownClass(row) {
  if (row.pm != null && row.pm >= FAST_ARCSEC_YR) return 'fast';
  if (row.simbad) return simbadClass({ ...row.simbad, pmra: null, pmdec: null });
  return 'star';
}

/**
 * Class of an engine object (MotionObject in the API). The catalogue check's verdict wins when it
 * ran: an artifact is never a mover, an unexplained motion is a candidate whatever its sigma, a
 * candidate track the catalogues explain (a known asteroid) is a mover. An unexplained brightness
 * change with no motion is drawn as a variable, so it is classed as one.
 */
const BY_VERDICT = { known_star: 'mover', known_asteroid: 'mover', variable_star: 'variable', artifact: 'other', static: 'star' };
const varies = (o) => o.is_variable || o.classification === 'variable_star';
export function engineClass(o) {
  // The backend's motion verdict, when it sent one, settles what moves: a fake motion is never a
  // mover, and an unsure one is at most a candidate.
  switch (o.motion_verdict?.verdict) {
    case 'moving': return o.motion_verdict.certainty === 'confirmed' ? 'mover' : 'candidate';
    case 'unsure': return 'candidate';
    case 'fake_motion': return varies(o) ? 'variable' : 'other';
    case 'not_moving': return varies(o) ? 'variable' : 'star';
    default: break;
  }
  if (o.classification === 'unknown_candidate') return o.kind === 'static' ? 'variable' : 'candidate';
  if (BY_VERDICT[o.classification]) return BY_VERDICT[o.classification];
  if (o.kind === 'candidate') return 'candidate';
  if (o.kind === 'mover') return 'mover';
  if (o.is_variable || o.classification === 'variable_star') return 'variable';
  return 'star';
}

/**
 * How many markers of each class a field holds: each known row once, each Gaia star that no row
 * covers, and each engine result. An engine result on a known object (Barnard's Star as a
 * measured mover and a fast star) counts in both classes, as both markers are drawn.
 */
export function countClasses(rows, stars, engineObjects) {
  const n = Object.fromEntries(CLASSES.map((c) => [c.id, 0]));
  const covered = new Set();
  for (const row of rows) {
    n[knownClass(row)] += 1;
    if (row.gaia) covered.add(row.gaia);
  }
  for (const s of stars) if (!covered.has(s)) n[gaiaClass(s)] += 1;
  for (const o of engineObjects) n[engineClass(o)] += 1;
  return n;
}
