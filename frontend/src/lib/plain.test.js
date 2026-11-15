import { describe, expect, it } from 'vitest';
import {
  archiveSentence, claim, classLabel, compass, confidence, dateSpan, distanceText, fakeMotionLine, lightYears, measuredMotion, moonCrossing,
  motionsAgree, motionVerdict, positionAngle, shortClaim, shortName, speed, VERDICT_HINT, verdict,
} from './plain.js';

const object = (over = {}) => ({
  kind: 'mover', is_variable: false, pm_total: 9.91, pm_err: 1.95, significance: 5.08, pm_ra_cosdec: 0.42, pm_dec: 9.9,
  position_angle_deg: 2.4, variability: null, classification: null,
  archive: { verdict: 'unconfirmed', pm_ra_cosdec: 0.4, pm_dec: 9.9, pm_err: 1.9, baseline_yr: 1, observatories: ['spherex'] },
  ...over,
});
const variable = object({ kind: 'static', is_variable: true, variability: { ratio: 1.786 } });

describe('directions and sizes', () => {
  it('turns a position angle into a compass word', () => {
    expect([0, 2.4, 44, 90, 158, 180, 225, 284, 340, 358].map(compass))
      .toEqual(['north', 'north', 'north-east', 'east', 'south', 'south', 'south-west', 'west', 'north', 'north']);
    expect(compass(-90)).toBe('west');
  });
  it('gets the position angle of east and north components', () => {
    expect(positionAngle(0, 1)).toBeCloseTo(0);
    expect(positionAngle(1, 0)).toBeCloseTo(90);
    expect(positionAngle(-1.29, -1.53)).toBeCloseTo(220.1, 1);
  });
  it('writes a motion with digits that suit its size', () => {
    expect([10.393, 9.9125, 0.0558, 0.00524].map(speed)).toEqual(['10.39', '9.91', '0.056', '0.0052']);
    expect(speed(null)).toBe('–');
  });
  it('says how long a motion takes to cross the full Moon', () => {
    expect(moonCrossing(10.39)).toBe('about 180 years');
    expect(moonCrossing(0.056)).toBe('about 33,000 years');
    expect(moonCrossing(0.0005)).toBe('about 3.7 million years');
    expect(moonCrossing(0)).toBeNull();
  });
  it('gives a distance only for a well-measured parallax', () => {
    expect(lightYears(546.9759, 0.0401)).toBeCloseTo(5.963, 3);
    expect(lightYears(1.2, 0.9)).toBeNull(); // noise
    expect(lightYears(-0.3, 0.1)).toBeNull();
    expect(lightYears(5, null)).toBeNull();
    expect(distanceText(5.963)).toBe('about 6.0 light-years away');
    expect(distanceText(1630)).toBe('about 1,600 light-years away');
  });
});

describe('measuredMotion', () => {
  it('quotes SPHEREx alone unless the archive confirms the motion', () => {
    expect(measuredMotion(object())).toEqual({ pm: 9.91, sigma: 5.08, err: 1.95, east: 0.42, north: 9.9, pa: 2.4, fused: false });
  });
  it('quotes the archival refit, with its own direction, for a confirmed mover', () => {
    const m = measuredMotion(object({ archive: { verdict: 'confirmed', pm_ra_cosdec: -0.8, pm_dec: 10.36, pm_err: 0.03 } }));
    expect(m.fused).toBe(true);
    expect(m.pm).toBeCloseTo(10.391, 3);
    expect(m.sigma).toBeCloseTo(346.4, 1);
    expect(m.pa).toBeCloseTo(355.6, 1);
  });
  it('falls back to SPHEREx when the refit has no usable error', () => {
    expect(measuredMotion(object({ archive: { verdict: 'confirmed', pm_ra_cosdec: 1, pm_dec: 1, pm_err: null } })).fused).toBe(false);
  });
});

