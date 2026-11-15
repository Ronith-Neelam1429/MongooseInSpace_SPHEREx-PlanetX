// What the side panel says first about a patch of sky: a one-line verdict, and the few objects
// worth a look. Pure functions over the app state (engine results, known-object rows).
import { engineClass, knownClass } from './classes.js';

const count = (n, one, many = `${one}s`) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`;
// Known classes that deserve a row at the top of the panel; the rest wait behind "More".
const FLAGGED_KNOWN = new Set(['fast', 'variable']);

/** Whether a known-object row is the object behind an engine result ({ o, who }). */
function sameAsResult(row, result) {
  const names = new Set([row.name, row.gaia && `Gaia DR3 ${row.gaia.id}`].filter(Boolean));
  return result.who.matches.some((m) => names.has(m.name));
}

/**
 * The verdict for a patch, one line: { text, tone }. After an engine run it counts what the
 * engine flagged ("1 candidate · 1 known mover · 1 variable", or "Nothing moved here"); before,
 * what the catalogues list ("2 catalogued fast movers · not measured yet"). `tone` is 'measured'
 * after a run with results, 'signal' for catalogued movers, otherwise 'plain'. `results` are the
 * engine's notable objects as { o, who } (who from identify in names.js); `rows` the known
 * objects (knownObjects in names.js); `loaded` whether the catalogues have answered.
 */
export function patchVerdict({ ran, results, rows, loaded }) {
  if (ran) {
    if (!results.length) return { text: 'Nothing moved here', tone: 'plain' };
    const n = { candidate: 0, known: 0, mover: 0, variable: 0, other: 0 };
    for (const r of results) {
      const cls = engineClass(r.o);
      if (cls === 'mover') n[r.who.matches.length ? 'known' : 'mover'] += 1;
      else if (cls in n) n[cls] += 1;
      else n.other += 1;
    }
    const parts = [
      n.candidate && count(n.candidate, 'candidate'),
      n.known && count(n.known, 'known mover'),
      n.mover && count(n.mover, 'mover'),
      n.variable && count(n.variable, 'variable'),
      n.other && count(n.other, 'other result'),
    ].filter(Boolean);
    return { text: parts.join(' · '), tone: 'measured' };
  }
  if (!loaded) return { text: 'Looking up what is known here', tone: 'plain' };
  const fast = rows.filter((r) => knownClass(r) === 'fast').length;
  const variable = rows.filter((r) => knownClass(r) === 'variable').length;
  const parts = [
    fast ? count(fast, 'catalogued fast mover') : 'No catalogued fast movers',
    variable && count(variable, 'variable star'),
    'not measured yet',
  ].filter(Boolean);
  return { text: parts.join(' · '), tone: fast ? 'signal' : 'plain' };
}

/**
 * The known objects to list at the top: fast movers and variables, minus those an engine result
 * already stands for (Barnard's Star, measured, is listed once, as the measurement). In the
 * known list's order (fastest first).
 */
export function flaggedKnown(rows, results) {
  return rows.filter((row) => FLAGGED_KNOWN.has(knownClass(row)) && !results.some((r) => sameAsResult(row, r)));
}

/** The known objects behind "More": everything not flagged. */
export function otherKnown(rows) {
  return rows.filter((row) => !FLAGGED_KNOWN.has(knownClass(row)));
}
