// "About this object": the answer of GET /api/object/profile (`ObjectProfile` in
// backend/planetx/api/schemas.py) turned into short display text and chart models. Every field
// of the profile may be null; each helper returns null when it has nothing to say.
import { measuredMotion, motionsAgree, motionVerdict } from './plain.js';

const SUN_RADIUS_KM = 695700;
// Reference bodies by radius in Sun radii (diameter ratios equal radius ratios).
export const REFERENCES = [
  { thing: 'Earth', rsun: 6371 / SUN_RADIUS_KM },
  { thing: 'Jupiter', rsun: 69911 / SUN_RADIUS_KM },
  { thing: 'Sun', rsun: 1 },
];
const SUN_TEFF_K = 5772;
const EARTH_ORBIT_KM_S = 29.8;
const MOON_KM = 384400;
const LY_PER_PC = 3.26156;
const MAX_NAME = 100;
// The backend's limits (backend/planetx/api/routes/profile.py): a value past them fails the request.
const MAX_PM_MAS = 20000;
const MAG_RANGE = [-30, 40];

const ok = (v) => typeof v === 'number' && Number.isFinite(v);
const group = (v) => Math.round(v).toLocaleString('en-US');
/** `v` to `n` significant figures, with thousands separators. */
export const sig = (v, n = 2) => v.toLocaleString('en-US', { maximumSignificantDigits: n });
/** A raw value as the backend gave it (researchers' view), up to 6 decimals. */
const raw = (v) => v.toLocaleString('en-US', { maximumFractionDigits: 6 });
const withErr = (v, e, unit = '') => (ok(v) ? `${raw(v)}${ok(e) ? ` ± ${raw(e)}` : ''}${unit}` : null);
const signedRaw = (v) => `${v >= 0 ? '+' : '−'}${raw(Math.abs(v))}`;

/** A distance in light-years (or parsecs) for people: 6.0, 42, 330, 25,000. */
export function lyText(ly) {
  if (!ok(ly)) return null;
  if (ly < 10) return ly.toFixed(1);
  if (ly < 100) return group(ly);
  return sig(ly, 2);
}

/** A calendar year: 2020, 11,800, 8,000 BCE. */
export function yearText(y) {
  if (!ok(y)) return null;
  const r = Math.round(y);
  if (r < 0) return `${group(-r)} BCE`;
  return r >= 10000 ? group(r) : String(r);
}

/** A multiple in words: 2, 7, 1.5, 12, 1,200. */
function times(v) {
  if (v < 10) return String(Number(v.toFixed(1)));
  return sig(v, 2);
}

/** How long something at `kms` takes to cross the Earth-Moon distance. */
export function moonTripText(kms) {
  if (!ok(kms) || kms <= 0) return null;
  const s = MOON_KM / kms;
  const unit = (n, one) => `${n} ${one}${n === 1 ? '' : 's'}`;
  if (s < 90) return unit(Math.round(s), 'second');
  if (s < 90 * 60) return unit(Math.round(s / 60), 'minute');
  if (s < 36 * 3600) return unit(Math.round(s / 3600), 'hour');
  return unit(Math.round(s / 86400), 'day');
}

const MOTION_SOURCE = { gaia: 'Gaia DR3', simbad: 'SIMBAD', measured: 'SPHEREx' };
const SOURCE = {
  gaia_parallax: 'Gaia parallax',
  simbad_parallax: 'SIMBAD parallax',
  photometric_estimate: 'estimate from its brightness',
};

/**
 * Distance tile and lines: { value, unit, sub, phrase, light, note } or null. A measured distance
 * says when the light we see left; an object with motion but no distance gets the range its
 * motion suggests (`motion.estimated_distance_pc`), clearly marked as an estimate.
 */
