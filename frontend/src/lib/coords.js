// Coordinate helpers: parsing, formatting and frame conversions (all J2000 / ICRS, degrees).

const D2R = Math.PI / 180;
const R2D = 180 / Math.PI;

// ICRS -> Galactic rotation matrix (Hipparcos / IAU, J2000).
const GAL = [
  [-0.0548755604162154, -0.873437090234885, -0.4838350155487132],
  [0.4941094278755837, -0.44482962996001, 0.74698224449722],
  [-0.8676661490190047, -0.1980763734312015, 0.4559837761750669],
];
// Mean obliquity of the ecliptic at J2000 (IAU 2006).
const OBLIQUITY = 23.4392794444 * D2R;

const toVec = (lon, lat) => {
  const a = lon * D2R, b = lat * D2R;
  return [Math.cos(b) * Math.cos(a), Math.cos(b) * Math.sin(a), Math.sin(b)];
};
const fromVec = ([x, y, z]) => {
  let lon = Math.atan2(y, x) * R2D;
  if (lon < 0) lon += 360;
  const lat = Math.asin(Math.max(-1, Math.min(1, z))) * R2D;
  return { lon, lat };
};

export const normRa = (ra) => ((ra % 360) + 360) % 360;
export const clampDec = (dec) => Math.max(-90, Math.min(90, dec));

export function equatorialToGalactic(ra, dec) {
  const v = toVec(ra, dec);
  const r = GAL.map((row) => row[0] * v[0] + row[1] * v[1] + row[2] * v[2]);
  const { lon, lat } = fromVec(r);
  return { l: lon, b: lat };
}

export function equatorialToEcliptic(ra, dec) {
  const [x, y, z] = toVec(ra, dec);
  const c = Math.cos(OBLIQUITY), s = Math.sin(OBLIQUITY);
  const { lon, lat } = fromVec([x, y * c + z * s, -y * s + z * c]);
  return { lambda: lon, beta: lat };
}

/** Great-circle separation in degrees. */
export function separation(ra1, dec1, ra2, dec2) {
  const a = toVec(ra1, dec1), b = toVec(ra2, dec2);
  const dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const cross = Math.hypot(a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]);
  return Math.atan2(cross, dot) * R2D;
}

/** Move a position by a proper motion (mas/yr, pmra includes cos(dec)) over `years`. */
export function propagate(ra, dec, pmra, pmdec, years) {
  const dDec = (pmdec * years) / 3.6e6;
  const cosd = Math.cos(dec * D2R);
  const dRa = cosd > 1e-9 ? (pmra * years) / 3.6e6 / cosd : 0;
  return { ra: normRa(ra + dRa), dec: clampDec(dec + dDec) };
}

/** Decimal year for a Date (default: now). */
export function decimalYear(date = new Date()) {
  const y = date.getUTCFullYear();
  const start = Date.UTC(y, 0, 1), end = Date.UTC(y + 1, 0, 1);
  return y + (date.getTime() - start) / (end - start);
}

/**
 * "Now" for every position the site asks the API about: the decimal year at the start of the
 * current UTC month. The API keeps its answers per position (to 1e-6 deg), and a fast star at
 * the live time drifts past that in hours, so cached and preloaded pictures (tools/prewarm.py,
 * same rule) would stop matching. Barnard's Star moves 0.9" in a month, far under a 6.2" pixel.
 */
export function epochNow(date = new Date()) {
  return decimalYear(new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1)));
}

/**
 * A picture patch's centre rounded to 0.001 deg (3.6", about half a SPHEREx pixel). The same star
 * reaches the patch code from Gaia, SIMBAD or the target list with positions that differ in the
 * 5th-6th decimal; rounded, they ask the API for the same patch, so its cache (and what
 * tools/prewarm.py loaded, same rule) is hit.
 */
export function snapCentre({ ra, dec }) {
  return { ra: normRa(Math.round(ra * 1000) / 1000), dec: Math.round(dec * 1000) / 1000 };
}

