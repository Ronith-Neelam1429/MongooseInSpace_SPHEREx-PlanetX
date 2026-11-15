// Plain-language wording for what the engine and the catalogues report. The audience is the
// general public, so motions are said in words and the symbols stay behind "Details".

const COMPASS = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];
const MOON_ARCSEC = 1865; // mean width of the full Moon
const LY_PER_PARSEC = 3.2616;

/** Compass word for a direction of motion given as a position angle (degrees east of north). */
export function compass(paDeg) {
  return COMPASS[Math.round((((paDeg % 360) + 360) % 360) / 45) % 8];
}

/** Position angle (degrees east of north, 0-360) of a motion with east and north components. */
export function positionAngle(east, north) {
  return ((Math.atan2(east, north) * 180) / Math.PI + 360) % 360;
}

/** A motion in arcsec/yr with digits that suit its size: 10.39, 0.056, 0.0052. */
export function speed(pm) {
  if (pm == null || !Number.isFinite(pm)) return '–';
  return pm.toFixed(pm >= 1 ? 2 : pm >= 0.01 ? 3 : 4);
}

const rounded = (v) => Number(v.toPrecision(2)).toLocaleString('en-US');

/** How long a motion (arcsec/yr) takes to cross the width of the full Moon, e.g. "about 180 years". */
export function moonCrossing(pm) {
  if (!(pm > 0)) return null;
  const years = MOON_ARCSEC / pm;
  return years >= 1e6 ? `about ${rounded(years / 1e6)} million years` : `about ${rounded(years)} years`;
}

/** Distance in light-years from a parallax, only when the parallax is well measured (5 sigma). */
export function lightYears(plxMas, errMas) {
  if (!(plxMas > 0) || !(errMas > 0) || plxMas / errMas < 5) return null;
  return (1000 / plxMas) * LY_PER_PARSEC;
}

export function distanceText(ly) {
  return `about ${ly < 10 ? ly.toFixed(1) : rounded(ly)} light-years away`;
}

/**
 * The motion to quote for an engine object: the fit with archival positions when those confirm
 * the motion (decades of baseline), otherwise SPHEREx alone. Motions in arcsec/yr, `pa` in degrees
 * east of north, `sigma` = pm / err.
 */
export function measuredMotion(o) {
  const a = o.archive;
  if (a?.verdict === 'confirmed' && a.pm_ra_cosdec != null && a.pm_err > 0) {
    const pm = Math.hypot(a.pm_ra_cosdec, a.pm_dec);
    return {
      pm, sigma: pm / a.pm_err, err: a.pm_err, east: a.pm_ra_cosdec, north: a.pm_dec,
      pa: positionAngle(a.pm_ra_cosdec, a.pm_dec), fused: true,
    };
  }
  return {
    pm: o.pm_total, sigma: o.significance, err: o.pm_err, east: o.pm_ra_cosdec, north: o.pm_dec,
    pa: o.position_angle_deg, fused: false,
  };
}

const CLASS_LABEL = {
  known_star: 'Known star',
  known_asteroid: 'Known asteroid',
  variable_star: 'Variable star',
  artifact: 'Likely artifact',
  unknown_candidate: 'Unknown candidate',
  static: 'Not moving',
};

/** A catalogue classification from the API in words; "Candidate" when it has none. */
export function classLabel(classification) {
  return CLASS_LABEL[classification] || 'Candidate';
}

// The backend's motion verdict (MotionVerdict in the API: is the motion real?), in words.
const MOTION_LABEL = {
  moving: 'Moving',
  not_moving: 'Not moving',
  fake_motion: 'Fake motion',
  unsure: 'Unsure',
};
// What most likely made a fake mover (the API's MotionVerdict.cause), as a phrase that ends
// "Most likely: …".
const CAUSE_TEXT = {
  blend: 'two sources blended into one blur',
  saturated_halo: 'a star too bright for the detector, or its glare',
  edge: 'the edge of the images',
  flagged_pixels: 'pixels SPHEREx flags as stray light (ghosts, halos, crosstalk)',
  low_snr: 'too faint to measure well',
  wavelength_centroid: 'its colour shifting where it appears in each detector',
  parallax_misfit: 'a yearly wobble that so few passes cannot pin down',
  chance_link: 'different objects joined into one track',
  other: 'no single cause stands out',
};
const DECIDED_BY = {
  spherex: 'SPHEREx alone',
  archive: 'older sky surveys',
  catalogue: 'the Gaia and SIMBAD catalogues',
  evidence_checks: 'the image tests',
};