export function distanceView(p) {
  const d = p?.distance;
  if (d && ok(d.ly)) {
    const estimate = d.source === 'photometric_estimate';
    return {
      value: lyText(d.ly),
      unit: 'ly',
      sub: estimate ? 'estimate' : ok(d.pc) ? `${lyText(d.pc)} pc` : 'light-years',
      phrase: `${estimate ? 'Roughly' : 'About'} ${lyText(d.ly)} light-years away${estimate ? ' (an estimate from its brightness)' : ''}.`,
      light: lightText(d),
      note: d.note || null,
    };
  }
  const range = p?.motion?.estimated_distance_pc;
  if (Array.isArray(range) && ok(range[0]) && ok(range[1])) {
    const lo = lyText(range[0] * LY_PER_PC), hi = lyText(range[1] * LY_PER_PC);
    return {
      value: `${lo}–${hi}`,
      unit: 'ly',
      sub: 'estimate',
      phrase: `Perhaps ${lo} to ${hi} light-years away, if it moves at an ordinary star's speed (20–100 km/s). An estimate, not a measurement.`,
      light: null,
      note: null,
    };
  }
  return null;
}

function lightText(d) {
  if (d.ly > 5000) return `The light you see left it about ${sig(d.ly, 2)} years ago.`;
  const y = d.light_left_year;
  if (!ok(y)) return null;
  // Year 0 is 1 BCE; at these distances "around" covers the rounding.
  if (y <= 0) return `The light you see left it around ${yearText(Math.min(y, -1))}.`;
  return `The light you see left it in ${yearText(y)}.`;
}

/** The object's diameter over each reference body's: { Sun, Jupiter, Earth } (null when unknown). */
function ratios(phys) {
  const listed = Object.fromEntries((phys?.size_comparison || []).filter((c) => ok(c?.ratio)).map((c) => [c.thing, c.ratio]));
  const r = ok(phys?.radius_rsun) ? phys.radius_rsun : listed.Sun;
  if (!ok(r) || r <= 0) return null;
  const out = {};
  for (const ref of REFERENCES) out[ref.thing] = ok(listed[ref.thing]) ? listed[ref.thing] : r / ref.rsun;
  out.rsun = r;
  return out;
}

/** "About 1/5 the Sun's width — 2 Jupiters across" from diameter ratios. */
export function sizePhrase(q) {
  const sun = q.Sun;
  let first;
  if (sun < 0.5) first = `About 1/${1 / sun < 10 ? Math.round(1 / sun) : sig(1 / sun, 2)} the Sun's width`;
  else if (sun < 0.95) first = `About ${Math.round(sun * 10) * 10}% of the Sun's width`;
  else if (sun <= 1.05) first = "About the Sun's width";
  else first = `About ${times(sun)} times the Sun's width`;
  if (sun >= 0.95) return first;
  const j = q.Jupiter, e = q.Earth;
  let second;
  if (j >= 1.5) second = `${Math.round(j)} Jupiters across`;
  else if (j >= 0.75) second = "about Jupiter's size";
  else if (e >= 1.5) second = `${Math.round(e)} Earths across`;
  else if (e >= 0.75) second = "about Earth's size";
  else second = 'smaller than Earth';
  return `${first} — ${second}`;
}

/** Size tile and phrase: { value, sub, phrase, estimate, discs } or null. */
export function sizeView(p) {
  const phys = p?.physical;
  const q = ratios(phys);
  if (!q) return null;
  return {
    value: `×${q.Sun < 10 ? Number(q.Sun.toPrecision(2)) : sig(q.Sun, 2)}`,
    sub: "the Sun's width",
    phrase: sizePhrase(q),
    estimate: typeof phys.radius_source === 'string' && phys.radius_source.startsWith('estimate'),
    discs: discModel(q.rsun),
  };
}

/**
 * Discs for the size drawing: the object next to the reference bodies within a factor of 12 of
 * its size (at least the nearest one). Radii in pixels; when the drawn sizes would span more than
 * 15×, they go on a log scale (`log` true) so the smallest disc still shows.
 */
