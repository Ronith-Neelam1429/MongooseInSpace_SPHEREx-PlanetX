import { afterEach, describe as suite, expect, it, vi } from 'vitest';
import {
  brightnessSeries, compass, dayMonthYear, describe, measure, monthYear, motionChart, sameObject, ticks,
} from './evidence.js';

// Barnard's Star as the backend reported it (cached field 269.448 +4.739), images left out.
const pass = (index, start, end, point, scatter) => ({
  index, start, end, n_frames: 18, coverage: 1, image_png_b64: '', point, field_scatter: scatter,
});
const point = (east, north, sigma, mjd, date, fluxes = []) => ({
  east_arcsec: east, north_arcsec: north, sigma_arcsec: sigma, mjd, date, n_frames: 17, fluxes,
});
const scatter = (sigma, median, n) => ({ sigma_arcsec: sigma, median_shift_arcsec: median, n_stars: n });

const barnardEvidence = () => ({
  object: 0, ra: 269.4461531, dec: 4.7687503,
  patch_ra: 269.4459059, patch_dec: 4.7672926, patch_arcsec: 150.675, patch_px: 49, patch_pixel_arcsec: 3.075,
  spherex_pixel_arcsec: 6.154,
  passes: [
    pass(0, '2025-08-15', '2025-10-01', point(0, 0, 1.231, 60914.93, '2025-08-27'), null),
    pass(1, '2026-03-11', '2026-05-05', point(2.4087, 5.992, 1.231, 61115.26, '2026-03-16'), scatter(0.306, 0.369, 245)),
    pass(2, '2026-08-16', '2026-09-22', point(0.2523, 9.751, 1.231, 61276.11, '2026-08-24'), scatter(0.302, 0.356, 264)),
  ],
  difference_png_b64: '', difference_passes: [0, 2],
  fit: { ref_mjd: 61102.1, east_arcsec: 0.8868, north_arcsec: 5.2477, pm_east: 0.4217, pm_north: 9.9036 },
  pm_floor_arcsec_yr: 0.307,
});

const object = (over = {}) => ({
  id: 0, kind: 'mover', is_variable: false, ra: 269.4461531, dec: 4.7687503,
  pm_ra_cosdec: 0.4217, pm_dec: 9.9036, pm_total: 9.9125, pm_err: 1.9507, significance: 5.0816,
  variability: null, matches: [],
  archive: { verdict: 'unconfirmed', pm_ra_cosdec: 0.4217, pm_dec: 9.9036, pm_err: 1.93, baseline_yr: 0.99, observatories: ['spherex'] },
  ...over,
});

/** Evidence of an object that stayed put: the same passes with small offsets. */
function stillEvidence(lastEast = 0.15, lastNorth = -0.1) {
  const ev = barnardEvidence();
  ev.passes[1].point = point(0.2, 0.1, 0.5, 61115.26, '2026-03-16');
  ev.passes[2].point = point(lastEast, lastNorth, 0.5, 61276.11, '2026-08-24');
  ev.fit = { ref_mjd: 61102.1, east_arcsec: 0.1, north_arcsec: 0, pm_east: 0.15, pm_north: -0.1 };
  return ev;
}
const still = (over = {}) => object({
  kind: 'static', pm_ra_cosdec: 0.15, pm_dec: -0.1, pm_total: 0.18, pm_err: 0.6, significance: 0.3, ...over,
});

suite('dates and directions', () => {
  it('formats dates for people', () => {
    expect(monthYear('2025-08-27')).toBe('Aug 2025');
    expect(dayMonthYear('2026-03-05')).toBe('5 Mar 2026');
  });
  it('names the direction of an (east, north) vector', () => {
    expect(compass(0, 1)).toBe('north');
    expect(compass(1, 1)).toBe('north-east');
    expect(compass(1, 0)).toBe('east');
    expect(compass(-1, 0)).toBe('west');
    expect(compass(-0.1, -1)).toBe('south');
    expect(compass(-1, 1)).toBe('north-west');
  });
});

