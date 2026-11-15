import { describe, expect, it } from 'vitest';
import { separation } from './coords.js';
import { toOffsets } from './evidence.js';
import {
  MAX_SIDE_ARCSEC, cutoutSideArcsec, cutoutSizeDeg, engineViews, frameImageUrl, groupPasses, knownMotion, knownViews,
  pickFrames, picturePatch, positionAt, spread,
} from './pictures.js';

const frame = (mjd, detector, date = '2025-08-15', obs = `o${mjd}`) => ({
  obs_id: obs, detector, mjd, date, image_url: `/api/cutout.png?url=x&obs=${obs}&d=${detector}`,
});

// Barnard's Star in Gaia DR3 (epoch 2016.0), mas/yr.
const BARNARD_GAIA = { id: '4472832130942575872', ra: 269.44850252543836, dec: 4.739420051112412, pmra: -801.551, pmdec: 10362.394 };
const barnardRow = { key: 'simbad:Barnard', name: "Barnard's Star", ra: 269.4473, dec: 4.7685, gaia: BARNARD_GAIA, simbad: { name: "Barnard's Star", ra: 269.4473, dec: 4.7685, pmra: -802.8, pmdec: 10362.5 } };

describe('choosing SPHEREx exposures', () => {
  it('groups exposures into passes at gaps, and cuts gapless coverage into windows', () => {
    const frames = [frame(60902, 1), frame(60950, 2), frame(60903, 3), frame(61100, 1), frame(61110, 3)];
    const passes = groupPasses(frames);
    expect(passes.map((g) => g.map((f) => f.mjd))).toEqual([[60902, 60903], [60950], [61100, 61110]]);
    // Near the ecliptic poles: one exposure every 10 days for a year is several windows of <= 90 days.
    const pole = Array.from({ length: 37 }, (_, i) => frame(60800 + i * 10, 3));
    const windows = groupPasses(pole);
    expect(windows.length).toBe(4);
    for (const w of windows) expect(w[w.length - 1].mjd - w[0].mjd).toBeLessThanOrEqual(90);
    expect(groupPasses([{ mjd: NaN }, frame(1, 1)]).length).toBe(1);
  });

  it('keeps the first and last of a long list, evenly spread', () => {
    expect(spread([1, 2, 3], 4)).toEqual([1, 2, 3]);
    expect(spread([0, 1, 2, 3, 4, 5, 6, 7, 8], 3)).toEqual([0, 4, 8]);
    expect(spread([0, 1, 2, 3, 4, 5, 6, 7], 4)).toEqual([0, 2, 5, 7]);
  });

  it('picks one exposure per pass on the detector nearest D3, near the middle of the pass', () => {
    const frames = [
      frame(60900, 1, '2025-08-13'), frame(60901, 3, '2025-08-14'), frame(60905, 3, '2025-08-18'), frame(60910, 3, '2025-08-23'),
      frame(61100, 2, '2026-03-01'), frame(61102, 5, '2026-03-03'), // no D3 in this pass: D2 is nearest
      frame(61290, 3, '2026-09-07'),
    ];
    const picked = pickFrames(frames);
    expect(picked.map((f) => [f.mjd, f.detector])).toEqual([[60905, 3], [61100, 2], [61290, 3]]);
    expect(picked[0].pass).toEqual({ start: '2025-08-13', end: '2025-08-23', n: 4 });
    // Never more than a few cutouts, however many passes there are.
    const many = Array.from({ length: 12 }, (_, i) => frame(60000 + i * 100, 3));
    const few = pickFrames(many);
    expect(few.length).toBe(4);
    expect(few[0].mjd).toBe(60000);
    expect(few[3].mjd).toBe(61100);
    expect(pickFrames([])).toEqual([]);
  });

  it('asks for cutouts the backend can make, and knows their size', () => {
    expect(cutoutSizeDeg(432)).toBe(0.12);
    expect(cutoutSizeDeg(10)).toBe(0.01); // backend minimum
    expect(cutoutSizeDeg(5000)).toBe(0.5);
    // 0.12 deg = 70.2 px -> 70 -> odd 71 px of 6.15″.
    expect(cutoutSideArcsec(0.12)).toBeCloseTo(71 * 6.15, 6);
    expect(cutoutSideArcsec(0.001)).toBeCloseTo(3 * 6.15, 6);
  });

  it('makes a frame address absolute on the API host', () => {
    expect(frameImageUrl(frame(1, 3), 'http://127.0.0.1:8011/api', 'http://localhost:5181/'))
      .toBe('http://127.0.0.1:8011/api/cutout.png?url=x&obs=o1&d=3');
    expect(frameImageUrl(frame(1, 3), '/api', 'https://example.org/app/')).toBe('https://example.org/api/cutout.png?url=x&obs=o1&d=3');
  });
});