export function discModel(rsun, { maxPx = 26, minPx = 2.5 } = {}) {
  if (!ok(rsun) || rsun <= 0) return null;
  let refs = REFERENCES.filter((r) => Math.max(r.rsun / rsun, rsun / r.rsun) <= 12);
  if (!refs.length) {
    const near = REFERENCES.reduce((a, b) => (Math.abs(Math.log(b.rsun / rsun)) < Math.abs(Math.log(a.rsun / rsun)) ? b : a));
    refs = [near];
  }
  const items = [...refs.map((r) => ({ label: r.thing, rsun: r.rsun, self: false })), { label: 'This object', rsun, self: true }]
    .sort((a, b) => a.rsun - b.rsun);
  const lo = items[0].rsun, hi = items[items.length - 1].rsun;
  const log = hi / lo > 15;
  const px = (r) => {
    if (hi === lo) return maxPx;
    if (log) return minPx + ((Math.log(r) - Math.log(lo)) / (Math.log(hi) - Math.log(lo))) * (maxPx - minPx);
    return Math.max(minPx * 0.6, (r / hi) * maxPx);
  };
  return { log, discs: items.map((it) => ({ ...it, px: px(it.rsun) })) };
}

/** A temperature's colour in words, as the eye would see the star. */
export function colourWord(k) {
  if (k < 2500) return 'deep red';
  if (k < 3700) return 'orange-red';
  if (k < 5200) return 'orange';
  if (k < 6000) return 'yellow-white';
  if (k < 7500) return 'white';
  if (k < 10000) return 'blue-white';
  return 'blue';
}

/** Temperature tile and phrase: { value, unit, sub, color, phrase } or null. */
export function temperatureView(p) {
  const phys = p?.physical;
  const t = phys?.teff_k;
  if (!ok(t) || t <= 0) return null;
  const word = colourWord(t);
  const r = t / SUN_TEFF_K;
  const sun = `the Sun's surface (${group(SUN_TEFF_K)} K)`;
  const compare = r < 0.9 ? `Cooler than ${sun}` : r <= 1.1 ? `About as hot as ${sun}` : `About ${times(r)} times as hot as ${sun}`;
  const glow = t < 2500 ? `it glows a dim ${word}; most of its light is infrared` : `it glows ${word}`;
  return {
    value: group(t),
    unit: 'K',
    sub: word,
    color: typeof phys.color_hex === 'string' && /^#[0-9a-f]{6}$/i.test(phys.color_hex) ? phys.color_hex : null,
    phrase: `${compare}, so ${glow}.`,
  };
}

/**
 * Speed tile and phrase: { value, unit, sub, phrase } or null. Prefers the full space velocity,
 * then the sideways (tangential) part; without either, the motion across the sky in ″/yr.
 */
export function speedView(p) {
  const m = p?.motion;
  if (!m) return null;
  const space = ok(m.space_km_s) ? m.space_km_s : null;
  const kms = space ?? (ok(m.tangential_km_s) ? m.tangential_km_s : null);
  if (kms == null) {
    if (!ok(m.pm)) return null;
    const arcsec = m.pm / 1000;
    return { value: arcsec < 1 ? arcsec.toFixed(2) : times(arcsec), unit: '″/yr', sub: 'across the sky', phrase: null };
  }
  const n = kms / EARTH_ORBIT_KM_S;
  const earth = n >= 1.5 ? `about ${times(n)} times Earth's speed around the Sun`
    : n >= 0.7 ? "about Earth's speed around the Sun" : null;
  const trip = moonTripText(kms);
  const lead = space != null
    ? `It moves ${group(kms)} km/s relative to the Sun`
    : `It moves at least ${group(kms)} km/s relative to the Sun (only its sideways speed is known)`;
  const parts = [earth, trip && `fast enough to cross the distance to the Moon in ${trip}`].filter(Boolean);
  return {
    value: group(kms),
    unit: 'km/s',
    sub: space != null ? 'vs the Sun' : 'sideways, at least',
    phrase: `${lead}${parts.length ? `: ${parts.join(', ')}` : ''}.`,
  };
}