suite('measure', () => {
  it('compares the object’s shift with the field’s own scatter', () => {
    const m = measure(object(), barnardEvidence());
    expect(m.shift).toBeCloseTo(9.754, 3);
    expect(m.pixels).toBeCloseTo(1.585, 3);
    expect(m.noise).toBe(0.356);
    expect(m.stars).toBe(264);
    expect(m.span).toBe('between Aug 2025 and Aug 2026');
    expect([m.seen, m.passes]).toEqual([3, 3]);
  });
  it('uses the first and last pass that measured the object', () => {
    const ev = barnardEvidence();
    ev.passes[2].point = null;
    const m = measure(object(), ev);
    expect(m.shift).toBeCloseTo(Math.hypot(2.4087, 5.992), 6);
    expect(m.noise).toBe(0.369);
    expect(m.span).toBe('between Aug 2025 and Mar 2026');
    expect([m.seen, m.passes]).toEqual([2, 3]);
  });
});

suite('describe', () => {
  it('says plainly that a mover moved, against the noise', () => {
    const v = describe(object(), barnardEvidence());
    expect(v.tone).toBe('moved');
    expect(v.headline).toBe('It moved.');
    expect(v.lines).toEqual([
      'Moved 9.8″ to the north (1.6 SPHEREx pixels) between Aug 2025 and Aug 2026.',
      'The 264 other objects in this field, nearly all fixed stars, appear to shift by about 0.4″ over the same time: that is the measurement noise.',
      'Its measured speed, 9.91″ per year, is 5.1 times the measurement error.',
    ]);
  });

  it('adds the archival track when older surveys confirm a visible mover', () => {
    const v = describe(object({
      archive: { verdict: 'confirmed', pm_ra_cosdec: -0.82, pm_dec: 10.362, pm_err: 0.029, baseline_yr: 26.6, observatories: ['spherex', 'twomass'] },
    }), barnardEvidence());
    expect(v.headline).toBe('It moved.');
    expect(v.lines[3]).toBe('Positions from 2MASS, going back 27 years, lie on the same track: 10.39″ per year.');
  });

  it('does not oversell a mover that only older surveys can show', () => {
    const ev = stillEvidence(1.1, -1.3);
    const v = describe(object({
      pm_ra_cosdec: 0.61, pm_dec: -1.51, pm_total: 1.63, pm_err: 1.14, significance: 1.43,
      archive: { verdict: 'confirmed', pm_ra_cosdec: -1.292, pm_dec: -1.53, pm_err: 0.07, baseline_yr: 16.2, observatories: ['spherex', 'allwise', 'catwise'] },
    }), ev);
    expect(v.tone).toBe('moved');
    expect(v.headline).toBe('A slow mover, shown by older surveys.');
    expect(v.lines[0]).toBe('In SPHEREx alone it shifted 1.7″ (0.3 SPHEREx pixels) between Aug 2025 and Aug 2026, 1.4 times the measurement error: too little to see in these images.');
    expect(v.lines[2]).toBe('Positions from AllWISE and CatWISE, going back 16 years, show a steady 2.00″ per year. That is why the engine lists it as a mover.');
  });

  it('calls a 3-5 sigma track a candidate and says what would settle it', () => {
    const ev = stillEvidence(5.16, 6.16);
    const v = describe(object({ kind: 'candidate', pm_ra_cosdec: 5.05, pm_dec: 5.92, pm_total: 7.78, pm_err: 2.25, significance: 3.46 }), ev);
    expect(v.tone).toBe('candidate');
    expect(v.headline).toBe('Candidate: it may have moved.');
    expect(v.lines[0]).toBe('Appears to have moved 8.0″ to the north-east (1.3 SPHEREx pixels) between Aug 2025 and Aug 2026.');
    expect(v.lines[2]).toContain('3.5 times the measurement error');
    expect(v.lines[2]).toContain('It stays a candidate until another pass or another survey confirms it.');
  });

  it('says just as plainly that a fixed star did not move', () => {
    const v = describe(still(), stillEvidence());
    expect(v.tone).toBe('still');
    expect(v.headline).toBe('No motion detected.');
    expect(v.lines).toEqual([
      'Its position changed by 0.2″ (under a tenth of a SPHEREx pixel) between Aug 2025 and Aug 2026, 0.3 times the measurement error.',
      'The 264 other objects in this field, nearly all fixed stars, appear to shift by about 0.4″ over the same time: that is the measurement noise.',
    ]);
  });

  it('is firmer when decades of archival positions agree', () => {
    const v = describe(still({
      archive: { verdict: 'stationary', pm_ra_cosdec: 0.078, pm_dec: 0.024, pm_err: 0.05, baseline_yr: 16.2, observatories: ['spherex', 'catwise', 'allwise'] },
    }), stillEvidence());
    expect(v.headline).toBe('It did not move.');
    expect(v.lines[2]).toBe('Positions from CatWISE and AllWISE, going back 16 years, show no motion either.');
  });

  it('explains why a significant but sub-pixel shift is not a mover', () => {
    const v = describe(still({ pm_total: 4.05, pm_err: 0.75, significance: 5.37 }), stillEvidence(-3.9, 1.1));
    expect(v.headline).toBe('No reliable motion.');
    expect(v.lines[0]).toBe('Its position changed by 4.1″ (0.7 SPHEREx pixels) between Aug 2025 and Aug 2026, 5.4 times the measurement error.');
    expect(v.lines[2]).toBe('That is less than one SPHEREx pixel, too little for the engine to call it a mover.');
  });

  it('explains a track that misses a pass, or has only two', () => {
    const gap = stillEvidence(7.9, 11.5);
    gap.passes[1].point = null;
    expect(describe(still({ pm_total: 13.6, pm_err: 1.1, significance: 12.5 }), gap).lines[2])
      .toBe('It was not found in every pass, so these positions may belong to different objects.');
    const two = stillEvidence(7.9, 11.5);
    two.passes.splice(1, 1);
    expect(describe(still({ pm_total: 13.6, pm_err: 1.1, significance: 12.5 }), two).lines[2])
      .toBe('Only two passes saw it. Two points always fit a line, so the engine asks for more before calling it a mover.');
  });

  it('reports a refuted track as a mix-up, not as motion', () => {
    const v = describe(still({
      pm_total: 13.6, pm_err: 1.1, significance: 12.5,
      archive: { verdict: 'refuted', pm_ra_cosdec: 7.7, pm_dec: 11.2, pm_err: 1.1, baseline_yr: 1.0, observatories: ['spherex'] },
    }), stillEvidence(7.9, 11.5));
    expect(v.tone).toBe('refuted');
    expect(v.headline).toBe('Not a moving object.');
    expect(v.lines).toEqual([
      'The SPHEREx positions differ by 14.0″ (2.3 SPHEREx pixels) between Aug 2025 and Aug 2026, but older catalogues show a separate, fixed source at each position. The engine linked two neighbours; nothing moved.',
    ]);
  });

  it('compares with a catalogued proper motion', () => {
    const gaia = { catalog: 'gaia', name: 'Barnard’s Star', kind: 'High proper-motion star', sep_arcsec: 0.4, pm_ra_cosdec: -0.8, pm_dec: 10.36, magnitude: 8.2 };
    const agree = describe(object({ matches: [{ catalog: 'simbad', name: 'x', sep_arcsec: 1, pm_ra_cosdec: null, pm_dec: null }, gaia] }), barnardEvidence());
    expect(agree.lines[3]).toBe('Gaia DR3 lists Barnard’s Star at 10.39″ per year to the north; SPHEREx measured 9.91 ± 1.95 to the north. The two agree within the measurement error.');
    const differ = describe(object({ pm_err: 0.3, matches: [gaia] }), barnardEvidence());
    expect(differ.lines[3]).toContain('They differ by 1.30″ per year, 4.3 times the measurement error.');
  });

  it('describes a brightness change, with the engine’s caution', () => {
    const variability = { band: 'D3', ratio: 1.786, significance: 12.04, change: 'P3/P1 = 0.56', caution: '80" from a very bright star; its scattered light may cause this' };
    const v = describe(still({ is_variable: true, variability }), stillEvidence());
    expect(v.variable).toBe(true);
    expect(v.headline).toBe('Its brightness changed.');
    expect(v.lines.slice(2)).toEqual([
      'In pass 3 it was 0.56 times as bright as in pass 1, comparing frames at matching wavelengths (D3): 12.0 times the measurement error.',
      'Caution: 80" from a very bright star; its scattered light may cause this.',
    ]);
    const moverToo = describe(object({ is_variable: true, variability: { ...variability, change: 'brighter', caution: null } }), barnardEvidence());
    expect(moverToo.headline).toBe('It moved.');
    expect(moverToo.lines[3]).toContain('Its brightness changed by a factor of 1.79 (brighter)');
  });

  it('never claims a discovery', () => {
    const cases = [
      describe(object(), barnardEvidence()),
      describe(object({ kind: 'candidate', significance: 3.5 }), barnardEvidence()),
      describe(still(), stillEvidence()),
    ];
    for (const v of cases) expect(`${v.headline} ${v.lines.join(' ')}`).not.toMatch(/discover/i);
  });
});

