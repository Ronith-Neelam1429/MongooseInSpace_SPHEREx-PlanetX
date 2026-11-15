import { describe, expect, it } from 'vitest';
import {
  causeRows, confusionText, crossing, edges, falsePositiveText, gaiaPoints, gaussianCurve, generatedText, histogram,
  injectionHeatmap, injectionText, lightCurve, mjdToDate, niceTicks, precisionGain, rateText, spectralPoints, spectralText,
  stdDev, surveySummary, symlog,
} from './results.js';

// Small fixtures in docs/RESULTS.md shapes (the backend's live-like ones are in tests/fixtures/results/).
const gaiaDoc = {
  objects: [
    { spherex_pm_ra: 0.3, spherex_pm_dec: -0.4, spherex_pm_err: 0.5, gaia_pm_ra: 0, gaia_pm_dec: 0, gaia_pm_err: 0, engine_is_mover: false, truth: 'static', g_mag: 14 },
    { spherex_pm_ra: -0.5, spherex_pm_dec: 10.4, spherex_pm_err: 1.77, gaia_pm_ra: -0.8, gaia_pm_dec: 10.36, gaia_pm_err: 0.0001, engine_is_mover: true, truth: 'real_mover', g_mag: 9.5 },
    { spherex_pm_ra: null, spherex_pm_dec: 1, spherex_pm_err: 1, gaia_pm_ra: 0, gaia_pm_dec: 0 }, // not measured
    { spherex_pm_ra: 1, spherex_pm_dec: 1, spherex_pm_err: 0, gaia_pm_ra: 0, gaia_pm_dec: 0 }, // no error
  ],
};

describe('symlog', () => {
  it('is zero at zero, odd, and logarithmic far from zero', () => {
    expect(symlog(0)).toBe(0);
    expect(symlog(-3)).toBeCloseTo(-symlog(3));
    expect(symlog(0.01, 0.01)).toBeCloseTo(Math.log10(2));
    expect(symlog(100) - symlog(10)).toBeCloseTo(1, 1);
  });
});

describe('gaiaPoints', () => {
  it('keeps objects with both motions and a SPHEREx error, with residuals and pulls', () => {
    const pts = gaiaPoints(gaiaDoc);
    expect(pts).toHaveLength(2);
    expect(pts[0]).toMatchObject({ gaia: 0, spherex: 0.5, truth: 'static', mover: false });
    expect(pts[0].resid).toEqual([0.3, -0.4]);
    expect(pts[0].pull[0]).toBeCloseTo(0.6);
    expect(pts[1].gaia).toBeCloseTo(10.39, 2);
  });
  it('copes with a missing document', () => expect(gaiaPoints(null)).toEqual([]));
});

describe('histogram', () => {
  it('bins values over [lo, hi) and counts the overflow on each side', () => {
    const h = histogram([-5, -1, -0.5, 0, 0.49, 0.5, 1, 2.9, 3, NaN], -1, 1, 4);
    expect(h.bins.map((b) => b.count)).toEqual([1, 1, 2, 1]); // 0.5 opens the last bin; 1 is past it
    expect(h).toMatchObject({ under: 1, over: 3, n: 9, width: 0.5 });
  });
});

describe('gaussianCurve', () => {
  it('peaks at n * width / sqrt(2 pi) and integrates to about n', () => {
    const c = gaussianCurve(1000, 0.5, -6, 6, 120);
    expect(c[60].x).toBeCloseTo(0);
    expect(c[60].y).toBeCloseTo(1000 * 0.5 / Math.sqrt(2 * Math.PI));
    const area = c.slice(1).reduce((s, p, i) => s + ((p.y + c[i].y) / 2) * (p.x - c[i].x), 0) / 0.5;
    expect(area).toBeCloseTo(1000, -1);
  });
});

describe('stdDev', () => {
  it('ignores non-numbers and needs two values', () => {
    expect(stdDev([1, NaN, 3])).toBeCloseTo(Math.SQRT2);
    expect(stdDev([1])).toBeNull();
  });
});

describe('confusionText', () => {
  it('says the matrix in plain words from Gaia\'s side', () => {
    const t = confusionText({ tp: 6, fp: 1, fn: 2, tn: 249 });
    expect(t.still).toBe('Of 250 stars Gaia says are still, we wrongly called 1 moving (0.4%).');
    expect(t.moving).toBe('Of 8 stars Gaia says move fast enough for SPHEREx to see, we caught 6 and missed 2 (75% caught).');
  });
  it('leaves out an empty row', () => {
    const t = confusionText({ tp: 0, fp: 0, fn: 0, tn: 1 });
    expect(t.still).toBe('Of 1 star Gaia says are still, we wrongly called 0 moving (0%).');
    expect(t.moving).toBeNull();
  });
});