describe('where a catalogued star was', () => {
  it('moves a Gaia star from 2016.0 with its proper motion', () => {
    const m = knownMotion(barnardRow, 2026.87);
    expect(m).toMatchObject({ year: 2016, source: 'Gaia DR3', pmdec: 10362.394 });
    const p2000 = positionAt(m, 2000);
    // 16 years at 10.36″/yr: 165.8″ further south.
    expect((p2000.dec - BARNARD_GAIA.dec) * 3600).toBeCloseTo(-10.362394 * 16, 3);
    expect(positionAt(m, 2016)).toEqual({ ra: BARNARD_GAIA.ra, dec: BARNARD_GAIA.dec });
  });

  it('falls back to SIMBAD (already at today), then to no motion', () => {
    const simbadOnly = { ra: 10, dec: 20, gaia: null, simbad: { ra: 10, dec: 20, pmra: 100, pmdec: 0 } };
    expect(knownMotion(simbadOnly, 2026.5)).toMatchObject({ year: 2026.5, source: 'SIMBAD', pmra: 100 });
    const still = { ra: 10, dec: 20, gaia: { ra: 10, dec: 20, pmra: null, pmdec: null }, simbad: null };
    expect(knownMotion(still, 2026.5)).toMatchObject({ pmra: 0, pmdec: 0, source: null });
  });

  it('centres the pictures between the 1999 and today positions', () => {
    const m = knownMotion(barnardRow, 2026.87);
    const patch = picturePatch(m, 2026.87);
    const then = positionAt(m, 1999), now = positionAt(m, 2026.87);
    const a = separation(patch.ra, patch.dec, then.ra, then.dec), b = separation(patch.ra, patch.dec, now.ra, now.dec);
    expect(a).toBeCloseTo(b, 6);
    // 27.9 years x 10.39″/yr = 290″ of track, plus 75″ either side.
    expect(patch.side).toBeGreaterThan(430);
    expect(patch.side).toBeLessThan(450);
    // A star that does not move gets the minimum; a very fast one the backend's maximum.
    expect(picturePatch({ ra: 1, dec: 1, year: 2016, pmra: 0, pmdec: 0 }, 2026).side).toBe(150);
    expect(picturePatch({ ra: 1, dec: 1, year: 2016, pmra: 0, pmdec: 60000 }, 2026).side).toBe(MAX_SIDE_ARCSEC);
  });
});