/**
 * The backend's verdict on whether an object's motion is real, ready to show:
 * { verdict, label, tone, certainty, cause, causeText, reason, decidedBy }, or null when the
 * backend sent none (older servers). `tone` is 'moving' | 'still' | 'fake' | 'unsure'.
 * Takes a MotionObject, a SurveyCandidate or a MotionEvidence (anything with `motion_verdict`).
 */
export function motionVerdict(o) {
  const v = o?.motion_verdict;
  if (!v || !MOTION_LABEL[v.verdict]) return null;
  const label = v.verdict === 'moving'
    ? (v.certainty === 'confirmed' ? 'Moving: confirmed' : 'Moving: candidate')
    : MOTION_LABEL[v.verdict];
  const tone = { moving: 'moving', not_moving: 'still', fake_motion: 'fake', unsure: 'unsure' }[v.verdict];
  return {
    verdict: v.verdict, label, tone, certainty: v.certainty ?? null, cause: v.cause ?? null,
    causeText: v.cause ? CAUSE_TEXT[v.cause] || v.cause : null,
    reason: v.reason || '', decidedBy: DECIDED_BY[v.decided_by] || v.decided_by || '',
  };
}

/**
 * A survey candidate's motion in one line once its verdict is in: a fake motion is not quoted
 * as a detection ("Apparent 4.03″ a year, not real"); otherwise null (use candidateLine).
 */
export function fakeMotionLine(c) {
  if (motionVerdict(c)?.verdict !== 'fake_motion' || !(c.pm_total > 0)) return null;
  return `Apparent ${speed(c.pm_total)}″ a year, not real motion${c.is_variable ? ' · changes brightness' : ''}`;
}

/** What a verdict means, in one sentence, for a tooltip or a legend. */
export const VERDICT_HINT = {
  moving: 'The motion is real: confirmed by a catalogue or older surveys, or a candidate SPHEREx alone measures clearly.',
  still: 'No motion beyond what measurement noise does.',
  fake: 'SPHEREx shows motion, but other evidence says it is not real.',
  unsure: 'Motion is claimed, but the evidence cannot settle it yet.',
};

/**
 * One- or two-word verdict for an engine object. Uses the backend's motion verdict when it sent
 * one (a catalogued star or asteroid keeps its name, a variable that stays put says so), else
 * the catalogue classification, else the engine's own motion class. Never "discovery": at best
 * "candidate".
 */
export function verdict(o) {
  const mv = motionVerdict(o);
  if (mv) {
    if (mv.verdict === 'moving' && o.classification === 'known_asteroid') return CLASS_LABEL.known_asteroid;
    if (mv.verdict === 'moving' && o.classification === 'known_star') return 'Known star, moving';
    if (mv.verdict === 'not_moving' && o.classification === 'variable_star') return CLASS_LABEL.variable_star;
    if (mv.verdict === 'not_moving' && o.is_variable) return 'Changes brightness';
    return mv.label;
  }
  if (o.classification && CLASS_LABEL[o.classification]) return CLASS_LABEL[o.classification];
  if (o.kind === 'mover') return 'Moving';
  if (o.kind === 'candidate') return 'Candidate';
  return o.is_variable ? 'Changes brightness' : 'Not moving';
}

/** How sure a measured motion is, from its size over its error. */
export function confidence(sigma) {
  if (sigma >= 5) return 'Clear detection';
  if (sigma >= 3) return 'Tentative';
  return 'Within the measurement error';
}

/** The headline for an engine object, e.g. "Moves 9.91″ per year, toward north". */
export function claim(o) {
  const parts = [];
  if (o.kind !== 'static') {
    const m = measuredMotion(o);
    parts.push(motionVerdict(o)?.verdict === 'fake_motion'
      ? `Seems to move ${speed(m.pm)}″ per year, but the motion is not real`
      : `Moves ${speed(m.pm)}″ per year, toward ${compass(m.pa)}`);
  }
  if (o.is_variable && o.variability) {
    parts.push(`${parts.length ? 'brightness' : 'Brightness'} changed ${o.variability.ratio.toFixed(2)}× between passes`);
  }
  return parts.join('; ') || 'No motion measured';
}