suite('motionChart', () => {
  it('lays out points, the fit and the fixed-star band on shared axes', () => {
    const c = motionChart(object(), barnardEvidence());
    expect(c.points.map((p) => p.index)).toEqual([0, 1, 2]);
    expect(c.points[2]).toMatchObject({ east: 0.2523, north: 9.751, sigma: 1.231, date: '2026-08-24' });
    // The time axis pads the first and last pass.
    expect(c.x[0]).toBeLessThan(60914.93);
    expect(c.x[1]).toBeGreaterThan(61276.11);
    // One y range for east and north: down to one SPHEREx pixel below zero, up past the last point's error bar.
    expect(c.y[0]).toBeLessThan(-6.154);
    expect(c.y[1]).toBeGreaterThan(9.751 + 1.231);
    expect(c.pixel).toBe(6.154);
    // Fixed stars start at zero by construction and scatter by their per-axis sigma later.
    expect(c.band).toEqual([{ mjd: 60914.93, sigma: 0 }, { mjd: 61115.26, sigma: 0.306 }, { mjd: 61276.11, sigma: 0.302 }]);
    expect(c.lines.map((l) => l.kind)).toEqual(['fit']);
  });

  it('draws the fitted line through the fitted position with the fitted slope', () => {
    const c = motionChart(object(), barnardEvidence());
    const { north, east } = c.lines[0];
    const years = (c.x[1] - c.x[0]) / 365.25;
    expect((north[1] - north[0]) / years).toBeCloseTo(9.9036, 6);
    expect((east[1] - east[0]) / years).toBeCloseTo(0.4217, 6);
    const atRef = north[0] + (north[1] - north[0]) * (61102.1 - c.x[0]) / (c.x[1] - c.x[0]);
    expect(atRef).toBeCloseTo(5.2477, 6);
  });

  it('adds the catalogue prediction and the archival refit through the same point', () => {
    const c = motionChart(object({
      matches: [{ catalog: 'gaia', name: 'Barnard’s Star', sep_arcsec: 0.4, pm_ra_cosdec: -0.8, pm_dec: 10.36 }],
      archive: { verdict: 'confirmed', pm_ra_cosdec: -0.82, pm_dec: 10.362, pm_err: 0.029, baseline_yr: 26.6, observatories: ['spherex', 'twomass', 'allwise'] },
    }), barnardEvidence());
    expect(c.lines.map((l) => [l.kind, l.label])).toEqual([
      ['fit', 'SPHEREx fit'], ['archive', 'with 2MASS and AllWISE'], ['catalog', 'Gaia DR3 catalogue'],
    ]);
    const years = (c.x[1] - c.x[0]) / 365.25;
    const frac = (61102.1 - c.x[0]) / (c.x[1] - c.x[0]);
    for (const [line, pmNorth] of [[c.lines[1], 10.362], [c.lines[2], 10.36]]) {
      expect((line.north[1] - line.north[0]) / years).toBeCloseTo(pmNorth, 6);
      expect(line.north[0] + (line.north[1] - line.north[0]) * frac).toBeCloseTo(5.2477, 6);
      expect(line.east[0] + (line.east[1] - line.east[0]) * frac).toBeCloseTo(0.8868, 6);
    }
  });

  it('keeps one pixel of range for an object that stayed put, so noise is not magnified', () => {
    const c = motionChart(still(), stillEvidence());
    expect(c.y[0]).toBeLessThan(-6.154);
    expect(c.y[1]).toBeGreaterThan(6.154);
    expect(c.y[1] - c.y[0]).toBeLessThan(15);
  });

  it('has no band when the field gave no scatter, and skips unmeasured passes', () => {
    const ev = barnardEvidence();
    ev.passes[1].point = null;
    ev.passes[2].field_scatter = null;
    const c = motionChart(object(), ev);
    expect(c.points.map((p) => p.index)).toEqual([0, 2]);
    expect(c.band).toBeNull();
  });
});