/** The "why it moves" sentences, in order. */
export function whyList(p) {
  return (Array.isArray(p?.why_moving) ? p.why_moving : []).filter((w) => w && typeof w.text === 'string' && w.text)
    .map((w) => ({ key: `${w.factor}:${w.text}`, factor: w.factor, text: w.text }));
}

const RAD = Math.PI / 180;

/**
 * Azimuthal equidistant offsets (degrees; x toward east, y toward north) of (ra, dec) from the
 * centre: true angular distance from the centre at any separation, so long tracks stay honest.
 */
export function skyOffset(ra0, dec0, ra, dec) {
  const d0 = dec0 * RAD, d = dec * RAD, da = (ra - ra0) * RAD;
  const xe = Math.cos(d) * Math.sin(da);
  const yn = Math.cos(d0) * Math.sin(d) - Math.sin(d0) * Math.cos(d) * Math.cos(da);
  const cosc = Math.sin(d0) * Math.sin(d) + Math.cos(d0) * Math.cos(d) * Math.cos(da);
  const sinc = Math.hypot(xe, yn);
  if (sinc < 1e-15) return { x: 0, y: 0 };
  const k = Math.atan2(sinc, cosc) / sinc;
  return { x: (k * xe) / RAD, y: (k * yn) / RAD };
}

/** A round length for a scale bar about a quarter of `extent`, with its label. */
export function scaleBar(extentDeg) {
  if (!ok(extentDeg) || extentDeg <= 0) return null;
  const target = extentDeg / 4;
  const [unit, per] = target >= 1 ? ['°', 1] : target >= 1 / 60 ? ['′', 1 / 60] : ['″', 1 / 3600];
  const t = target / per;
  const pow = 10 ** Math.floor(Math.log10(t));
  const step = [1, 2, 5, 10].map((m) => m * pow).filter((v) => v <= t).pop() ?? pow;
  return { deg: step * per, label: `${sig(step, 3)}${unit}` };
}

/**
 * The sky track for drawing: { points: [{ year, x, y }], now: { x, y, year }, start, end, extent,
 * scale, span, note }, offsets in degrees from the object's position today (x east, y north).
 * `now` is the track at `nowYear` (interpolated), or the profile position when outside the span.
 */
export function pathModel(p, nowYear) {
  const path = p?.path;
  const pts = (Array.isArray(path?.points) ? path.points : [])
    .filter((q) => ok(q?.year) && ok(q.ra) && ok(q.dec)).sort((a, b) => a.year - b.year);
  if (pts.length < 2 || !ok(p.ra) || !ok(p.dec)) return null;
  const points = pts.map((q) => ({ year: q.year, ...skyOffset(p.ra, p.dec, q.ra, q.dec) }));
  let now = { x: 0, y: 0, year: nowYear };
  const i = points.findIndex((q) => q.year >= nowYear);
  if (i > 0) {
    const a = points[i - 1], b = points[i], f = (nowYear - a.year) / (b.year - a.year);
    now = { x: a.x + f * (b.x - a.x), y: a.y + f * (b.y - a.y), year: nowYear };
  } else if (i === 0 && points[0].year === nowYear) now = { ...points[0] };
  const xs = points.map((q) => q.x), ys = points.map((q) => q.y);
  const extent = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
  return {
    points,
    now,
    start: points[0],
    end: points[points.length - 1],
    extent,
    scale: scaleBar(extent),
    span: ok(path.span_years) ? path.span_years : points[points.length - 1].year - points[0].year,
    note: path.note || null,
  };
}