/** The same, shortened for a list row or a map label: "9.91″/yr north". */
export function shortClaim(o) {
  const parts = [];
  if (o.kind !== 'static') {
    const m = measuredMotion(o);
    parts.push(`${speed(m.pm)}″/yr ${compass(m.pa)}${motionVerdict(o)?.verdict === 'fake_motion' ? ' (fake)' : ''}`);
  }
  if (o.is_variable && o.variability) parts.push(`brightness ×${o.variability.ratio.toFixed(2)}`);
  return parts.join(', ');
}

const OBSERVATORY = { '2mass': '2MASS', allwise: 'AllWISE', catwise: 'CatWISE', neowise: 'NEOWISE' };

/** What the check against older sky surveys found, as a sentence. */
export function archiveSentence(a) {
  if (!a) return null;
  const others = a.observatories.filter((x) => x !== 'spherex').map((x) => OBSERVATORY[x] || x);
  const who = others.length ? others.join(', ') : 'older surveys';
  const span = a.baseline_yr > 1.5 ? ` over ${Math.round(a.baseline_yr)} years` : '';
  switch (a.verdict) {
    case 'confirmed': return `Confirmed: ${who} saw it somewhere else, so it has kept moving${span}.`;
    case 'stationary': return `Not confirmed: ${who} saw it in the same place${span}, so it is probably not moving.`;
    case 'refuted': return `Not confirmed: ${who} show a separate object at each position, so this is probably two objects, not one moving.`;
    case 'absent': return 'Not confirmed: CatWISE lists nothing moving like this where its track puts it in 2015.';
    case 'unconfirmed': return a.candidate
      ? `Candidate: ${who} positions alone show it moving${span}, but a second, independent survey is needed to confirm it.`
      : 'Older sky surveys have too little here to confirm or rule out the motion.';
    case 'spherex_only': return 'Not compared with older sky surveys.';
    default: return a.verdict;
  }
}

/**
 * Does our measured motion match a catalogued one (both { east, north } in arcsec/yr)? They agree
 * when the difference is within three times our error, with some slack for a catalogue that is far
 * more precise than SPHEREx (10% of the catalogued motion, at least 0.15″/yr).
 */
export function motionsAgree(ours, catalog) {
  const diff = Math.hypot(ours.east - catalog.east, ours.north - catalog.north);
  const pm = Math.hypot(catalog.east, catalog.north);
  return diff <= Math.max(3 * (ours.err || 0), 0.1 * pm, 0.15);
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "2025-08-15" -> "Aug 2025". */
export function monthYear(date) {
  const [y, m] = String(date).split('-');
  return MONTHS[+m - 1] ? `${MONTHS[+m - 1]} ${y}` : String(date);
}

/** A span of two YYYY-MM-DD dates: "Aug – Oct 2025", "Aug 2025 – Sep 2026" or "Aug 2025". */
export function dateSpan(start, end) {
  const a = monthYear(start), b = monthYear(end);
  if (a === b) return a;
  return a.slice(-4) === b.slice(-4) ? `${a.slice(0, 3)} – ${b}` : `${a} – ${b}`;
}

/** Shorten a 19-digit Gaia designation for lists and map labels: "Gaia DR3 …575872". */
export function shortName(name) {
  const m = /^Gaia DR3 (\d{12,})$/.exec(name || '');
  return m ? `Gaia DR3 …${m[1].slice(-6)}` : name;
}

// SIMBAD's type names that mean little to most people, in plainer words (others are kept as is).
const PLAIN_TYPES = {
  'by dra variable': 'Spotted variable star',
  'spectroscopic binary': 'Binary star',
  'eclipsing binary': 'Eclipsing binary star',
  'chemically peculiar star': 'Star (unusual chemistry)',
  'hot subdwarf': 'Hot subdwarf star',
  'high proper motion star': 'Fast-moving star',
  'long-period variable': 'Slowly pulsing star',
  'mira variable': 'Pulsing giant star (Mira)',
  'rs cvn variable': 'Spotted binary star',
  'infra-red source': 'Infrared source',
  'x-ray source': 'X-ray source',
  'red giant branch star': 'Red giant star',
  'asymptotic giant branch star': 'Red giant star',
  't tauri star': 'Young star (T Tauri)',
  'young stellar object': 'Young star',
  'low-mass star': 'Low-mass star',
  'brown dwarf': 'Brown dwarf',
};

/** A SIMBAD object type in plain words, e.g. "BY Dra Variable" -> "Spotted variable star". */
export function plainType(type) {
  if (!type) return '';
  return PLAIN_TYPES[type.trim().toLowerCase()] || type;
}
