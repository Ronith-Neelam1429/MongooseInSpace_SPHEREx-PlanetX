import { describe, expect, it } from 'vitest';
import { baseline, detectorOf, perDetector, timeline, yearTicks } from './coverage.js';

const passes = [
  { start: '2025-08-15', end: '2025-10-01', n_visits: 3 },
  { start: '2026-03-11', end: '2026-05-05', n_visits: 2 },
];
const visits = [
  { date: '2025-08-15', detector: 1 },
  { date: '2025-08-16', detector: 3 },
  { date: '2025-10-01', detector: null, wavelength_min_um: 4.5, wavelength_max_um: 4.6 },
  { date: '2026-03-11', detector: 3 },
  { date: '2026-05-05', detector: null },
];
const NOW = new Date('2026-11-15T12:00:00Z');

describe('the detector of a visit', () => {
  it('uses the detector field, else the middle of its wavelengths', () => {
    expect(detectorOf({ detector: 4 })).toBe(4);
    expect(detectorOf({ wavelength_min_um: 0.8, wavelength_max_um: 0.82 })).toBe(1);
    expect(detectorOf({ wavelength_min_um: 2.0, wavelength_max_um: 2.05 })).toBe(3);
    expect(detectorOf({ wavelength_min_um: 4.9, wavelength_max_um: 4.95 })).toBe(6);
    expect(detectorOf({ detector: 9 })).toBeNull();
    expect(detectorOf({})).toBeNull();
  });

  it('counts images per detector, leaving out the unplaceable', () => {
    expect(perDetector(visits)).toEqual([1, 0, 2, 0, 0, 1]);
    expect(perDetector()).toEqual([0, 0, 0, 0, 0, 0]);
  });
});

describe('the coverage timeline', () => {
  it('runs from the survey start to today, binning visits by week', () => {
    const t = timeline(passes, visits, NOW);
    expect(t.start.toISOString().slice(0, 10)).toBe('2025-05-01');
    expect(t.end > NOW).toBe(true);
    expect(t.bins.reduce((s, b) => s + b.n, 0)).toBe(5);
    expect(t.max).toBe(2); // 15 and 16 Aug share a week
    for (const b of t.bins) {
      expect(b.x0).toBeGreaterThanOrEqual(0);
      expect(b.x1).toBeLessThanOrEqual(1);
      expect(b.x1).toBeGreaterThan(b.x0);
    }
    expect(t.today).toBeGreaterThan(0.95);
    expect(t.passes[0].x0).toBeLessThan(t.passes[1].x0);
  });

  it('falls back to one bin per pass without single visits', () => {
    const t = timeline(passes, [], NOW);
    expect(t.bins.map((b) => b.n)).toEqual([3, 2]);
    expect(t.max).toBe(3);
  });

  it('marks each new year', () => {
    expect(yearTicks(timeline(passes, [], NOW)).map((y) => y.label)).toEqual(['2026']);
  });
});

describe('the baseline', () => {
  it('spans first to last image, in months or years', () => {
    expect(baseline(passes)).toBe('9 mo');
    expect(baseline([passes[0], { start: '2027-01-01', end: '2027-03-01', n_visits: 1 }])).toBe('1.5 yr');
    expect(baseline([passes[0]])).toBeNull();
    expect(baseline()).toBeNull();
  });
});
