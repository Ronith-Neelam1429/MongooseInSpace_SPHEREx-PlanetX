// The offline survey in words and on the map: its progress line, the coverage layers, the
// candidates list's wording, and finding a candidate again among an engine run's objects.
import { separation } from './coords.js';
import { compass, confidence, speed } from './plain.js';

const n = (x) => x.toLocaleString('en-US');

/**
 * The survey's patches by status, in drawing order (finished and failed over the queued outlines).
 * `style` is Aladin Lite 3.8.2's MOCFromJSON options; `swatch` is the legend's CSS class, whose
 * colour must match (CandidatesPanel.css). The map is night-dark in both themes, so are these.
 */
export const COVERAGE_LAYERS = [
  { key: 'queued', label: 'next to search', swatch: 'sw-queued', style: { name: 'Survey: next', color: '#f4f4f2', opacity: 0.35, edge: true, lineWidth: 1 } },
  { key: 'done', label: 'searched', swatch: 'sw-done', style: { name: 'Survey: searched', color: '#5ce1ff', opacity: 0.3, fill: true, edge: true, lineWidth: 1 } },
  { key: 'failed', label: 'failed', swatch: 'sw-failed', style: { name: 'Survey: failed', color: '#ff4d2e', opacity: 0.45, fill: true, edge: true, lineWidth: 1 } },
];

/** "312 of 49,152 patches searched", plus failures and where the crawl is, from a SurveyStatus. */
export function surveyProgress(s) {
  if (!s?.total) return 'Our automatic survey has not started yet.';
  const parts = [`${n(s.counts.done)} of ${n(s.total)} patches searched`];
  if (s.counts.failed) parts.push(`${n(s.counts.failed)} failed`);
  if (s.current_radius_deg != null) parts.push(`now ${s.current_radius_deg.toFixed(1)}° from the north ecliptic pole`);
  else if (s.counts.done + s.counts.failed === s.total) parts.push('finished');
  return parts.join(' · ');
}

/**
 * How a candidate moves, in words: "8.90″ a year toward the west · tentative (3.2σ)". Null when its
 * motion is within the error (the survey also stores brightness variables, whose fit is only noise).
 */
export function candidateMotion(c) {
  if (!(c.pm_total > 0) || !(c.significance >= 3)) return null;
  return `${speed(c.pm_total)}″ a year toward the ${compass(c.position_angle_deg)} · ${confidence(c.significance).toLowerCase()} (${c.significance.toFixed(1)}σ)`;
}

/** The candidate's "found" date (YYYY-MM-DD) as "14 Nov 2026"; the input unchanged if it is not a date. */
export function foundDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
  if (!m) return iso || '';
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}

/** The field the survey measured a candidate in (older servers: the candidate itself). */
export function candidateField(c) {
  return Number.isFinite(c.field_ra) && Number.isFinite(c.field_dec) ? { ra: c.field_ra, dec: c.field_dec } : { ra: c.ra, dec: c.dec };
}

/** Within this distance an engine object is the survey candidate (one SPHEREx pixel). */
export const MATCH_ARCSEC = 6.2;

/**
 * The object of an engine run that is survey candidate `c`: the nearest within MATCH_ARCSEC of its
 * position (both are positions at the same reference date from the same pipeline), else null.
 */
export function matchCandidate(objects, c) {
  let best = null, bestSep = MATCH_ARCSEC / 3600;
  for (const o of objects || []) {
    const sep = separation(c.ra, c.dec, o.ra, o.dec);
    if (sep <= bestSep) { best = o; bestSep = sep; }
  }
  return best;
}

/**
 * At most `limit` tasks at once, first come first served. `run(release)` starts a task, which calls
 * `release()` once it is over; the returned function cancels a task that has not started yet.
 */
export function slots(limit) {
  let busy = 0;
  const waiting = [];
  const next = () => {
    while (busy < limit && waiting.length) {
      const run = waiting.shift();
      busy++;
      let done = false;
      run(() => { if (!done) { done = true; busy--; next(); } });
    }
  };
  return (run) => {
    waiting.push(run);
    next();
    return () => { const i = waiting.indexOf(run); if (i >= 0) waiting.splice(i, 1); };
  };
}
