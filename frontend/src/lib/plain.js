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

/**
 * One- or two-word verdict for an engine object. Uses the backend's catalogue classification when
 * that check ran; otherwise the engine's own motion class. Never "discovery": at best "candidate".
 */
export function verdict(o) {
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
    parts.push(`Moves ${speed(m.pm)}″ per year, toward ${compass(m.pa)}`);
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
    parts.push(`${speed(m.pm)}″/yr ${compass(m.pa)}`);
  }
  if (o.is_variable && o.variability) parts.push(`brightness ×${o.variability.ratio.toFixed(2)}`);
  return parts.join(', ');
}

const OBSERVATORY = { twomass: '2MASS', allwise: 'AllWISE', catwise: 'CatWISE', neowise: 'NEOWISE' };

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
    case 'unconfirmed': return 'Older sky surveys have too little here to confirm or rule out the motion.';
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