function sexa(value, decimals) {
  const sign = value < 0 ? -1 : 1;
  let v = Math.abs(value);
  let a = Math.floor(v);
  let b = Math.floor((v - a) * 60);
  let c = ((v - a) * 60 - b) * 60;
  const f = 10 ** decimals;
  c = Math.round(c * f) / f;
  if (c >= 60) { c -= 60; b += 1; }
  if (b >= 60) { b -= 60; a += 1; }
  return { sign, a, b, c };
}
const pad = (n, w) => String(n).padStart(w, '0');

export function formatRa(ra) {
  let { a, b, c } = sexa(normRa(ra) / 15, 2);
  if (a >= 24) a -= 24;
  return `${pad(a, 2)}h ${pad(b, 2)}m ${c.toFixed(2).padStart(5, '0')}s`;
}

export function formatDec(dec) {
  const { sign, a, b, c } = sexa(dec, 1);
  return `${sign < 0 ? '−' : '+'}${pad(a, 2)}° ${pad(b, 2)}′ ${c.toFixed(1).padStart(4, '0')}″`;
}

export function formatFov(fovDeg) {
  if (fovDeg >= 1) return `${fovDeg.toFixed(fovDeg >= 10 ? 0 : 2)}°`;
  if (fovDeg * 60 >= 1) return `${(fovDeg * 60).toFixed(1)}′`;
  return `${(fovDeg * 3600).toFixed(0)}″`;
}

const NUM = /^[+-]?(\d+\.?\d*|\.\d+)$/;

/**
 * Parse a coordinate string. Accepts decimal degrees ("269.452 +4.693", "269.452, 4.693")
 * or sexagesimal ("17h57m48.5s +04d41m36s", "17:57:48.5 +04:41:36", "17 57 48.5 +04 41 36").
 * Returns { ra, dec } in degrees, or null if the text is not a coordinate.
 */
export function parseCoords(text) {
  if (!text) return null;
  const raw = text.trim().replace(/−/g, '-');
  const sexMarkers = /[hms:°'"′″d]/i.test(raw.replace(/^[^\d+-]+/, ''));
  const tokens = raw.replace(/[hms:°'"′″d,]/gi, ' ').trim().split(/\s+/).filter(Boolean);
  if (!tokens.length || !tokens.every((t) => NUM.test(t))) return null;

  let ra, dec;
  if (tokens.length === 2 && !sexMarkers) {
    ra = parseFloat(tokens[0]);
    dec = parseFloat(tokens[1]);
  } else if (tokens.length === 6 || tokens.length === 4) {
    const half = tokens.length / 2;
    const r = tokens.slice(0, half).map(parseFloat);
    const dTok = tokens.slice(half);
    const d = dTok.map((t) => Math.abs(parseFloat(t)));
    const neg = dTok[0].startsWith('-');
    ra = 15 * (r[0] + r[1] / 60 + (r[2] || 0) / 3600);
    dec = (neg ? -1 : 1) * (d[0] + d[1] / 60 + (d[2] || 0) / 3600);
    if (r[1] >= 60 || (r[2] || 0) >= 60 || d[1] >= 60 || (d[2] || 0) >= 60) return null;
  } else {
    return null;
  }
  if (!Number.isFinite(ra) || !Number.isFinite(dec) || ra < 0 || ra >= 360 || dec < -90 || dec > 90) return null;
  return { ra, dec };
}

/** Point at angular distance `distDeg` and position angle `paDeg` (east of north) from (ra, dec). */
export function offsetPoint(ra, dec, distDeg, paDeg) {
  const d = distDeg * D2R, pa = paDeg * D2R, d0 = dec * D2R;
  const sinDec = Math.sin(d0) * Math.cos(d) + Math.cos(d0) * Math.sin(d) * Math.cos(pa);
  const dec2 = Math.asin(Math.max(-1, Math.min(1, sinDec)));
  const dRa = Math.atan2(Math.sin(pa) * Math.sin(d) * Math.cos(d0), Math.cos(d) - Math.sin(d0) * sinDec);
  return { ra: normRa(ra + dRa * R2D), dec: dec2 * R2D };
}