describe('the views of a catalogued star', () => {
  const now = 2026.87;
  const m = knownMotion(barnardRow, now);
  const patch = picturePatch(m, now);
  const surveys = {
    ra: patch.ra, dec: patch.dec, patch_arcsec: patch.side, unavailable: [],
    images: [
      { survey: 'wise', label: 'AllWISE W1', mjd: 55400, date: '2010-07-27', pixel_arcsec: 1.375, image_png_b64: 'B' },
      { survey: '2mass', label: '2MASS Ks', mjd: 51692.43, date: '2000-05-28', pixel_arcsec: 1, image_png_b64: 'A' },
    ],
  };
  const frames = [frame(61290, 3, '2026-09-07'), frame(60905, 3, '2025-08-18')];

  it('orders every picture by date and labels each with telescope and date', () => {
    const views = knownViews({ motion: m, patch, surveys, frames, cutoutUrl: (f) => `u:${f.obs_id}` });
    expect(views.map((v) => v.telescope)).toEqual(['2MASS', 'WISE', 'SPHEREx', 'SPHEREx']);
    expect(views.map((v) => v.button)).toEqual(['2MASS 2000', 'WISE 2010', 'SPHEREx Aug 2025', 'SPHEREx Sep 2026']);
    expect(views[0].src).toBe('data:image/png;base64,A');
    expect(views[2].src).toBe('u:o60905');
    expect(views[2].band).toBe('D3 · 1.6–2.4 µm');
  });

  it('rings the star where its motion puts it on each date, the newest position as reference', () => {
    const views = knownViews({ motion: m, patch, surveys, frames, cutoutUrl: () => '' });
    const p2000 = positionAt(m, 2000 + (51692.43 - 51544.5) / 365.25);
    const want = toOffsets(patch.ra, patch.dec, p2000.ra, p2000.dec);
    expect(views[0].marks.ring.north).toBeCloseTo(want.north, 6);
    expect(views[0].marks.ring.east).toBeCloseTo(want.east, 6);
    // Barnard's Star moves north: in 2000 it was ~270″ south of where SPHEREx sees it in 2026.
    expect(views[3].at.north - views[0].at.north).toBeGreaterThan(260);
    expect(views[0].marks.ref).toEqual(views[3].at);
    expect(views[3].marks.ref).toBeNull();
    expect(views.every((v) => !v.outside)).toBe(true);
  });

  it('shows archive images alone until SPHEREx arrives, and no reference for a still star', () => {
    expect(knownViews({ motion: m, patch, surveys, frames: null, cutoutUrl: () => '' }).length).toBe(2);
    const still = { ra: 10, dec: 20, year: 2016, pmra: 0, pmdec: 0 };
    const views = knownViews({ motion: still, patch: { ra: 10, dec: 20, side: 150 }, surveys: null, frames, cutoutUrl: () => '' });
    expect(views.map((v) => v.marks.ref)).toEqual([null, null]);
    expect(views[0].marks.ring.east).toBeCloseTo(0, 9);
  });
});

describe('the views of an engine object', () => {
  const evidence = {
    patch_ra: 269.4459, patch_dec: 4.7673, patch_arcsec: 180, patch_pixel_arcsec: 3.075,
    fit: { ref_mjd: 61102.1, east_arcsec: 0.9, north_arcsec: 5.2, pm_east: -0.8, pm_north: 10.4 },
    difference_passes: [0, 2], difference_png_b64: 'D',
    passes: [
      { index: 0, start: '2025-08-10', end: '2025-08-20', n_frames: 18, image_png_b64: 'P0', point: { east_arcsec: 0, north_arcsec: 0, mjd: 60905, date: '2025-08-18' } },
      { index: 1, start: '2026-03-01', end: '2026-03-20', n_frames: 18, image_png_b64: 'P1', point: null },
      { index: 2, start: '2026-09-01', end: '2026-09-10', n_frames: 20, image_png_b64: 'P2', point: { east_arcsec: 0.8, north_arcsec: 10.4, mjd: 61290, date: '2026-09-07' } },
    ],
  };
  const object = { kind: 'mover', pm_ra_cosdec: -0.8, pm_dec: 10.4, pm_err: 0.5, matches: [] };

  it('puts the passes in time order, an unmeasured pass at its dates, and the difference aside', () => {
    const { views, extras } = engineViews(object, evidence, null);
    expect(views.map((v) => v.key)).toEqual(['pass:0', 'pass:1', 'pass:2']);
    expect(views[1].missed).toBe(true);
    expect(views[1].marks.ring).toBeNull();
    expect(views[2].marks.ring).toEqual({ east: 0.8, north: 10.4 });
    expect(views[2].marks.ref).toEqual({ east: 0, north: 0 });
    expect(extras[0]).toMatchObject({ key: 'difference', marks: { was: { east: 0, north: 0 }, ring: { east: 0.8, north: 10.4 } } });
  });

  it('puts the older surveys first', () => {
    const surveys = {
      ra: evidence.patch_ra, dec: evidence.patch_dec, patch_arcsec: 900, unavailable: [],
      images: [{ survey: '2mass', label: '2MASS Ks', mjd: 51692, date: '2000-05-28', pixel_arcsec: 1, image_png_b64: 'A' }],
    };
    const { views } = engineViews(object, evidence, surveys);
    expect(views.map((v) => v.key)).toEqual(['survey:2mass', 'pass:0', 'pass:1', 'pass:2']);
    // ~26 years at 10.4″/yr north: the 2000 position is far south of the SPHEREx one.
    expect(views[0].marks.ring.north).toBeLessThan(-200);
    expect(views[0].marks.line).not.toBeNull();
  });
});