describe('causeRows', () => {
  it('groups fake movers by cause, most common first, with a plain sentence each', () => {
    const doc = {
      movers: [
        { cause: 'blend', evidence: 'A' }, { cause: 'blend', evidence: 'A' }, { cause: 'blend', evidence: 'B' },
        { cause: 'edge', evidence: 'C' }, { cause: null }, { cause: 'something_new', evidence: 'D' },
      ],
      summary: { by_cause: { blend: 3, edge: 1, low_snr: 2 } },
    };
    const rows = causeRows(doc);
    expect(rows.map((r) => [r.cause, r.count])).toEqual([['blend', 3], ['low_snr', 2], ['edge', 1], ['other', 1]]);
    expect(rows[0].examples).toEqual(['A', 'B']);
    expect(rows.every((r) => r.sentence && r.label)).toBe(true);
  });
});

describe('injection heatmap', () => {
  const doc = {
    grid: [
      { pm_arcsec_yr: 0.5, mag_bin: 12, n_injected: 10, n_recovered: 1, recovered_fraction: 0.1 },
      { pm_arcsec_yr: 1, mag_bin: 12, n_injected: 10, n_recovered: 5, recovered_fraction: 0.5 },
      { pm_arcsec_yr: 2, mag_bin: 12, n_injected: 10, n_recovered: 9, recovered_fraction: 0.9 },
      { pm_arcsec_yr: 4, mag_bin: 12, n_injected: 10, n_recovered: 10, recovered_fraction: 1 },
      { pm_arcsec_yr: 1, mag_bin: 16, n_injected: 10, n_recovered: 0 }, // fraction computed
      { pm_arcsec_yr: 4, mag_bin: 16, n_injected: 10, n_recovered: 3 },
    ],
  };
  it('sorts the axes and fills cells, computing missing fractions', () => {
    const h = injectionHeatmap(doc);
    expect(h.speeds).toEqual([0.5, 1, 2, 4]);
    expect(h.mags).toEqual([12, 16]);
    expect(h.cells.find((c) => c.pm === 4 && c.mag === 16).fraction).toBeCloseTo(0.3);
  });
  it('finds the 50% and 90% speeds per brightness, null where never reached', () => {
    const h = injectionHeatmap(doc);
    expect(h.lines[50]).toEqual([{ mag: 12, pm: 1 }, { mag: 16, pm: null }]);
    expect(h.lines[90][0].pm).toBeCloseTo(2);
  });
  it('reads the real file: one mode, pooled field, text bins, flagged share', () => {
    const row = (mode, field, pm, flagged) => ({
      mode, field, pm_arcsec_yr: pm, mag_bin: '11.5-12.5', mag_ab: 12, n_injected: 10, n_recovered: 10,
      n_flagged_mover: flagged, recovered_fraction: 1, flagged_fraction: flagged / 10,
    });
    const h = injectionHeatmap({ grid: [
      row('single', 'all', 4, 0), row('single', 'all', 8, 10), row('stack', 'all', 8, 2), row('single', 'nep', 8, 3),
    ] });
    expect(h.speeds).toEqual([4, 8]);
    expect(h.mags).toEqual([12]);
    expect(h.cells.map((c) => c.fraction)).toEqual([0, 1]);
    expect(injectionHeatmap({ grid: [row('stack', 'all', 8, 2)] }, 'stack').cells[0].fraction).toBeCloseTo(0.2);
    expect(injectionText({ summary: { pm_50: 4.957142857, pm_90: 7, mag_50: 17.2394 } })).toMatch(/4\.96? *″|5″/);
  });
  it('interpolates in log speed', () => {
    expect(crossing([{ pm: 1, fraction: 0 }, { pm: 4, fraction: 1 }], 0.5)).toBeCloseTo(2);
  });
  it('puts cell edges halfway between centres', () => {
    expect(edges([1, 2, 4], true).map((v) => +v.toFixed(4))).toEqual([0.7071, 1.4142, 2.8284, 5.6569]);
    expect(edges([10, 11, 12])).toEqual([9.5, 10.5, 11.5, 12.5]);
  });
  it('words the summary and false alarms', () => {
    expect(injectionText({ summary: { pm_50: 1.4, pm_90: 3.6, mag_50: 14.6 } }))
      .toBe('Half of the planted movers are found once they move 1.4″ a year, and nine in ten at 3.6″ a year; below magnitude 14.6 (fainter) most are lost.');
    expect(injectionText({})).toBeNull();
    expect(falsePositiveText({ n_static_injected: 400, n_flagged_mover: 3 })).toBe('Of 400 planted stars that do not move, 3 were wrongly flagged as moving (0.8%).');
  });
});

describe('spectral points', () => {
  const doc = {
    objects: [
      { kind: 'mover', methane_depth: 0.3, water_depth: 0.3, flag: 'suggests_brown_dwarf' },
      { kind: 'reference_star', methane_depth: 0, water_depth: 0.01, flag: 'star_like' },
      { kind: 'known_brown_dwarf', name: 'eps Ind B', methane_depth: 0.5, water_depth: 0.4, flag: 'suggests_brown_dwarf' },
      { kind: 'mover', methane_depth: null, water_depth: 0.1 },
      { kind: 'mover', methane_depth: 0.01, water_depth: 0.02, flag: 'star_like' },
    ],
  };
  it('keeps measured objects, stars drawn first', () => {
    const pts = spectralPoints(doc);
    expect(pts.map((p) => p.kind)).toEqual(['reference_star', 'mover', 'mover', 'known_brown_dwarf']);
    expect(pts[3].name).toBe('eps Ind B');
  });
  it('says "suggests", never more', () => {
    expect(spectralText(spectralPoints(doc)))
      .toBe('1 of 1 known brown dwarfs show both dips; 1 of our 2 moving candidates do too, which suggests it is a brown dwarf.');
  });
});

