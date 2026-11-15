import { describe, expect, it, vi } from 'vitest';
import { separation } from './coords.js';
import { toOffsets } from './evidence.js';
import {
  MAX_SIDE_ARCSEC, cutoutSideArcsec, cutoutSizeDeg, engineViews, frameImageUrl, groupPasses, imageSrc, isRemote, knownMotion,
  knownViews, pickFrames, picturePatch, positionAt, spread, stepKey, wantedKeys, defaultFrame, offFrame, angleText,
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

  it('moves east in right ascension by the motion over cos(dec)', () => {
    const m = { ra: 100, dec: 60, year: 2016, pmra: 3600, pmdec: 0 }; // 3.6″/yr east
    const p = positionAt(m, 2026);
    expect((p.ra - 100) * 3600 * Math.cos((60 * Math.PI) / 180)).toBeCloseTo(36, 6);
    expect(p.dec).toBe(60);
    expect(toOffsets(100, 60, p.ra, p.dec).east).toBeCloseTo(36, 2);
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
    // Centres are snapped to 0.001 deg (snapCentre): up to 2.6" off the midpoint, so |a - b| <= 5.2".
    expect(Math.abs(a - b) * 3600).toBeLessThan(5.2);
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
    const { views } = knownViews({ motion: m, patch, surveys, frames, cutoutUrl: (f) => `u:${f.obs_id}` });
    expect(views.map((v) => v.telescope)).toEqual(['2MASS', 'WISE', 'SPHEREx', 'SPHEREx']);
    expect(views.map((v) => v.button)).toEqual(['2MASS 2000', 'WISE 2010', 'SPHEREx Aug 2025', 'SPHEREx Sep 2026']);
    expect(views[0].src).toBe('data:image/png;base64,A');
    expect(views[2].src).toBe('u:o60905');
    expect(views[2].band).toBe('D3 · 1.6–2.4 µm');
  });

  it('rings the star where its motion puts it on each date, the newest position as reference', () => {
    const { views } = knownViews({ motion: m, patch, surveys, frames, cutoutUrl: () => '' });
    const p2000 = positionAt(m, 2000 + (51692.43 - 51544.5) / 365.25);
    const want = toOffsets(patch.ra, patch.dec, p2000.ra, p2000.dec);
    expect(views[0].marks.ring.north).toBeCloseTo(want.north, 6);
    expect(views[0].marks.ring.east).toBeCloseTo(want.east, 6);
    // Barnard's Star moves north: in 2000 it was ~270″ south of where SPHEREx sees it in 2026.
    expect(views[3].at.north - views[0].at.north).toBeGreaterThan(260);
    expect(views[0].marks.ref).toEqual(views[3].at);
    expect(views[1].note).toBe('mean date of its exposures');
    expect(views[3].marks.ref).toBeNull();
    expect(views.every((v) => !v.outside)).toBe(true);
  });

  it('puts every picture on one frame, so they blink in register', () => {
    const { frames: zooms, views } = knownViews({ motion: m, patch, surveys, frames, cutoutUrl: () => '' });
    const { side } = zooms.find((z) => z.key === 'wide');
    for (const v of views) expect([v.box.east, v.box.north]).toEqual([0, 0]);
    // The SPHEREx cutout is a whole number of pixels: a little wider than the archive images,
    // and the frame holds the widest.
    const spherexSide = views[2].box.side, archiveSide = views[0].box.side;
    expect(spherexSide).toBeCloseTo(73 * 6.15, 6);
    expect(archiveSide).toBe(patch.side);
    expect(side).toBeCloseTo(Math.max(spherexSide, archiveSide), 6);
  });

  it('carries the other exposures of a pass to fall back on, each marked at its own date', () => {
    const picked = pickFrames([frame(60900, 3, '2025-08-13'), frame(60905, 3, '2025-08-18'), frame(60910, 3, '2025-08-23')]);
    expect(picked[0].alternates.map((f) => f.mjd)).toEqual([60900, 60910]);
    const { views } = knownViews({ motion: m, patch, surveys: null, frames: picked, cutoutUrl: (f) => `u:${f.mjd}` });
    const [alt] = views[0].alternates;
    expect(alt.src).toBe('u:60900');
    expect(alt.date).toBe('2025-08-13');
    expect(alt.marks.ring.north).toBeLessThan(views[0].marks.ring.north); // 5 days earlier: further south
  });

  it('shows archive images alone until SPHEREx arrives, and no reference for a still star', () => {
    expect(knownViews({ motion: m, patch, surveys, frames: null, cutoutUrl: () => '' }).views.length).toBe(2);
    const still = { ra: 10, dec: 20, year: 2016, pmra: 0, pmdec: 0 };
    const { views } = knownViews({ motion: still, patch: { ra: 10, dec: 20, side: 150 }, surveys: null, frames, cutoutUrl: () => '' });
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

  it('puts the older surveys first, on one frame with the SPHEREx images', () => {
    const surveys = {
      ra: evidence.patch_ra, dec: evidence.patch_dec - 0.035, patch_arcsec: 432, unavailable: [],
      images: [{ survey: '2mass', label: '2MASS Ks', mjd: 51692, date: '2000-05-28', pixel_arcsec: 1, image_png_b64: 'A' }],
    };
    const { views } = engineViews(object, evidence, surveys);
    expect(views.map((v) => v.key)).toEqual(['survey:2mass', 'pass:0', 'pass:1', 'pass:2']);
    // ~26 years at 10.4″/yr north: the 2000 position is far south of the SPHEREx one.
    expect(views[0].marks.line).not.toBeNull();
    // The archive patch is centred 126″ south of the SPHEREx one: the SPHEREx images sit north
    // of the frame's centre, at their own scale, and every mark is in the frame's coordinates.
    const { side } = engineViews(object, evidence, surveys).frames.find((z) => z.key === 'wide');
    expect(side).toBeCloseTo(432, 2);
    expect(views[0].box).toEqual({ east: 0, north: 0, side: 432 });
    const pass = views[1];
    expect(pass.box.side).toBe(180);
    expect(pass.box.north).toBeCloseTo(126, 1);
    expect(pass.box.east).toBeCloseTo(0, 1);
    expect(pass.marks.ring.north).toBeCloseTo(126, 1); // its first position is the patch centre
    expect(views[3].marks.ring.north - pass.marks.ring.north).toBeCloseTo(10.4, 1);
    // 2000: ~26 years at 10.4″/yr south of the SPHEREx position, still on the archive image.
    expect(views[0].marks.ring.north).toBeLessThan(pass.marks.ring.north - 250);
  });

  it('makes the frame large enough for an image that reaches beyond the archive patch', () => {
    const surveys = { ra: evidence.patch_ra, dec: evidence.patch_dec - 0.035, patch_arcsec: 200, unavailable: [], images: [] };
    const { frames: zooms, views } = engineViews(object, evidence, surveys);
    const { side } = zooms.find((z) => z.key === 'wide');
    // SPHEREx box: 126″ north, 180″ wide, so it reaches 216″ from the centre.
    expect(side).toBeCloseTo(2 * (126 + 90), 0);
    expect(views.every((v) => v.box.side === 180)).toBe(true);
  });

  it('uses the SPHEREx patch as the frame without archive images', () => {
    expect(engineViews(object, evidence, null).frames).toEqual([{ key: 'spherex', label: 'SPHEREx passes', east: 0, north: 0, side: 180 }]);
  });
});

describe('stepping through the pictures and loading them', () => {
  const views = ['a', 'b', 'c', 'd'].map((key) => ({ key }));

  it('steps in time order, wrapping around', () => {
    expect(stepKey(views, 'a', 1)).toBe('b');
    expect(stepKey(views, 'd', 1)).toBe('a');
    expect(stepKey(views, 'a', -1)).toBe('d');
    expect(stepKey([], 'a', 1)).toBeNull();
  });

  it('goes to the first (or last) picture from one outside the sequence', () => {
    expect(stepKey(views, 'difference', 1)).toBe('a');
    expect(stepKey(views, 'difference', -1)).toBe('d');
    expect(stepKey(views, null, 1)).toBe('a');
  });

  it('skips pictures that failed', () => {
    expect(stepKey(views, 'a', 1, { b: 'error', c: 'ok' })).toBe('c');
    expect(stepKey(views, 'difference', 1, { a: 'error' })).toBe('b');
    expect(stepKey(views, 'a', 1, { a: 'error', b: 'error', c: 'error', d: 'error' })).toBeNull();
  });

  it('fetches only what was shown, the current picture and the next', () => {
    expect([...wantedKeys(views, 'a', new Set())].sort()).toEqual(['a', 'b']);
    expect([...wantedKeys(views, 'c', new Set(['a', 'b']))].sort()).toEqual(['a', 'b', 'c', 'd']);
    expect([...wantedKeys(views, 'd', new Set())].sort()).toEqual(['a', 'd']);
  });

  it('retries a remote picture at a fresh address, and never touches inline ones', () => {
    expect(isRemote('http://127.0.0.1:8011/api/cutout.png?url=x')).toBe(true);
    expect(isRemote('data:image/png;base64,AAA')).toBe(false);
    expect(imageSrc('http://h/api/cutout.png?url=x', 2)).toBe('http://h/api/cutout.png?url=x&retry=2');
    expect(imageSrc('http://h/a.png', 1)).toBe('http://h/a.png?retry=1');
    expect(imageSrc('data:image/png;base64,AAA', 3)).toBe('data:image/png;base64,AAA');
    expect(imageSrc('http://h/a.png', 0)).toBe('http://h/a.png');
  });
});

describe('zooming', () => {
  const at = (north) => ({ at: { east: 0, north } });
  const frames = [{ key: 'close', side: 150 }, { key: 'wide', side: 500 }];

  it('opens wide when the motion crosses at least 15% of the wide frame, else close up', () => {
    expect(defaultFrame(frames, [at(-270), at(0)])).toBe('wide'); // Barnard: 270″ in 514″
    expect(defaultFrame(frames, [at(-30), at(0)])).toBe('close'); // 30″: 6% of wide, 20% of close
    expect(defaultFrame(frames, [at(0), at(0.5)])).toBe('close'); // a slow star
    expect(defaultFrame(frames, [at(0)])).toBe('close');
    expect(defaultFrame([{ key: 'wide', side: 500 }], [at(0), at(1)])).toBe('wide');
  });

  it('opens an engine card on the SPHEREx passes, with the 25 years beside it', () => {
    const evidence = {
      patch_ra: 269.4459, patch_dec: 4.7673, patch_arcsec: 180, patch_pixel_arcsec: 3.075,
      fit: { ref_mjd: 61102.1, east_arcsec: 0, north_arcsec: 0, pm_east: -0.8, pm_north: 10.4 },
      difference_passes: [0, 0], difference_png_b64: 'D',
      passes: [{ index: 0, start: '2025-08-10', end: '2025-08-20', n_frames: 18, image_png_b64: 'P0', point: { east_arcsec: 0, north_arcsec: 0, mjd: 60905, date: '2025-08-18' } }],
    };
    const surveys = { ra: 269.4459, dec: 4.7673 - 0.035, patch_arcsec: 432, unavailable: [], images: [] };
    const r = engineViews({ kind: 'mover', pm_ra_cosdec: -0.8, pm_dec: 10.4, pm_err: 0.5, matches: [] }, evidence, surveys);
    expect(r.initial).toBe('spherex');
    expect(r.frames.map((f) => f.key)).toEqual(['spherex', 'wide']);
    // The close frame is the SPHEREx patch, wherever it sits in the wide one.
    expect(r.frames[0].north).toBeCloseTo(126, 1);
    expect(r.frames[0].side).toBe(180);
  });

  it('points to a position outside the frame from its edge', () => {
    const frame = { east: 0, north: 126, side: 180 };
    expect(offFrame({ east: 10, north: 140 }, frame)).toBeNull();
    expect(offFrame(null, frame)).toBeNull();
    // 2000: 270″ south of the SPHEREx position.
    const o = offFrame({ east: 0, north: 126 - 270 }, frame);
    expect(o.direction).toBe('south');
    expect(o.distance).toBeCloseTo(270, 6);
    expect(o.x).toBeCloseTo(0.5, 6);
    expect(o.y).toBeCloseTo(0.86, 6); // near the bottom edge, clear of the scale bar
    expect(o.angle).toBeCloseTo(180, 6); // arrow points down
    const east = offFrame({ east: 500, north: 126 }, frame);
    expect(east.direction).toBe('east');
    expect(east.x).toBeCloseTo(0.14, 6); // east is on the left
    expect(east.angle).toBeCloseTo(270, 6); // arrow points left
    expect(angleText(270)).toBe('4.5′');
    expect(angleText(40.4)).toBe('40″');
  });

  it('offers a close-up of a catalogued star around its newest position', () => {
    const m = { ra: 10, dec: 20, year: 2016, pmra: 0, pmdec: 10000 };
    const patch = picturePatch(m, 2026.8);
    const fr = [frame(61290, 3, '2026-09-07')];
    const r = knownViews({ motion: m, patch, surveys: null, frames: fr, cutoutUrl: () => '' });
    expect(r.frames.map((f) => f.key)).toEqual(['close', 'wide']);
    expect(r.frames[0].north).toBeCloseTo(r.views[0].at.north, 6);
    expect(r.frames[0].side).toBe(150);
  });
});

describe('requests the server would refuse', () => {
  it('fails clearly, without asking, for a patch with no usable position or an out-of-range side', async () => {
    vi.stubEnv('VITE_API_BASE', 'http://api.test/api');
    vi.resetModules();
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    try {
      const pictures = await import('./pictures.js');
      const evidence = await import('./evidence.js');
      await expect(pictures.fetchPatchFrames({ ra: NaN, dec: -56.8, side: 281 })).rejects.toThrow('no usable position');
      await expect(evidence.fetchPatchSurveys({ ra: 331.07, dec: undefined, side: 281 })).rejects.toThrow('no usable position');
      await expect(evidence.fetchPatchSurveys({ ra: 331.07, dec: -56.79, side: 1200 })).rejects.toThrow('the server takes 30–900″');
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.resetModules();
    }
  });

  it('asks for Epsilon Indi B (a fast mover known to SIMBAD only here) with a patch the server takes', () => {
    // SIMBAD J2000 position moved to today, as querySimbad returns it; no Gaia star joined.
    const row = { ra: 331.0984, dec: -56.80126, gaia: null, simbad: { ra: 331.0984, dec: -56.80126, pmra: 3981.977, pmdec: -2466.832 } };
    const patch = picturePatch(knownMotion(row, 2026.87), 2026.87);
    expect([patch.ra, patch.dec, patch.side].every(Number.isFinite)).toBe(true);
    expect(patch.side).toBeGreaterThanOrEqual(30);
    expect(patch.side).toBeLessThanOrEqual(900);
  });
});