suite('ticks', () => {
  it('picks round steps', () => {
    expect(ticks(-7.2, 12.4)).toEqual([-5, 0, 5, 10]);
    expect(ticks(-7, 7)).toEqual([-5, 0, 5]);
    expect(ticks(0.4, 1.3)).toEqual([0.5, 1]);
    expect(ticks(-0.6, 30)).toEqual([0, 10, 20, 30]);
  });
});

suite('brightnessSeries', () => {
  it('scales each band to its first measured pass', () => {
    const ev = stillEvidence();
    const flux = (band, value, err) => ({ band, flux: value, flux_err: err });
    ev.passes[0].point.fluxes = [flux('D3', 0.8, 0.08), flux('D2', 0.6, 0.06)];
    ev.passes[1].point.fluxes = [flux('D3', 0.72, 0.04), flux('D1', 0.7, 0.1)];
    ev.passes[2].point.fluxes = [flux('D3', 0.4, 0.04), flux('D2', 0.3, 0.03), flux('D1', -0.1, 0.1)];
    const series = brightnessSeries(ev);
    expect(series.map((s) => s.band)).toEqual(['D2', 'D3']); // D1 has one usable pass only
    expect(series[1].points.map((p) => [p.index, +p.value.toFixed(3), +p.err.toFixed(3)]))
      .toEqual([[0, 1, 0.1], [1, 0.9, 0.05], [2, 0.5, 0.05]]);
    expect(series[0].points.map((p) => p.index)).toEqual([0, 2]);
    expect(series[0].points[1].mjd).toBe(61276.11);
  });
});

