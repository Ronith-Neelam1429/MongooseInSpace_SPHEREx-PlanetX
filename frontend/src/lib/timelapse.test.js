import { describe, expect, it } from 'vitest';
import { apiUrl, framePerPass, marksFor, rowAt, shiftText, timelapsePatch } from './timelapse.js';
import { separation } from './coords.js';
import { targetRow } from './names.js';
import { plainType } from './plain.js';

// Barnard's Star as a known-object row: today's position (2026.87) and its Gaia motion in mas/yr.
const barnard = { name: "Barnard's star", ra: 269.4486, dec: 4.7576, pmra: -801.6, pmdec: 10362.4, pm: 10.39, pa: 356, epoch: 2026.87, also: [] };

describe('rowAt', () => {
  it('moves a star by its proper motion', () => {
    const p = rowAt(barnard, 2000.4);
    // 26.5 years at 10.39"/yr is ~275" (4.6'), almost due south going back in time.
    expect(separation(p.ra, p.dec, barnard.ra, barnard.dec) * 3600).toBeCloseTo(275, 0);
    expect(p.dec).toBeLessThan(barnard.dec);
  });

  it('keeps a star without a motion in place', () => {
    expect(rowAt({ ra: 10, dec: 20, pmra: null, pmdec: null, epoch: 2026 }, 1999)).toEqual({ ra: 10, dec: 20 });
  });
});

describe('timelapsePatch', () => {
  it('centres between the 1999 and today positions with room around both', () => {
    const p = timelapsePatch(barnard, barnard.epoch);
    const then = rowAt(barnard, 1999.5);
    const toThen = separation(p.ra, p.dec, then.ra, then.dec) * 3600;
    const toNow = separation(p.ra, p.dec, barnard.ra, barnard.dec) * 3600;
    expect(toThen).toBeCloseTo(toNow, 0);
    expect(p.side).toBeGreaterThan(p.span + 150);
    expect(p.side).toBeLessThanOrEqual(900);
  });

  it('uses the smallest patch for a star that barely moves', () => {
    const p = timelapsePatch({ ra: 100, dec: 10, pmra: 2, pmdec: 1, epoch: 2026 }, 2026);
    expect(p.side).toBe(180);
    expect(p.ra).toBeCloseTo(100, 4);
  });

  it('centres on today when the path is too long for one patch', () => {
    const fast = { ra: 50, dec: 0, pmra: 0, pmdec: 40000, epoch: 2026 };
    const p = timelapsePatch(fast, 2026);
    expect(p.side).toBe(900);
    expect(p.dec).toBeCloseTo(0, 6);
  });
});

describe('framePerPass', () => {
  const f = (mjd) => ({ mjd, image_url: `/x?${mjd}` });

  it('keeps the frame nearest the middle of each pass, oldest first', () => {
    const out = framePerPass([f(61200), f(60900), f(60905), f(60920), f(61210), f(61260)]);
    expect(out.map((x) => x.mjd)).toEqual([60905, 61200, 61260]);
    expect(out.map((x) => [x.pass, x.n_pass])).toEqual([[1, 3], [2, 2], [3, 1]]);
  });

  it('handles no frames', () => {
    expect(framePerPass([])).toEqual([]);
  });
});

describe('marksFor', () => {
  it('puts the star on the image at each date and traces its path', () => {
    const patch = timelapsePatch(barnard, barnard.epoch);
    const m = marksFor(barnard, patch, 2000.4, [2000.4, 2026.6]);
    expect(m.inside).toBe(true);
    expect(m.path).toHaveLength(2);
    // North is up: the old position is south of the new one by ~4.6'.
    expect(m.path[1].north - m.path[0].north).toBeCloseTo(274, -1);
    expect(m.at).toEqual(m.path[0]);
  });
});

describe('shiftText', () => {
  it('uses arcminutes from one arcminute up', () => {
    expect(shiftText(275)).toBe('4.6′');
    expect(shiftText(12.4)).toBe('12″');
    expect(shiftText(0.14)).toBe('0.1″');
  });
});

describe('apiUrl', () => {
  it('resolves an API path on the API origin', () => {
    expect(apiUrl('/api/cutout.png?a=1', 'http://127.0.0.1:8000/api')).toBe('http://127.0.0.1:8000/api/cutout.png?a=1');
  });
});

describe('targetRow', () => {
  const row = (name, ra, dec, also = []) => ({ name, ra, dec, also });

  it('finds the searched object by name, ignoring case and punctuation', () => {
    const rows = [row('TYC 425-223-1', 269.45, 4.76), row("Barnard's star", 269.4486, 4.7576)];
    expect(targetRow(rows, { label: "Barnard's Star", ra: 269.5, dec: 4.6 })).toBe(rows[1]);
  });

  it('matches a name listed at the same place', () => {
    const rows = [row('HD 1', 10, 10, [{ name: 'Other name' }])];
    expect(targetRow(rows, { label: 'other name', ra: 0, dec: 0 })).toBe(rows[0]);
  });

  it('falls back to the nearest row close to the centre, else nothing', () => {
    const rows = [row('A', 10, 10.004), row('B', 10, 10.001)];
    expect(targetRow(rows, { label: 'unknown', ra: 10, dec: 10 })).toBe(rows[1]);
    expect(targetRow(rows, { label: 'unknown', ra: 11, dec: 10 })).toBeNull();
  });
});

describe('plainType', () => {
  it('rewords jargon and keeps the rest', () => {
    expect(plainType('BY Dra Variable')).toBe('Spotted variable star');
    expect(plainType('Star')).toBe('Star');
    expect(plainType(null)).toBe('');
  });
});
