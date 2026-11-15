// When and in which colours SPHEREx imaged a patch, for the panel's coverage chart. Pure functions
// over the /visits answer ({ count, passes, visits }, see fetchVisits in services.js).

const DAY_MS = 86400000;
// SPHEREx began its survey in May 2025; the timeline never starts later than that.
export const SURVEY_START = '2025-05-01';
// The upper wavelength edge of each detector (µm), to place a visit whose detector is not given.
const DETECTOR_TOP_UM = [1.1, 1.63, 2.42, 3.83, 4.42, Infinity];

const ms = (date) => Date.parse(`${date}T00:00:00Z`);

/** The detector (1-6) a visit came from: its own field, else from the middle of its wavelengths. */
export function detectorOf(v) {
  if (Number.isInteger(v.detector) && v.detector >= 1 && v.detector <= 6) return v.detector;
  const lo = v.wavelength_min_um, hi = v.wavelength_max_um;
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
  const mid = (lo + hi) / 2;
  return DETECTOR_TOP_UM.findIndex((top) => mid < top) + 1;
}

/** Images per detector, as an array of six counts (detector 1 first). Unplaceable visits are left out. */
export function perDetector(visits = []) {
  const n = [0, 0, 0, 0, 0, 0];
  for (const v of visits) {
    const d = detectorOf(v);
    if (d) n[d - 1] += 1;
  }
  return n;
}

/**
 * The timeline: images per week from the survey's start (or the first visit, if earlier) to today
 * (or the last pass, if later). Returns { start, end, bins: [{ x0, x1, n }], max, passes, today }
 * with x as fractions 0-1 of the span; `passes` add x0/x1 to each pass. Bins come only from
 * individual visits; without them each pass is one bin holding its n_visits.
 */
export function timeline(passes = [], visits = [], now = new Date()) {
  const dates = [...visits.map((v) => v.date), ...passes.flatMap((p) => [p.start, p.end])].map(ms).filter(Number.isFinite);
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const start = Math.min(ms(SURVEY_START), ...dates);
  const end = Math.max(today, ...dates) + DAY_MS;
  const x = (t) => (t - start) / (end - start);
  let bins;
  if (visits.length) {
    const week = 7 * DAY_MS;
    const counts = new Map();
    for (const v of visits) {
      const t = ms(v.date);
      if (!Number.isFinite(t)) continue;
      const i = Math.floor((t - start) / week);
      counts.set(i, (counts.get(i) || 0) + 1);
    }
    bins = [...counts].sort((a, b) => a[0] - b[0])
      .map(([i, n]) => ({ x0: x(start + i * week), x1: x(Math.min(start + (i + 1) * week, end)), n }));
  } else {
    bins = passes.map((p) => ({ x0: x(ms(p.start)), x1: x(ms(p.end) + DAY_MS), n: p.n_visits }));
  }
  return {
    start: new Date(start), end: new Date(end), bins,
    max: Math.max(1, ...bins.map((b) => b.n)),
    passes: passes.map((p) => ({ ...p, x0: x(ms(p.start)), x1: x(ms(p.end) + DAY_MS) })),
    today: x(today),
  };
}

/** The first of January of every year inside the timeline, as { x, label }. */
export function yearTicks(t) {
  const out = [];
  const span = t.end - t.start;
  for (let y = t.start.getUTCFullYear() + 1; Date.UTC(y, 0, 1) < t.end; y += 1) {
    out.push({ x: (Date.UTC(y, 0, 1) - t.start) / span, label: String(y) });
  }
  return out;
}

/** How long the passes span, first image to last: "8 mo", "1.4 yr"; null with under two passes. */
export function baseline(passes = []) {
  if (passes.length < 2) return null;
  const days = (ms(passes[passes.length - 1].end) - ms(passes[0].start)) / DAY_MS;
  if (!Number.isFinite(days)) return null;
  const months = Math.round(days / 30.44);
  return months < 18 ? `${months} mo` : `${(days / 365.25).toFixed(1)} yr`;
}
