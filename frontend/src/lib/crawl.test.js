import { describe, expect, it, vi } from 'vitest';
import { COVERAGE_LAYERS, candidateField, candidateMotion, foundDate, matchCandidate, slots, surveyProgress } from './crawl.js';

const counts = (done, failed = 0, pending = 0) => ({ pending, in_progress: 0, done, failed });

describe('surveyProgress', () => {
  it('says how many patches are searched, the failures and how far out the crawl is', () => {
    expect(surveyProgress({ total: 49152, counts: counts(312, 0, 48840), current_radius_deg: 6.04 }))
      .toBe('312 of 49,152 patches searched · now 6.0° from the north ecliptic pole');
    expect(surveyProgress({ total: 49152, counts: counts(312, 4, 48836), current_radius_deg: 6 }))
      .toBe('312 of 49,152 patches searched · 4 failed · now 6.0° from the north ecliptic pole');
  });

  it('says when the survey has not started or has finished', () => {
    expect(surveyProgress({ total: 0, counts: counts(0) })).toBe('Our automatic survey has not started yet.');
    expect(surveyProgress(null)).toBe('Our automatic survey has not started yet.');
    expect(surveyProgress({ total: 12, counts: counts(11, 1), current_radius_deg: null })).toBe('11 of 12 patches searched · 1 failed · finished');
  });
});

describe('candidateMotion', () => {
  const c = { pm_total: 8.9, significance: 3.2, position_angle_deg: 284 };

  it('puts speed, direction and how sure in words', () => {
    expect(candidateMotion(c)).toBe('8.90″ a year toward the west · tentative (3.2σ)');
    expect(candidateMotion({ ...c, significance: 12.04, position_angle_deg: 2 })).toBe('8.90″ a year toward the north · clear detection (12.0σ)');
  });

  it('says nothing for a motion within its error (a brightness variable)', () => {
    expect(candidateMotion({ ...c, significance: 1.4 })).toBeNull();
    expect(candidateMotion({ ...c, pm_total: 0 })).toBeNull();
  });
});

describe('foundDate', () => {
  it('formats the API date in UTC, whatever the local zone', () => {
    expect(foundDate('2026-11-14')).toBe('14 Nov 2026');
    expect(foundDate('2026-01-01')).toBe('1 Jan 2026');
  });

  it('passes anything else through', () => {
    expect(foundDate('yesterday')).toBe('yesterday');
    expect(foundDate(undefined)).toBe('');
  });
});

describe('candidateField', () => {
  it('is the field the survey measured, or the candidate itself without one', () => {
    expect(candidateField({ ra: 1, dec: 2, field_ra: 1.1, field_dec: 2.1 })).toEqual({ ra: 1.1, dec: 2.1 });
    expect(candidateField({ ra: 1, dec: 2 })).toEqual({ ra: 1, dec: 2 });
  });
});

describe('matchCandidate', () => {
  const c = { ra: 248.0, dec: 66.0 };
  const at = (id, dArcsec) => ({ id, ra: 248.0, dec: 66.0 + dArcsec / 3600 });

  it('takes the nearest object within one SPHEREx pixel', () => {
    expect(matchCandidate([at(1, 5), at(2, 0.4), at(3, 30)], c).id).toBe(2);
    expect(matchCandidate([at(1, 5.9)], c).id).toBe(1);
  });

  it('finds nothing when the run no longer has it', () => {
    expect(matchCandidate([at(1, 7), at(2, 60)], c)).toBeNull();
    expect(matchCandidate([], c)).toBeNull();
    expect(matchCandidate(undefined, c)).toBeNull();
  });
});

describe('slots', () => {
  it('runs at most `limit` tasks at once, in order, and skips cancelled ones', () => {
    const take = slots(2);
    const started = [];
    const releases = {};
    const task = (id) => (release) => { started.push(id); releases[id] = release; };
    take(task('a'));
    take(task('b'));
    take(task('c'));
    const cancelD = take(task('d'));
    take(task('e'));
    expect(started).toEqual(['a', 'b']);
    cancelD();
    releases.a();
    releases.a(); // a second release of the same task frees nothing more
    expect(started).toEqual(['a', 'b', 'c']);
    releases.b();
    expect(started).toEqual(['a', 'b', 'c', 'e']);
  });

  it('cancelling a started task does nothing', () => {
    const take = slots(1);
    const run = vi.fn();
    const cancel = take(run);
    cancel();
    expect(run).toHaveBeenCalledTimes(1);
  });
});

describe('COVERAGE_LAYERS', () => {
  it('draws the queued outlines first and every status once', () => {
    expect(COVERAGE_LAYERS.map((l) => l.key)).toEqual(['queued', 'done', 'failed']);
    for (const l of COVERAGE_LAYERS) expect(l.style.color).toMatch(/^#[0-9a-f]{6}$/);
  });
});