/**
 * Distance from the Sun against year, when the track carries distances and a closest approach:
 * { inSpan, points: [{ year, ly }], closest: { year, ly }, now: { year, ly } | null, phrase } or
 * null. When the closest point lies outside the track's years (`path.closest_in_span` false, or
 * computed from the points when the backend leaves it out) there is no curve to draw: `inSpan` is
 * false, `points` is null and the phrase says how rough so long an extrapolation is.
 */
export function approachModel(p, nowYear) {
  const c = p?.path?.closest_approach;
  if (!c || !ok(c.year) || !ok(c.dist_ly)) return null;
  const all = (Array.isArray(p.path.points) ? p.path.points : []).filter((q) => ok(q?.year));
  const years = all.map((q) => q.year);
  const inSpan = typeof p.path.closest_in_span === 'boolean' ? p.path.closest_in_span
    : years.length > 0 && c.year >= Math.min(...years) && c.year <= Math.max(...years);
  const closest = { year: c.year, ly: c.dist_ly };
  if (!inSpan) {
    return {
      inSpan: false,
      points: null,
      closest,
      now: null,
      phrase: `Straight-line motion would bring it closest to the Sun around ${yearText(c.year)}, at about ${lyText(c.dist_ly)} light-years, `
        + 'far outside this view; such long extrapolations are rough.',
    };
  }
  const points = all.filter((q) => ok(q.dist_ly)).map((q) => ({ year: q.year, ly: q.dist_ly })).sort((a, b) => a.year - b.year);
  if (points.length < 2) return null;
  const nowLy = p.distance?.ly;
  const when = c.year > nowYear ? 'will pass closest' : 'passed closest';
  return {
    inSpan: true,
    points,
    closest,
    now: ok(nowLy) ? { year: nowYear, ly: nowLy } : null,
    phrase: `It ${when} to the Sun around ${yearText(c.year)}, at about ${lyText(c.dist_ly)} light-years.`,
  };
}

/**
 * Whether the backend's gravity summary already says what a sentence of ours would: it covers the
 * RUWE / unseen-companion hint and the non-single-star flag itself (planetx/profile/service.py).
 */
const SAYS_WOBBLE = /\bRUWE\b|wobble|unseen companion|companion we cannot see/i;
const SAYS_NON_SINGLE = /non-single/i;

/**
 * What pulls on it: { summary, companions: [{ key, name, text }], wobble } or null. `wobble` holds
 * only what the summary does not already say (no RUWE hint or non-single-star flag twice).
 */
export function gravityView(p) {
  const g = p?.gravity;
  if (!g) return null;
  const companions = (Array.isArray(g.companions) ? g.companions : []).map((c, i) => {
    const bits = [
      ok(c.sep_au) && `${sig(c.sep_au, 2)} AU apart${ok(c.sep_arcsec) ? ` (${raw(c.sep_arcsec)}″)` : ''}`,
      ok(c.period_yr) && `one orbit takes roughly ${sig(c.period_yr, 2)} years`,
      c.bound === 'likely' ? 'likely bound' : c.bound === 'possible' ? 'possibly bound' : null,
    ].filter(Boolean);
    return {
      key: c.gaia_id || `companion-${i}`,
      name: c.name || (c.gaia_id ? `Gaia DR3 ${c.gaia_id}` : 'Unnamed companion'),
      text: bits.join(' · '),
    };
  });
  const summary = typeof g.summary === 'string' && g.summary ? g.summary : null;
  const wobble = [
    g.wobble_hint && !SAYS_WOBBLE.test(summary || '') && `Gaia's fit to its motion is poor${ok(g.ruwe) ? ` (RUWE ${g.ruwe.toFixed(2)})` : ''}, which suggests an unseen companion tugging it.`,
    ok(g.non_single_star) && g.non_single_star > 0 && !SAYS_NON_SINGLE.test(summary || '') && 'Gaia lists it as a non-single star.',
  ].filter(Boolean).join(' ') || null;
  if (!summary && !companions.length && !wobble) return null;
  return { summary, companions, wobble };
}

