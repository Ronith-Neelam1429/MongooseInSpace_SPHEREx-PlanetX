import { afterEach, describe, expect, it, vi } from 'vitest';
import { engineLabel, measuredMotion } from './starfield.js';

const object = (over = {}) => ({
  kind: 'mover', is_variable: false, pm_total: 9.91, significance: 5.1, variability: null,
  archive: { verdict: 'unconfirmed', pm_ra_cosdec: 0.4, pm_dec: 9.9, pm_err: 1.9, observatories: ['spherex'] },
  ...over,
});

describe('measuredMotion', () => {
  it('quotes SPHEREx alone unless the archive confirms the motion', () => {
    expect(measuredMotion(object())).toEqual({ pm: 9.91, sigma: 5.1, fused: false });
  });
  it('quotes the archival refit for a confirmed mover', () => {
    const m = measuredMotion(object({ archive: { verdict: 'confirmed', pm_ra_cosdec: -0.8, pm_dec: 10.36, pm_err: 0.03 } }));
    expect(m.fused).toBe(true);
    expect(m.pm).toBeCloseTo(10.391, 3);
    expect(m.sigma).toBeCloseTo(346.4, 1);
  });
  it('falls back to SPHEREx when the refit has no usable error', () => {
    expect(measuredMotion(object({ archive: { verdict: 'confirmed', pm_ra_cosdec: 1, pm_dec: 1, pm_err: null } })).fused).toBe(false);
  });
});

describe('engineLabel', () => {
  it('describes movers, candidates and variables', () => {
    expect(engineLabel(object())).toBe('SPHEREx mover  µ 9.91″/yr  5σ');
    expect(engineLabel(object({ kind: 'candidate', pm_total: 7.78, significance: 3.46 }))).toBe('SPHEREx candidate  µ 7.78″/yr  3σ');
    expect(engineLabel(object({ kind: 'static', is_variable: true, variability: { ratio: 1.786 } }))).toBe('SPHEREx variable  ×1.79');
  });
});

describe('runMotion', () => {
  afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.useRealTimers(); vi.resetModules(); });

  async function load(responses) {
    vi.stubEnv('VITE_API_BASE', 'http://api.test/api/');
    vi.resetModules();
    const fetchMock = vi.fn(async () => {
      const r = responses.shift();
      return { ok: r.status === 200, status: r.status, json: async () => r.body };
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.useFakeTimers();
    const { runMotion } = await import('./services.js');
    return { runMotion, fetchMock };
  }
  const running = (done) => ({ status: 200, body: { status: 'running', stage: 'downloading', frames_done: done, frames_total: 54 } });

  it('polls until the run is done and reports progress', async () => {
    const result = { objects: [] };
    const { runMotion, fetchMock } = await load([running(0), running(30), { status: 200, body: { status: 'done', ra: 269.448, dec: 4.739, size_deg: 0.1, result } }]);
    const seen = [];
    const p = runMotion(269.4485, 4.7394, 0.1, { onProgress: (j) => seen.push(j.frames_done) });
    await vi.runAllTimersAsync();
    expect(await p).toEqual({ objects: [], field: { ra: 269.448, dec: 4.739, size_deg: 0.1 } });
    expect(seen).toEqual([0, 30]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[0][0]).toBe('http://api.test/api/motion?ra=269.448500&dec=4.739400&size_deg=0.1000');
  });

  it('rejects with the backend reason when the run fails', async () => {
    const { runMotion } = await load([{ status: 200, body: { status: 'failed', error: 'IRSA request failed' } }]);
    await expect(runMotion(1, 2, 0.1)).rejects.toThrow('IRSA request failed');
  });

  it('rejects when the engine queue is full', async () => {
    const { runMotion } = await load([{ status: 503, body: {} }]);
    await expect(runMotion(1, 2, 0.1)).rejects.toThrow(/busy/);
  });

  it('stops polling when aborted', async () => {
    const { runMotion, fetchMock } = await load([running(0), running(1)]);
    const ctrl = new AbortController();
    const p = runMotion(1, 2, 0.1, { signal: ctrl.signal });
    const caught = p.catch((e) => e);
    await vi.advanceTimersByTimeAsync(100);
    ctrl.abort(new DOMException('gone', 'AbortError'));
    await vi.runAllTimersAsync();
    expect((await caught).name).toBe('AbortError');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
