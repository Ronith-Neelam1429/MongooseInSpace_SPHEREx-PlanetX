import { describe, expect, it } from 'vitest';
import { failPage, mergePage } from './candidatesList.js';

const row = (id) => ({ id, classification: 'unknown_candidate' });
const shown = (filter, ids, total = 10) => ({ status: 'ok', filter, total, candidates: ids.map(row), loadingMore: true });

describe('mergePage', () => {
  it('appends the next page for the filter it was asked for', () => {
    const next = mergePage(shown('', [1, 2]), { total: 10, candidates: [row(3)] }, '');
    expect(next.candidates.map((c) => c.id)).toEqual([1, 2, 3]);
    expect(next.loadingMore).toBeUndefined();
    expect(next.filter).toBe('');
  });

  it('drops a page that arrives after the filter changed', () => {
    // "Show more" on All, then the filter switched to Unknown (now loading, then shown).
    const loading = { status: 'loading' };
    expect(mergePage(loading, { total: 10, candidates: [row(3)] }, '')).toBe(loading);
    const unknown = shown('unknown_candidate', [7]);
    expect(mergePage(unknown, { total: 10, candidates: [row(3)] }, '')).toBe(unknown);
  });

  it('skips rows already shown when new rows shifted the pages', () => {
    const next = mergePage(shown('', [5, 4]), { total: 11, candidates: [row(4), row(3)] }, '');
    expect(next.candidates.map((c) => c.id)).toEqual([5, 4, 3]);
    expect(next.total).toBe(11);
  });

  it('stops loading when the route has gone', () => {
    expect(mergePage(shown('', [1]), null, '')).toMatchObject({ status: 'ok', loadingMore: false });
  });
});

describe('failPage', () => {
  it('shows the error for the current filter only', () => {
    expect(failPage(shown('', [1]), 'HTTP 502', '')).toEqual({ status: 'error', error: 'HTTP 502' });
    const unknown = shown('unknown_candidate', [7]);
    expect(failPage(unknown, 'HTTP 502', '')).toBe(unknown);
  });
});