describe('wording', () => {
  it('describes movers, candidates and variables in words', () => {
    expect(claim(object())).toBe('Moves 9.91″ per year, toward north');
    expect(shortClaim(object({ kind: 'candidate', pm_total: 7.78, position_angle_deg: 40.4 }))).toBe('7.78″/yr north-east');
    expect(claim(variable)).toBe('Brightness changed 1.79× between passes');
    expect(shortClaim(variable)).toBe('brightness ×1.79');
    expect(claim(object({ is_variable: true, variability: { ratio: 2 } }))).toBe('Moves 9.91″ per year, toward north; brightness changed 2.00× between passes');
  });
  it("gives the engine's own class until the backend has classified the object", () => {
    expect(verdict(object())).toBe('Moving');
    expect(verdict(object({ kind: 'candidate' }))).toBe('Candidate');
    expect(verdict(variable)).toBe('Changes brightness');
    expect(verdict(object({ classification: 'known_star' }))).toBe('Known star');
    expect(verdict(object({ kind: 'candidate', classification: 'unknown_candidate' }))).toBe('Unknown candidate');
    expect(verdict(object({ classification: 'artifact' }))).toBe('Likely artifact');
  });
  it('never calls anything a discovery', () => {
    const classes = ['known_star', 'known_asteroid', 'variable_star', 'artifact', 'unknown_candidate', 'static', null, 'something_new'];
    for (const c of classes) expect(classLabel(c)).not.toMatch(/discover/i);
    expect(classLabel(null)).toBe('Candidate');
  });
  it('grades a detection by its size over its error', () => {
    expect([28, 5, 4.9, 3, 1.4].map(confidence))
      .toEqual(['Clear detection', 'Clear detection', 'Tentative', 'Tentative', 'Within the measurement error']);
  });
  it('explains what older surveys found', () => {
    const a = (verdictName, observatories = ['spherex', 'allwise', 'catwise'], baseline = 16.2) => archiveSentence({ verdict: verdictName, observatories, baseline_yr: baseline });
    expect(a('confirmed')).toBe('Confirmed: AllWISE, CatWISE saw it somewhere else, so it has kept moving over 16 years.');
    expect(a('stationary', ['spherex', '2mass'], 26.4)).toBe('Not confirmed: 2MASS saw it in the same place over 26 years, so it is probably not moving.');
    expect(a('unconfirmed', ['spherex'], 1)).toMatch(/too little/);
    // One survey family alone (AllWISE + CatWISE share exposures) can only make it a candidate.
    expect(archiveSentence({ verdict: 'unconfirmed', candidate: true, observatories: ['spherex', 'allwise', 'catwise'], baseline_yr: 16.2 }))
      .toBe('Candidate: AllWISE, CatWISE positions alone show it moving over 16 years, but a second, independent survey is needed to confirm it.');
    expect(archiveSentence(null)).toBeNull();
  });
  it('compares our motion with a catalogued one', () => {
    const gaia = { east: -0.80, north: 10.36 };
    // SPHEREx alone on Barnard's Star: 1.3"/yr off, well inside its 1.95"/yr error.
    expect(motionsAgree({ east: 0.42, north: 9.9, err: 1.95 }, gaia)).toBe(true);
    // A precise archival refit that differs by 1% is still the same motion.
    expect(motionsAgree({ east: -0.82, north: 10.26, err: 0.03 }, gaia)).toBe(true);
    // 2"/yr claimed for a star the catalogue has standing still.
    expect(motionsAgree({ east: -1.29, north: -1.53, err: 0.07 }, { east: 0.002, north: -0.004 })).toBe(false);
  });
  it('writes date spans and short names', () => {
    expect(dateSpan('2025-08-15', '2026-09-22')).toBe('Aug 2025 – Sep 2026');
    expect(dateSpan('2025-08-15', '2025-10-01')).toBe('Aug – Oct 2025');
    expect(dateSpan('2026-03-11', '2026-03-30')).toBe('Mar 2026');
    expect(shortName('Gaia DR3 4472832130942575872')).toBe('Gaia DR3 …575872');
    expect(shortName("Barnard's star")).toBe("Barnard's star");
    expect(shortName(null)).toBeNull();
  });
});