/**
 * Every raw value with its error and source, for researchers: [{ title, rows: [[label, value]] }],
 * leaving out what is unknown (and groups left empty).
 */
export function researcherGroups(p) {
  if (!p) return [];
  const id = p.identity || {}, d = p.distance || {}, ph = p.physical || {}, m = p.motion || {}, g = p.gravity || {};
  const from = (v, src) => (v == null ? null : src ? `${v} (${src})` : v);
  const groups = [
    {
      title: 'Identity',
      rows: [
        ['Name', id.name],
        ['SIMBAD type', id.simbad_type],
        ['Spectral type', id.spectral_type],
        ['Gaia DR3', id.gaia_id],
        ['Position', ok(p.ra) && ok(p.dec) ? `RA ${raw(p.ra)}° · Dec ${signedRaw(p.dec)}°` : null],
        ['Galactic', ok(id.galactic_l) && ok(id.galactic_b) ? `l ${raw(id.galactic_l)}° · b ${signedRaw(id.galactic_b)}°` : null],
        ['Ecliptic lat.', ok(id.ecliptic_lat) ? `${signedRaw(id.ecliptic_lat)}°` : null],
        ['Constellation', id.constellation],
      ],
    },
    {
      title: 'Distance and motion',
      rows: [
        ['Distance', from(withErr(d.pc, d.pc_err, ' pc'), SOURCE[d.source] || d.source)],
        ['Distance note', d.note],
        ['Parallax', ok(m.parallax_wobble_arcsec) ? `${raw(m.parallax_wobble_arcsec * 1000)} mas` : null],
        ['Est. distance', Array.isArray(m.estimated_distance_pc) && ok(m.estimated_distance_pc[0]) && ok(m.estimated_distance_pc[1])
          ? `${raw(m.estimated_distance_pc[0])}–${raw(m.estimated_distance_pc[1])} pc (assumes 20–100 km/s)` : null],
        ['µα*, µδ', ok(m.pmra) && ok(m.pmdec) ? `${signedRaw(m.pmra)}, ${signedRaw(m.pmdec)} mas/yr` : null],
        ['Total motion', from(withErr(m.pm, m.pm_err, ' mas/yr'), MOTION_SOURCE[m.source] || m.source)],
        ['Position angle', ok(m.pa_deg) ? `${raw(m.pa_deg)}° east of north` : null],
        ['Tangential', withErr(m.tangential_km_s, m.tangential_err, ' km/s')],
        ['Radial', withErr(m.radial_km_s, m.radial_err, ' km/s')],
        ['Space velocity', ok(m.space_km_s) ? `${raw(m.space_km_s)} km/s` : null],
        ['U, V, W', Array.isArray(m.uvw_km_s) && m.uvw_km_s.length === 3 && m.uvw_km_s.every(ok)
          ? `${m.uvw_km_s.map(signedRaw).join(', ')} km/s (heliocentric, U to the Galactic centre)` : null],
        ['Population', from(m.population, m.population_basis)],
      ],
    },
    {
      title: 'Physical',
      rows: [
        ['Class', from(ph.class, ph.class_basis)],
        ['Teff', from(ok(ph.teff_k) ? `${group(ph.teff_k)} K` : null, ph.teff_source)],
        ['Radius', from(withErr(ph.radius_rsun, ph.radius_rsun_err, ' R☉'), ph.radius_source)],
        ['Radius (km)', ok(ph.radius_km) ? `${group(ph.radius_km)} km` : null],
        ['Mass', from(ok(ph.mass_msun) ? `${raw(ph.mass_msun)} M☉` : null, ph.mass_source)],
        ['Luminosity', from(ok(ph.luminosity_lsun) ? `${raw(ph.luminosity_lsun)} L☉` : null, ph.luminosity_source)],
        ['Age', ok(ph.age_gyr) ? `${raw(ph.age_gyr)} Gyr` : null],
        ['M_G', ok(ph.abs_mag_g) ? raw(ph.abs_mag_g) : null],
      ],
    },
    {
      title: 'Gravity',
      rows: [
        ['RUWE', ok(g.ruwe) ? raw(g.ruwe) : null],
        ['non_single_star', ok(g.non_single_star) ? String(g.non_single_star) : null],
        ...(Array.isArray(g.companions) ? g.companions : []).map((c, i) => [
          `Companion ${i + 1}`,
          [c.gaia_id && `Gaia DR3 ${c.gaia_id}`, c.name, ok(c.sep_arcsec) && `${raw(c.sep_arcsec)}″`, ok(c.sep_au) && `${raw(c.sep_au)} AU`,
            ok(c.g_mag) && `G ${raw(c.g_mag)}`, ok(c.period_yr) && `P ≈ ${raw(c.period_yr)} yr`, c.bound].filter(Boolean).join(' · ') || null,
        ]),
      ],
    },
    {
      title: 'Path',
      rows: [
        ['Span', ok(p.path?.span_years) ? `${group(p.path.span_years)} yr, ${p.path.points?.length ?? 0} points` : null],
        ['Closest approach', ok(p.path?.closest_approach?.year) && ok(p.path.closest_approach.dist_ly)
          ? `${raw(p.path.closest_approach.dist_ly)} ly in ${yearText(p.path.closest_approach.year)}` : null],
      ],
    },
  ];
  const ss = p.solar_system;
  if (ss && typeof ss === 'object') {
    groups.push({
      title: 'Solar System body',
      rows: [
        ['Name', ss.name], ['Class', ss.class],
        ['a', ok(ss.a_au) ? `${raw(ss.a_au)} AU` : null], ['e', ok(ss.e) ? raw(ss.e) : null],
        ['i', ok(ss.i_deg) ? `${raw(ss.i_deg)}°` : null], ['Period', ok(ss.period_yr) ? `${raw(ss.period_yr)} yr` : null],
        ['Diameter', ok(ss.diameter_km) ? `${raw(ss.diameter_km)} km` : null], ['Albedo', ok(ss.albedo) ? raw(ss.albedo) : null],
      ],
    });
  }
  return groups
    .map((grp) => ({ ...grp, rows: grp.rows.filter(([, v]) => v != null && v !== '') }))
    .filter((grp) => grp.rows.length);
}