suite('sameObject', () => {
  it('finds an object again by position after a re-run renumbered it', () => {
    const wanted = { ra: 269.4461531, dec: 4.7687503 };
    const objects = [
      { id: 0, ra: 269.39, dec: 4.815 },
      { id: 1, ra: 269.4461531 + 1.0 / 3600, dec: 4.7687503 },
      { id: 2, ra: 269.4461531 + 2.5 / 3600, dec: 4.7687503 },
    ];
    expect(sameObject(objects, wanted).id).toBe(1);
    expect(sameObject(objects.slice(2), wanted).id).toBe(2);
    expect(sameObject([{ id: 0, ra: 269.4461531, dec: 4.7687503 + 4 / 3600 }], wanted)).toBeNull();
  });
});

suite('fetchEvidence', () => {
  afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.resetModules(); });

  const field = { ra: 269.448, dec: 4.739, size_deg: 0.1 };
  const ok = (body) => ({ ok: true, status: 200, json: async () => body });
  const notKept = { ok: false, status: 404, json: async () => ({ detail: 'no finished motion run is kept for this field' }) };

  /** Loads the module against a fake backend: queued answers for the evidence and the motion endpoints. */
  async function load({ evidence = [], motion = [], base = 'http://api.test/api' } = {}) {
    vi.stubEnv('VITE_API_BASE', base);
    vi.resetModules();
    const calls = [];
    vi.stubGlobal('fetch', vi.fn(async (url) => {
      calls.push(url);
      const answer = (url.includes('/motion/evidence') ? evidence : motion).shift();
      if (!answer) throw new Error(`unexpected request ${url}`);
      return answer;
    }));
    const { fetchEvidence } = await import('./evidence.js');
    return { fetchEvidence, calls };
  }

  it('asks for the object of the run by its id', async () => {
    const { fetchEvidence, calls } = await load({ evidence: [ok(barnardEvidence())] });
    expect(await fetchEvidence(field, object())).toEqual(barnardEvidence());
    expect(calls).toEqual(['http://api.test/api/motion/evidence?ra=269.448000&dec=4.739000&size_deg=0.1000&object=0']);
  });

  it('re-runs the field when its data is gone and finds the object again by position', async () => {
    const renumbered = { ...object(), id: 4 };
    const rerun = { status: 'done', ra: 269.448, dec: 4.739, size_deg: 0.1, result: { objects: [{ id: 0, ra: 269.39, dec: 4.8 }, renumbered] } };
    const { fetchEvidence, calls } = await load({
      evidence: [notKept, ok({ ...barnardEvidence(), object: 4 })],
      motion: [ok(rerun)],
    });
    const progress = [];
    const got = await fetchEvidence(field, object(), { onProgress: (job) => progress.push(job.stage) });
    expect(got.object).toBe(4);
    expect(calls.map((u) => u.replace('http://api.test/api', ''))).toEqual([
      '/motion/evidence?ra=269.448000&dec=4.739000&size_deg=0.1000&object=0',
      '/motion?ra=269.448000&dec=4.739000&size_deg=0.1000',
      '/motion/evidence?ra=269.448000&dec=4.739000&size_deg=0.1000&object=4',
    ]);
    expect(progress).toEqual(['queued']);
  });

  it('does not trust an answer about a different object (the run was renumbered)', async () => {
    const other = { ...barnardEvidence(), ra: 269.39, dec: 4.8 };
    const rerun = { status: 'done', ra: 269.448, dec: 4.739, size_deg: 0.1, result: { objects: [{ ...object(), id: 7 }] } };
    const { fetchEvidence, calls } = await load({ evidence: [ok(other), ok({ ...barnardEvidence(), object: 7 })], motion: [ok(rerun)] });
    expect((await fetchEvidence(field, object())).object).toBe(7);
    expect(calls[2]).toContain('object=7');
  });

  it('fails clearly when the re-run no longer contains the object', async () => {
    const rerun = { status: 'done', ra: 269.448, dec: 4.739, size_deg: 0.1, result: { objects: [{ id: 0, ra: 269.39, dec: 4.8 }] } };
    const { fetchEvidence } = await load({ evidence: [notKept], motion: [ok(rerun)] });
    await expect(fetchEvidence(field, object())).rejects.toThrow('did not find this object again');
  });

  it('gives up after one re-run', async () => {
    const rerun = { status: 'done', ra: 269.448, dec: 4.739, size_deg: 0.1, result: { objects: [object()] } };
    const { fetchEvidence, calls } = await load({ evidence: [notKept, notKept], motion: [ok(rerun)] });
    await expect(fetchEvidence(field, object())).rejects.toThrow('no longer holds this run');
    expect(calls).toHaveLength(3);
  });

  it('passes on the reason when the re-run fails', async () => {
    const { fetchEvidence } = await load({ evidence: [notKept], motion: [ok({ status: 'failed', error: 'IRSA is temporarily unavailable' })] });
    await expect(fetchEvidence(field, object())).rejects.toThrow('IRSA is temporarily unavailable');
  });

  it('rejects on server errors and malformed bodies without re-running', async () => {
    const a = await load({ evidence: [{ ok: false, status: 500, json: async () => ({}) }] });
    await expect(a.fetchEvidence(field, object())).rejects.toThrow('HTTP 500');
    expect(a.calls).toHaveLength(1);
    const b = await load({ evidence: [ok({ passes: [] })] });
    await expect(b.fetchEvidence(field, object())).rejects.toThrow('bad response');
  });

  it('stops when aborted', async () => {
    const { fetchEvidence } = await load({ evidence: [] });
    vi.stubGlobal('fetch', vi.fn((url, { signal }) => new Promise((_, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason));
    })));
    const ctrl = new AbortController();
    const pending = fetchEvidence(field, object(), { signal: ctrl.signal });
    ctrl.abort(new DOMException('gone', 'AbortError'));
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('resolves to null without a backend', async () => {
    const { fetchEvidence, calls } = await load({ base: '' });
    expect(await fetchEvidence(field, object())).toBeNull();
    expect(calls).toEqual([]);
  });
});
