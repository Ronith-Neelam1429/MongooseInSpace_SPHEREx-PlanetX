import { describe, expect, it } from 'vitest';
import { engineTarget, featureText, knownTarget, progressText, spectrumChart } from './spectrum.js';

const engine = (over = {}) => ({
  kind: 'mover', ra: 133.76, dec: -7.24, ref_mjd: 61100, pm_ra_cosdec: -8.9, pm_dec: 0.6,
  archive: { verdict: 'spherex_only', pm_ra_cosdec: null, pm_dec: null }, ...over,
});

describe('engineTarget', () => {
  it('carries a mover\'s motion in mas/yr at its reference date', () => {
    const t = engineTarget(engine());
    expect(t).toMatchObject({ ra: 133.76, dec: -7.24, pmra: -8900, pmdec: 600 });
    expect(t.epoch).toBeCloseTo(2026.1615, 3);
  });

  it('prefers the archival refit when it confirmed the motion', () => {
    const t = engineTarget(engine({ archive: { verdict: 'confirmed', pm_ra_cosdec: -8.1, pm_dec: 0.7 } }));
    expect(t.pmra).toBeCloseTo(-8100);
    expect(t.pmdec).toBeCloseTo(700);
  });

  it('leaves a static object fixed: its slope is noise', () => {
    expect(engineTarget(engine({ kind: 'static' }))).toEqual({ ra: 133.76, dec: -7.24 });
  });
});

describe('knownTarget', () => {
  it('uses Gaia at epoch 2016 when it lists a motion', () => {
    const row = { ra: 1, dec: 2, gaia: { ra: 269.4485, dec: 4.7394, pmra: -801.6, pmdec: 10362.4 }, simbad: null };
    expect(knownTarget(row, 2026.9)).toEqual({ ra: 269.4485, dec: 4.7394, pmra: -801.6, pmdec: 10362.4, epoch: 2016 });
  });

  it('keeps an object without a motion where it is', () => {
    expect(knownTarget({ ra: 10, dec: 20, gaia: null, simbad: null }, 2026.9)).toEqual({ ra: 10, dec: 20, pmra: 0, pmdec: 0, epoch: 2026.9 });
  });
});

const result = {
  bands: [
    { wavelength_um: 1.25, lo_um: 1.24, hi_um: 1.26, flux_mjy: 40, flux_err_mjy: 1, n_used: 2 },
    { wavelength_um: 3.3, lo_um: 3.25, hi_um: 3.35, flux_mjy: -0.5, flux_err_mjy: 1, n_used: 1 },
  ],
  points: [{ wavelength_um: 1.25, flux_mjy: 300, flux_err_mjy: 1, outlier: true }],
  features: [{ key: 'ch4_3.3', molecule: 'CH4', label: 'Methane 3.3 µm', lo_um: 3.2, hi_um: 3.45, detected: true }],
};

describe('spectrumChart', () => {
  it('scales y to the bands, not to an outlying single exposure', () => {
    const m = spectrumChart(result);
    expect(m.y[1]).toBeGreaterThan(41);
    expect(m.y[1]).toBeLessThan(50);
    expect(m.y[0]).toBeLessThan(-1.5); // a band below zero stays in view
    expect(m.points[0].outlier).toBe(true);
    expect(m.features[0]).toMatchObject({ molecule: 'methane', lo: 3.2, hi: 3.45, detected: true });
  });

  it('copes with no bands', () => {
    const m = spectrumChart({ bands: [], points: [], features: [] });
    expect(m.y).toEqual([0, 1.08]);
  });
});

describe('featureText', () => {
  it('describes dips, rises and missing data', () => {
    expect(featureText({ depth: 0.77, depth_err: 0.07, significance: 11 })).toBe('77% ± 7% below the continuum (11.0σ)');
    expect(featureText({ depth: -0.03, depth_err: 0.02, significance: -1.5 })).toMatch(/^no dip: 3% ± 2% above/);
    expect(featureText({ depth: null })).toMatch(/not measured/);
  });
});

describe('progressText', () => {
  it('says what the server is doing', () => {
    expect(progressText(null)).toMatch(/Waiting/);
    expect(progressText({ stage: 'listing', frames_total: 0 })).toMatch(/Listing/);
    expect(progressText({ stage: 'measuring', frames_done: 3, frames_total: 72 })).toBe('Measuring exposure 3 of 72…');
  });
});