/** Links that are safe to open: http(s) only. */
export function safeLinks(p) {
  return (Array.isArray(p?.links) ? p.links : []).filter((l) => {
    if (!l || typeof l.label !== 'string' || typeof l.url !== 'string') return false;
    try { return ['http:', 'https:'].includes(new URL(l.url).protocol); } catch { return false; }
  });
}

/** The one-line headline: "Red dwarf star · M4V · in Ophiuchus". */
export function identityLine(p) {
  const id = p?.identity;
  if (!id) return null;
  return [id.kind, id.spectral_type, id.constellation && `in ${id.constellation}`].filter(Boolean).join(' · ') || null;
}

// ---------- what to ask for ----------

const finite = (v) => (ok(v) ? v : null);

/** The profile request for a catalogued object (a known-object row from lib/names.js). */
export function knownProfileParams(row) {
  return {
    ra: row.ra, dec: row.dec,
    gaia_id: row.gaia?.id ?? null,
    name: row.simbad?.name ?? null,
    pmra: finite(row.pmra), pmdec: finite(row.pmdec),
    mag: finite(row.mag),
  };
}

/** A catalogue entry's motion as { east, north } in arcsec/yr, or null when it lists none. */
const listedMotion = (x) => (ok(x?.pm_ra_cosdec) && ok(x?.pm_dec) ? { east: x.pm_ra_cosdec, north: x.pm_dec } : null);

