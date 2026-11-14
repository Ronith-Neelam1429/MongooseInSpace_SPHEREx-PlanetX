// Test targets from CLAUDE.md. Positions are SIMBAD J2000 (epoch 2000) with proper motions in mas/yr;
// the app moves high-proper-motion stars to the current epoch before centering.
export const TARGETS = [
  { id: 'barnard', label: "Barnard's Star", aliases: ['barnard', 'barnards star', 'gj 699'], ra: 269.4520769586187, dec: 4.693364966576667, pmra: -801.551, pmdec: 10362.394, tag: '10.4″/yr', note: 'Fastest-moving star in the sky (10.4″/yr).' },
  { id: 'epsindib', label: 'Epsilon Indi B', aliases: ['eps ind b', 'epsilon indi ba', 'epsilon indi bb', 'eps indi b'], ra: 331.04414658852124, dec: -56.782844195116105, pmra: 3981.977, pmdec: -2466.832, tag: 'brown dwarfs', note: 'Brown-dwarf binary Ba/Bb (<1″ apart, unresolved by SPHEREx).' },
  { id: 'wise0855', label: 'WISE 0855−0714', aliases: ['wise 0855', 'wise 0855-0714', 'wise j085510.83-071442.5', 'wise j0855-0714'], ra: 133.81890825, dec: -7.247048, pmra: -8123.7, pmdec: 673.2, tag: 'Y dwarf', note: 'Coldest known brown dwarf; very red, high proper motion.' },
  { id: 'rwaur', label: 'RW Aurigae', aliases: ['rw aur'], ra: 76.95652603458333, dec: 30.401438173888888, pmra: 7.23, pmdec: -20.33, tag: 'T Tauri', note: 'T Tauri star: a brightness variable, not a mover.' },
  { id: 'rdra', label: 'R Draconis', aliases: ['r dra'], ra: 248.16761083295, dec: 66.75497941722, pmra: -20.643, pmdec: 6.149, tag: 'Mira', note: 'Mira variable (~245 d period), near the North Ecliptic Pole.' },
];

export const TARGET_FOV = 0.25;

const norm = (s) => s.toLowerCase().replace(/[−–]/g, '-').replace(/[^a-z0-9+-]/g, '');

/** Match a search string against the built-in targets (label or alias). */
export function findTarget(text) {
  const q = norm(text);
  return TARGETS.find((t) => norm(t.label) === q || (t.aliases || []).some((a) => norm(a) === q)) || null;
}