describe('motion verdict', () => {
  const mv = (verdict, extra = {}) => ({ verdict, certainty: null, cause: null, reason: 'Because.', decided_by: 'archive', ...extra });

  it('says whether the motion is real, how sure, and why', () => {
    expect(motionVerdict(object({ motion_verdict: mv('moving', { certainty: 'confirmed' }) }))).toMatchObject({
      verdict: 'moving', label: 'Moving: confirmed', tone: 'moving', reason: 'Because.', decidedBy: 'older sky surveys',
    });
    expect(motionVerdict(object({ motion_verdict: mv('moving', { certainty: 'candidate' }) })).label).toBe('Moving: candidate');
    const fake = motionVerdict(object({ motion_verdict: mv('fake_motion', { cause: 'chance_link', decided_by: 'catalogue' }) }));
    expect(fake).toMatchObject({ label: 'Fake motion', tone: 'fake', cause: 'chance_link', causeText: 'different objects joined into one track' });
    expect(motionVerdict(object({ motion_verdict: mv('unsure', { cause: 'blend' }) }))).toMatchObject({ tone: 'unsure', causeText: 'two sources blended into one blur' });
    expect(motionVerdict(object({ motion_verdict: mv('not_moving') })).tone).toBe('still');
  });

  it('is null for older servers and unknown verdicts', () => {
    expect(motionVerdict(object())).toBeNull();
    expect(motionVerdict(object({ motion_verdict: mv('teleported') }))).toBeNull();
    expect(motionVerdict(null)).toBeNull();
  });

  it('replaces the yes/no label, keeping catalogue names and brightness changes', () => {
    expect(verdict(object({ motion_verdict: mv('fake_motion', { cause: 'blend' }) }))).toBe('Fake motion');
    expect(verdict(object({ motion_verdict: mv('unsure') }))).toBe('Unsure');
    expect(verdict(object({ classification: 'known_star', motion_verdict: mv('moving', { certainty: 'confirmed' }) }))).toBe('Known star, moving');
    expect(verdict(object({ classification: 'known_asteroid', motion_verdict: mv('moving', { certainty: 'confirmed' }) }))).toBe('Known asteroid');
    expect(verdict({ ...variable, motion_verdict: mv('not_moving') })).toBe('Changes brightness');
    expect(verdict(object({ kind: 'static', classification: 'variable_star', motion_verdict: mv('not_moving') }))).toBe('Variable star');
  });

  it('does not quote a fake motion as a speed', () => {
    const fake = object({ motion_verdict: mv('fake_motion', { cause: 'saturated_halo' }) });
    expect(claim(fake)).toBe('Seems to move 9.91″ per year, but the motion is not real');
    expect(shortClaim(fake)).toBe('9.91″/yr north (fake)');
  });

  it('does not quote a survey candidate’s fake motion as a detection', () => {
    const row = { pm_total: 4.03, significance: 6.2, is_variable: false };
    expect(fakeMotionLine({ ...row, motion_verdict: mv('fake_motion', { cause: 'blend' }) })).toBe('Apparent 4.03″ a year, not real motion');
    expect(fakeMotionLine({ ...row, motion_verdict: mv('unsure') })).toBeNull();
    expect(fakeMotionLine(row)).toBeNull();
  });

  it('never calls anything a discovery', () => {
    for (const v of ['moving', 'not_moving', 'fake_motion', 'unsure']) {
      for (const certainty of ['confirmed', 'candidate', null]) {
        expect(motionVerdict(object({ motion_verdict: mv(v, { certainty }) })).label).not.toMatch(/discover/i);
      }
    }
    expect(Object.values(VERDICT_HINT).join(' ')).not.toMatch(/discover/i);
  });
});