/**
 * The catalogue entries an engine object really is (`who` from lib/names.js identityOf), lead
 * first: none unless its name is the lead match's own (not "near Gaia DR3 …", and not a false
 * track that joins different objects), and only entries whose catalogued motion agrees with the
 * measured one. A Gaia entry right after a SIMBAD lead is that star's Gaia counterpart (both
 * lib/names.js and the backend list them so), and must also agree with the lead's motion. A mover
 * needs a listed motion to agree with; for a static object an entry without one is fine.
 */
function sameObjectMatches(o, who, m, moving) {
  const matches = who?.matches || [];
  const lead = matches[0];
  if (!lead || !who.name || who.name !== lead.name || /^near /i.test(who.name)) return [];
  const measured = ok(m.east) && ok(m.north) ? m : null;
  const agrees = (x) => {
    const theirs = listedMotion(x);
    if (!theirs) return !moving;
    return !measured || motionsAgree(measured, theirs);
  };
  if (!agrees(lead)) return [];
  const out = [lead];
  const next = matches[1];
  if (lead.catalog === 'simbad' && next?.catalog === 'gaia' && agrees(next)) {
    const a = listedMotion(lead), b = listedMotion(next);
    if (!a || !b || motionsAgree({ ...b, err: 0 }, a)) out.push(next);
  }
  return out;
}

/**
 * The profile request for an engine object: its measured position; its motion in mas/yr (the
 * archive refit when that confirmed it), only when it is accepted as moving and not a fake motion
 * (a static object's slope is noise); and the Gaia id and SIMBAD name of the catalogue object it
 * really is (sameObjectMatches), never a neighbour's.
 */
export function engineProfileParams(o, who) {
  const m = measuredMotion(o);
  const moving = o.kind !== 'static' && motionVerdict(o)?.verdict !== 'fake_motion' && ok(m.east) && ok(m.north);
  const same = sameObjectMatches(o, who, m, moving);
  const gaia = same.find((x) => x.catalog === 'gaia' && /^Gaia DR3 \d+$/.test(x.name || ''));
  const simbad = same.find((x) => x.catalog === 'simbad' && x.name);
  return {
    ra: o.ra, dec: o.dec,
    gaia_id: gaia ? gaia.name.slice('Gaia DR3 '.length) : null,
    name: simbad ? simbad.name : null,
    pmra: moving ? m.east * 1000 : null,
    pmdec: moving ? m.north * 1000 : null,
    pm_err: moving && ok(m.err) ? m.err * 1000 : null,
    mag: finite((simbad || gaia)?.magnitude),
  };
}

/** The query string for GET /object/profile, or null without a usable position. */
export function profileQuery(params) {
  const { ra, dec, gaia_id: gaiaId, name, pmra, pmdec, pm_err: pmErr, mag } = params || {};
  if (!ok(ra) || !ok(dec) || dec < -90 || dec > 90) return null;
  const q = new URLSearchParams({ ra: (((ra % 360) + 360) % 360).toFixed(6), dec: dec.toFixed(6) });
  if (gaiaId != null && /^\d{1,19}$/.test(String(gaiaId))) q.set('gaia_id', String(gaiaId));
  const n = typeof name === 'string' ? name.trim().slice(0, MAX_NAME) : '';
  if (n) q.set('name', n);
  // The motion goes as a whole or not at all: a part out of range (or missing) drops all three.
  const inRange = (v) => ok(v) && Math.abs(v) <= MAX_PM_MAS;
  if (inRange(pmra) && inRange(pmdec) && (pmErr == null || (inRange(pmErr) && pmErr >= 0))) {
    q.set('pmra', pmra.toFixed(3));
    q.set('pmdec', pmdec.toFixed(3));
    if (pmErr != null) q.set('pm_err', pmErr.toFixed(3));
  }
  if (ok(mag) && mag >= MAG_RANGE[0] && mag <= MAG_RANGE[1]) q.set('mag', mag.toFixed(2));
  return q.toString();
}
