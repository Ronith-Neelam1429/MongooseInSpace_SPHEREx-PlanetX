import { describe, expect, it } from 'vitest';
import {
  equatorialToEcliptic, equatorialToGalactic, formatDec, formatRa, parseCoords, propagate, separation,
} from './coords.js';
import { parseVizierTsv } from './services.js';
import { readViewFromUrl, viewToSearch } from './url.js';

describe('parseCoords', () => {
  it('parses decimal degrees', () => {
    expect(parseCoords('269.452 4.693')).toEqual({ ra: 269.452, dec: 4.693 });
    expect(parseCoords('269.452, -4.693')).toEqual({ ra: 269.452, dec: -4.693 });
  });
  it('parses sexagesimal forms', () => {
    for (const s of ['08h55m10.831s -07d14m42.53s', '08:55:10.831 -07:14:42.53', '08 55 10.831 -07 14 42.53']) {
      const c = parseCoords(s);
      expect(c.ra).toBeCloseTo(133.795129, 5);
      expect(c.dec).toBeCloseTo(-7.245147, 5);
    }
  });
  it('keeps the sign of -00 declinations', () => {
    expect(parseCoords('12 00 00 -00 30 00').dec).toBeCloseTo(-0.5, 9);
  });
  it('rejects names and out-of-range values', () => {
    expect(parseCoords("Barnard's Star")).toBeNull();
    expect(parseCoords('M31')).toBeNull();
    expect(parseCoords('400 10')).toBeNull();
    expect(parseCoords('10 95')).toBeNull();
    expect(parseCoords('')).toBeNull();
  });
});

describe('frames', () => {
  it('maps the galactic centre and north pole', () => {
    const gc = equatorialToGalactic(266.40499, -28.93617);
    expect(gc.l === 0 || gc.l > 359.99 || gc.l < 0.01).toBe(true);
    expect(gc.b).toBeCloseTo(0, 2);
    expect(equatorialToGalactic(192.85948, 27.12825).b).toBeCloseTo(90, 3);
  });
  it('puts the North Ecliptic Pole at beta = +90', () => {
    expect(equatorialToEcliptic(270, 66.560708).beta).toBeCloseTo(90, 3);
    expect(equatorialToEcliptic(0, 0).lambda).toBeCloseTo(0, 6);
  });
});

describe('formatting & motion', () => {
  it('formats sexagesimal', () => {
    expect(formatRa(133.795129)).toBe('08h 55m 10.83s');
    expect(formatDec(-7.245147)).toBe('−07° 14′ 42.5″');
    expect(formatRa(359.99999999)).toBe('00h 00m 00.00s');
  });
  it('propagates Barnard proper motion', () => {
    const p = propagate(269.4520769586187, 4.693364966576667, -801.551, 10362.394, 25);
    expect(separation(269.4520769586187, 4.693364966576667, p.ra, p.dec) * 3600).toBeCloseTo(260.3, 0);
  });
});

describe('vizier tsv + url', () => {
  it('parses asu-tsv', () => {
    const tsv = '#comment\n\nSource\tRA_ICRS\tpmRA\n \tdeg\tmas/yr\n------\t-------\t-----\n123\t269.45\t -4.5\n';
    expect(parseVizierTsv(tsv)).toEqual([{ Source: '123', RA_ICRS: '269.45', pmRA: '-4.5' }]);
    expect(parseVizierTsv('#RESOURCE=yCat_1355\n#Name: I/355\n')).toEqual([]);
    expect(() => parseVizierTsv('<!doctype html><html></html>')).toThrow();
  });
  it('round-trips the view through the query string', () => {
    const s = viewToSearch({ ra: 269.45208, dec: 4.69336, fov: 0.25 }, '');
    expect(readViewFromUrl(s)).toEqual({ ra: 269.45208, dec: 4.69336, fov: 0.25 });
    expect(readViewFromUrl('?ra=999&dec=0')).toEqual({});
    expect(readViewFromUrl('?target=R%20Dra').name).toBe('R Dra');
  });
});

describe('targets', async () => {
  const { findTarget } = await import('./targets.js');
  it('matches labels and aliases', () => {
    expect(findTarget('WISE 0855-0714').id).toBe('wise0855');
    expect(findTarget('r dra').id).toBe('rdra');
    expect(findTarget("Barnard's Star").id).toBe('barnard');
    expect(findTarget('M31')).toBeNull();
  });
});

describe('star field scaling', async () => {
  const { starStyle, arrowYears } = await import('./starfield.js');
  it('draws brighter stars larger and more opaque', () => {
    const bright = starStyle(8), faint = starStyle(19);
    expect(bright.r).toBeGreaterThan(faint.r);
    expect(bright.a).toBeGreaterThan(faint.a);
    expect(starStyle(null).r).toBeGreaterThan(0);
  });
  it('picks a nice motion span from the fastest star', () => {
    // Barnard's Star (10.4"/yr) in a 4.5' field: half the radius in a nice number of years.
    expect(arrowYears([{ pmra: -801.551, pmdec: 10362.394 }, { pmra: 1, pmdec: 1 }], 0.075)).toBe(10);
    expect(arrowYears([{ pmra: null, pmdec: null }], 0.1)).toBeNull();
  });
});

describe('surveys', async () => {
  const { SPHEREX_FRAMES, SPHEREX_WAVELENGTHS, DEFAULT_FRAME } = await import('./surveys.js');
  it('lists 102 SPHEREx frames minus the two blank dichroic ones', () => {
    expect(SPHEREX_WAVELENGTHS).toHaveLength(102);
    expect(SPHEREX_FRAMES).toHaveLength(100);
    expect(SPHEREX_FRAMES.some((f) => f.frame === 50 || f.frame === 51)).toBe(false);
    expect(SPHEREX_FRAMES.find((f) => f.frame === DEFAULT_FRAME).band).toBe(2);
  });
});

describe('camera flight path', async () => {
  const { flightPath } = await import('./camera.js');
  const { separation } = await import('./coords.js');
  it('starts and ends on the requested views', () => {
    const from = { ra: 350, dec: 10, fov: 180 }, to = { ra: 10, dec: -5, fov: 0.25 };
    const p = flightPath(from, to);
    const a = p.at(0), b = p.at(1);
    expect(separation(a.ra, a.dec, from.ra, from.dec)).toBeLessThan(1e-6);
    expect(separation(b.ra, b.dec, to.ra, to.dec)).toBeLessThan(1e-6);
    expect(b.fov).toBeCloseTo(0.25, 6);
    expect(p.duration).toBeGreaterThan(0.5);
    expect(p.duration).toBeLessThanOrEqual(2.6);
  });
  it('crosses RA 0 the short way and widens on long hops at high zoom', () => {
    const p = flightPath({ ra: 359, dec: 0, fov: 0.3 }, { ra: 1, dec: 0, fov: 0.3 });
    const mid = p.at(0.5);
    expect(mid.ra < 1 || mid.ra > 359).toBe(true);
    const far = flightPath({ ra: 0, dec: 0, fov: 0.3 }, { ra: 90, dec: 0, fov: 0.3 });
    expect(far.at(0.5).fov).toBeGreaterThan(10);
  });
});