describe('lightCurve', () => {
  it('sorts by date, folds on the fitted period and gives the brightness range', () => {
    const lc = lightCurve({ points: [{ mjd: 61000, flux: 4 }, { mjd: 60755, flux: 1, flux_err: 0.1 }], period_days_fit: 245, period_days_literature: 245.6 });
    expect(lc.points.map((p) => p.mjd)).toEqual([60755, 61000]);
    expect(lc.folded.map((p) => p.phase)).toEqual([0, 0]);
    expect(lc).toMatchObject({ period: 245, literature: 245.6, ratio: 4 });
  });
  it('is null without points, and unfolded without a period', () => {
    expect(lightCurve({ points: [] })).toBeNull();
    expect(lightCurve({ points: [{ mjd: 1, flux: 1 }] }).folded).toBeNull();
  });
  it('converts MJD to dates', () => expect(mjdToDate(60993).toISOString()).toBe('2025-11-14T00:00:00.000Z'));
});

describe('surveySummary', () => {
  const hour = (h, done, failed = 0) => ({ start: new Date(Date.UTC(2026, 10, 14, h)).toISOString(), done, failed });
  const stats = {
    available: true,
    patches: { pending: 90, in_progress: 1, done: 8, failed: 1 },
    total_patches: 100,
    objects_measured: 20000,
    candidates_total: 5,
    by_classification: { unchecked: 3, known_star: 1, unknown_candidate: 1 },
    last_day: Array.from({ length: 24 }, (_, i) => hour(i, i >= 20 ? 2 : 0)),
    first_finished_at: new Date(Date.UTC(2026, 10, 14, 20)).toISOString(),
    last_finished_at: new Date(Date.UTC(2026, 10, 14, 23, 50)).toISOString(),
  };
  const now = Date.UTC(2026, 10, 15, 0);
  it('counts from the first finished patch, not over the whole day', () => {
    const s = surveySummary(stats, now);
    expect(s).toMatchObject({ done: 8, failed: 1, total: 100, objects: 20000, candidates: 5, doneLastDay: 8 });
    expect(s.rate).toBeCloseTo(2); // 8 patches in the 4 hours since the first
    expect(s.fraction).toBeCloseTo(0.08);
    expect(s.classes[0]).toEqual({ key: 'unchecked', label: 'Not checked against catalogues', count: 3 });
  });
  it('has no rate when nothing finished in the last day', () => {
    expect(surveySummary({ ...stats, last_day: [hour(0, 0)] }, now).rate).toBeNull();
    expect(surveySummary(null)).toBeNull();
  });
  it('words the rate', () => {
    expect(rateText(2)).toBe('2.0 patches an hour');
    expect(rateText(1)).toBe('1.0 patch an hour');
    expect(rateText(14.4)).toBe('14 patches an hour');
    expect(rateText(null)).toBe('none finished in the last day');
  });
});

describe('partial or missing documents', () => {
  it('shape to empty results instead of throwing', () => {
    expect(causeRows(null)).toEqual([]);
    expect(confusionText(undefined)).toMatchObject({ still: null, moving: null, counts: { tp: 0, fp: 0, fn: 0, tn: 0 } });
    expect(injectionHeatmap({}).cells).toEqual([]);
    expect(spectralPoints(undefined)).toEqual([]);
    expect(falsePositiveText(undefined)).toBeNull();
    expect(surveySummary({ available: false })).toMatchObject({ available: false, total: 0, rate: null, classes: [] });
  });
});

describe('niceTicks', () => {
  it('steps by 1, 2 or 5 times a power of ten', () => {
    expect(niceTicks(0, 4200, 4)).toEqual([0, 2000, 4000]); // the step is at least (hi - lo) / n
    expect(niceTicks(0, 4000, 4)).toEqual([0, 1000, 2000, 3000, 4000]);
    expect(niceTicks(0, 37, 3)).toEqual([0, 20]);
    expect(niceTicks(0, 0.3, 3)).toEqual([0, 0.1, 0.2, 0.3]);
    expect(niceTicks(5, 5)).toEqual([5]);
  });
});

describe('precision and dates', () => {
  it('the fused fit is about 60 times more precise than SPHEREx alone', () => {
    expect(Math.round(precisionGain())).toBe(61);
  });
  it('writes generated_at in UTC', () => {
    expect(generatedText('2026-11-14T22:00:00Z')).toBe('14 Nov 2026, 22:00 UTC');
    expect(generatedText('nonsense')).toBeNull();
  });
});
