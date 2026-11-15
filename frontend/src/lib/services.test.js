import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.useRealTimers(); vi.resetModules(); });

/** services.js against a fake PlanetX API that answers with `responses` in order ({ status, body }). */
async function load(responses, { apiBase = 'http://api.test/api/' } = {}) {
  vi.stubEnv('VITE_API_BASE', apiBase);
  vi.resetModules();
  const fetchMock = vi.fn(async () => {
    const r = responses.shift();
    return { ok: r.status === 200, status: r.status, json: async () => r.body };
  });
  vi.stubGlobal('fetch', fetchMock);
  return { services: await import('./services.js'), fetchMock };
}
const ok = (body) => ({ status: 200, body });

describe('runMotion', () => {
  const running = (done) => ok({ status: 'running', stage: 'downloading', frames_done: done, frames_total: 54 });

  it('polls until the run is done and reports progress', async () => {
    const result = { objects: [] };
    const { services, fetchMock } = await load([running(0), running(30), ok({ status: 'done', ra: 269.448, dec: 4.739, size_deg: 0.1, result })]);
    vi.useFakeTimers();
    const seen = [];
    const p = services.runMotion(269.4485, 4.7394, 0.1, { onProgress: (j) => seen.push(j.frames_done) });
    await vi.runAllTimersAsync();
    expect(await p).toEqual({ objects: [], field: { ra: 269.448, dec: 4.739, size_deg: 0.1 } });
    expect(seen).toEqual([0, 30]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[0][0]).toBe('http://api.test/api/motion?ra=269.448500&dec=4.739400&size_deg=0.1000');
  });

  it('rejects with the backend reason when the run fails', async () => {
    const { services } = await load([ok({ status: 'failed', error: 'IRSA request failed' })]);
    await expect(services.runMotion(1, 2, 0.1)).rejects.toThrow('IRSA request failed');
  });

  it('rejects when the engine queue is full', async () => {
    const { services } = await load([{ status: 503, body: {} }]);
    await expect(services.runMotion(1, 2, 0.1)).rejects.toThrow(/busy/);
  });

  it('stops polling when aborted', async () => {
    const { services, fetchMock } = await load([running(0), running(1)]);
    vi.useFakeTimers();
    const ctrl = new AbortController();
    const p = services.runMotion(1, 2, 0.1, { signal: ctrl.signal });
    const caught = p.catch((e) => e);
    await vi.advanceTimersByTimeAsync(100);
    ctrl.abort(new DOMException('gone', 'AbortError'));
    await vi.runAllTimersAsync();
    expect((await caught).name).toBe('AbortError');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('runSpectrum', () => {
  it('polls until done and sends the motion only for a moving object', async () => {
    const result = { bands: [] };
    const { services, fetchMock } = await load([
      ok({ status: 'running', stage: 'measuring', frames_done: 1, frames_total: 72 }), ok({ status: 'done', result }),
    ]);
    vi.useFakeTimers();
    const seen = [];
    const p = services.runSpectrum({ ra: 331.044, dec: -56.783, pmra: 3982, pmdec: -2466.8, epoch: 2000 }, { onProgress: (j) => seen.push(j.frames_done) });
    await vi.runAllTimersAsync();
    expect(await p).toBe(result);
    expect(seen).toEqual([1]);
    expect(fetchMock.mock.calls[0][0]).toBe('http://api.test/api/spectrum?ra=331.044000&dec=-56.783000&pmra=3982.0&pmdec=-2466.8&epoch=2000.000');
  });

  it('leaves the motion out for a fixed object', async () => {
    const { services, fetchMock } = await load([ok({ status: 'done', result: {} })]);
    await services.runSpectrum({ ra: 10, dec: 20, pmra: 0, pmdec: 0, epoch: 2026 });
    expect(fetchMock.mock.calls[0][0]).toBe('http://api.test/api/spectrum?ra=10.000000&dec=20.000000');
  });

  it('rejects with the backend reason, or when busy', async () => {
    let { services } = await load([ok({ status: 'failed', error: 'IRSA is down' })]);
    await expect(services.runSpectrum({ ra: 1, dec: 2 })).rejects.toThrow('IRSA is down');
    ({ services } = await load([{ status: 503, body: {} }]));
    await expect(services.runSpectrum({ ra: 1, dec: 2 })).rejects.toThrow(/busy/);
  });
});

describe('fetchVisits', () => {
  it('summarises the visits as a count and the survey passes', async () => {
    const passes = [{ start: '2025-08-15', end: '2025-10-01', n_visits: 118 }, { start: '2026-03-11', end: '2026-05-05', n_visits: 122 }];
    const { services, fetchMock } = await load([ok({ n_visits: 240, passes, visits: [{ date: '2025-08-15' }] })]);
    expect(await services.fetchVisits(269.4485, 4.7394)).toEqual({ count: 240, passes });
    expect(fetchMock.mock.calls[0][0]).toBe('http://api.test/api/visits?ra=269.448500&dec=4.739400');
  });

  it('rejects a response without passes', async () => {
    const { services } = await load([ok({ visits: [] })]);
    await expect(services.fetchVisits(1, 2)).rejects.toThrow('bad response');
  });
});

describe('fetchCandidates', () => {
  // One row of GET /api/candidates as the API contract defines it.
  const candidate = {
    id: 7, ra: 133.79, dec: -7.24, name: null, classification: 'unknown_candidate', is_variable: false, pm_total: 8.9,
    pm_ra_cosdec: -8.6, pm_dec: 2.1, significance: 3.2, position_angle_deg: 284, score: 0.82, nside: 64, ipix: 1234, found: '2026-11-12',
  };

  it('asks for a page and returns the total with the rows', async () => {
    const { services, fetchMock } = await load([ok({ total: 31, candidates: [candidate] })]);
    expect(await services.fetchCandidates()).toEqual({ total: 31, candidates: [candidate] });
    expect(fetchMock.mock.calls[0][0]).toBe('http://api.test/api/candidates?limit=100&offset=0');
  });

  it('passes paging and the classification filter', async () => {
    const { services, fetchMock } = await load([ok({ total: 0, candidates: [] })]);
    expect(await services.fetchCandidates({ limit: 50, offset: 100, classification: 'unknown_candidate' })).toEqual({ total: 0, candidates: [] });
    expect(fetchMock.mock.calls[0][0]).toBe('http://api.test/api/candidates?limit=50&offset=100&classification=unknown_candidate');
  });

  it('reports the list as unavailable when the server has no such route, or there is no server', async () => {
    const { services } = await load([{ status: 404, body: { detail: 'Not Found' } }]);
    expect(await services.fetchCandidates()).toBeNull();
    const none = await load([], { apiBase: '' });
    expect(await none.services.fetchCandidates()).toBeNull();
    expect(none.fetchMock).not.toHaveBeenCalled();
  });

  it('rejects on a server error or a malformed body', async () => {
    const { services } = await load([{ status: 500, body: {} }, ok({ total: 3 })]);
    await expect(services.fetchCandidates()).rejects.toThrow('HTTP 500');
    await expect(services.fetchCandidates()).rejects.toThrow('bad response');
  });
});

describe('fetchSurvey', () => {
  const status = { nside: 64, moc_order: 6, total: 49152, counts: { pending: 49100, in_progress: 1, done: 50, failed: 1 }, current_radius_deg: 3.2 };
  const idle = { nside: null, moc_order: null, total: 0, counts: { pending: 0, in_progress: 0, done: 0, failed: 0 } };
  // The two requests go out together; answer each by its URL.
  async function survey(byPath) {
    const { services, fetchMock } = await load([]);
    fetchMock.mockImplementation(async (url) => ({ ok: true, status: 200, json: async () => byPath[new URL(url).pathname] }));
    return services.fetchSurvey();
  }

  it('returns the status and the finished patches as a MOC', async () => {
    const coverage = { 6: [1, 2, 3] };
    expect(await survey({ '/api/survey/status': status, '/api/survey/coverage': coverage })).toEqual({ status, coverage });
  });

  it('has no coverage to draw before the first patch is finished', async () => {
    expect(await survey({ '/api/survey/status': idle, '/api/survey/coverage': { 6: [] } })).toEqual({ status: idle, coverage: null });
  });

  it('is unavailable without a server', async () => {
    const { services, fetchMock } = await load([], { apiBase: '' });
    expect(await services.fetchSurvey()).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
