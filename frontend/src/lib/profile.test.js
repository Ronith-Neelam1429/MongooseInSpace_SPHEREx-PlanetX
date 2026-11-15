import { describe, expect, it } from 'vitest';
import barnard from './__fixtures__/profile-barnard.json';
import {
  approachModel, colourWord, discModel, distanceView, engineProfileParams, gravityView, identityLine, knownProfileParams, lyText,
  moonTripText, pathModel, profileQuery, researcherGroups, safeLinks, scaleBar, sizePhrase, sizeView, skyOffset, speedView,
  temperatureView, whyList, yearText,
} from './profile.js';

const NOW = 2026.9;
const empty = { ra: 10, dec: 20, identity: null, distance: null, physical: null, motion: null, path: null, why_moving: [], gravity: null, links: [], sources: [], notes: [] };

describe('number wording', () => {
  it('formats light-years by size', () => {
    expect(lyText(5.96)).toBe('6.0');
    expect(lyText(42.4)).toBe('42');
    expect(lyText(326)).toBe('330');
    expect(lyText(25300)).toBe('25,000');
    expect(lyText(null)).toBeNull();
  });

  it('formats years, BCE for negative ones', () => {
    expect(yearText(2020)).toBe('2020');
    expect(yearText(11800)).toBe('11,800');
    expect(yearText(-8000)).toBe('8,000 BCE');
  });

  it('says how long a trip to the Moon takes', () => {
    expect(moonTripText(142.6)).toBe('45 minutes');
    expect(moonTripText(5000)).toBe('77 seconds');
    expect(moonTripText(2)).toBe('2 days');
    expect(moonTripText(0)).toBeNull();
  });
});

describe('the Barnard profile (contract example)', () => {
  it('gives the distance and when the light left', () => {
    const d = distanceView(barnard);
    expect(d).toMatchObject({ value: '6.0', unit: 'ly', sub: '1.8 pc', phrase: 'About 6.0 light-years away.' });
    expect(d.light).toBe('The light you see left it in 2020.');
  });

  it('compares its size with the Sun and Jupiter', () => {
    const s = sizeView(barnard);
    expect(s.value).toBe('×0.19');
    expect(s.phrase).toBe("About 1/5 the Sun's width — 2 Jupiters across");
    expect(s.estimate).toBe(false);
  });

  it('describes its temperature and colour', () => {
    expect(temperatureView(barnard)).toMatchObject({
      value: '3,200', unit: 'K', sub: 'orange-red', color: '#ffb46b',
      phrase: "Cooler than the Sun's surface (5,772 K), so it glows orange-red.",
    });
  });

  it('gives its speed against everyday references', () => {
    const v = speedView(barnard);
    expect(v).toMatchObject({ value: '143', unit: 'km/s', sub: 'vs the Sun' });
    expect(v.phrase).toBe("It moves 143 km/s relative to the Sun: about 4.8 times Earth's speed around the Sun, fast enough to cross the distance to the Moon in 45 minutes.");
  });

  it('lists why it moves, in order', () => {
    expect(whyList(barnard).map((w) => w.factor)).toEqual(['closeness', 'speed', 'population', 'parallax']);
  });

  it('draws its sky track centred on today, heading north', () => {
    const m = pathModel(barnard, NOW);
    expect(m.points).toHaveLength(41);
    expect(m.span).toBe(20000);
    // Today's position sits close to the centre (the profile's own position).
    expect(Math.hypot(m.now.x, m.now.y)).toBeLessThan(0.1);
    // It moves north: the past lies south, the future north, and over 20,000 years about 70°.
    expect(m.start.y).toBeLessThan(-15);
    expect(m.end.y).toBeGreaterThan(45);
    expect(m.extent).toBeGreaterThan(60);
    expect(m.scale).toEqual({ deg: 10, label: '10°' });
  });

  it('finds the closest approach', () => {
    const a = approachModel(barnard, NOW);
    expect(a.closest).toEqual({ year: 11800, ly: 3.8 });
    expect(a.now).toEqual({ year: NOW, ly: 5.96 });
    expect(a.phrase).toBe('It will pass closest to the Sun around 11,800, at about 3.8 light-years.');
    expect(a.inSpan).toBe(true);
  });

  it('only words a closest approach far outside the track', () => {
    const far = { ...barnard, path: { ...barnard.path, closest_approach: { year: -205000, dist_ly: 2.1 } } };
    const a = approachModel(far, NOW);
    expect(a).toMatchObject({ inSpan: false, points: null, closest: { year: -205000, ly: 2.1 } });
    expect(a.phrase).toBe('Straight-line motion would bring it closest to the Sun around 205,000 BCE, at about 2.1 light-years, '
      + 'far outside this view; such long extrapolations are rough.');
    // The backend's flag wins over the points' years.
    expect(approachModel({ ...barnard, path: { ...barnard.path, closest_in_span: false } }, NOW).inSpan).toBe(false);
    expect(approachModel({ ...far, path: { ...far.path, closest_in_span: true } }, NOW).inSpan).toBe(true);
  });

  it('says what pulls on it', () => {
    expect(gravityView(barnard)).toEqual({
      summary: "No companion found in Gaia; its path is set by the Galaxy's overall pull.", companions: [], wobble: null,
    });
  });

  it('keeps every raw value for researchers', () => {
    const groups = researcherGroups(barnard);
    expect(groups.map((g) => g.title)).toEqual(['Identity', 'Distance and motion', 'Physical', 'Gravity', 'Path']);
    const rows = Object.fromEntries(groups.flatMap((g) => g.rows));
    expect(rows.Distance).toBe('1.83 ± 0.0003 pc (Gaia parallax)');
    expect(rows.Parallax).toBe('547 mas');
    expect(rows['µα*, µδ']).toBe('−801.6, +10,362.4 mas/yr');
    expect(rows['U, V, W']).toMatch(/^−141.8, \+4.4, \+18.6 km\/s/);
    expect(rows.Radius).toBe('0.19 ± 0.01 R☉ (Gaia FLAME)');
    expect(rows['Closest approach']).toBe('3.8 ly in 11,800');
    expect(rows).not.toHaveProperty('Age');
  });

  it('heads the section with its kind', () => {
    expect(identityLine(barnard)).toBe('Red dwarf star · M4V · in Ophiuchus');
  });
});

describe('size wording', () => {
  const q = (sun) => ({ Sun: sun, Jupiter: sun * 9.955, Earth: sun * 109.2 });
  it('covers white dwarfs, brown dwarfs, the Sun and giants', () => {
    expect(sizePhrase(q(0.012))).toBe("About 1/83 the Sun's width — about Earth's size");
    expect(sizePhrase(q(0.1))).toBe("About 1/10 the Sun's width — about Jupiter's size");
    expect(sizePhrase(q(0.7))).toBe("About 70% of the Sun's width — 7 Jupiters across");
    expect(sizePhrase(q(1))).toBe("About the Sun's width");
    expect(sizePhrase(q(12))).toBe("About 12 times the Sun's width");
  });

  it('draws nearby references, on a log scale only when sizes differ hugely', () => {
    const red = discModel(0.19);
    expect(red.discs.map((d) => d.label)).toEqual(['Jupiter', 'This object', 'Sun']);
    expect(red.log).toBe(false);
    const giant = discModel(500);
    expect(giant.discs.map((d) => d.label)).toEqual(['Sun', 'This object']);
    expect(giant.log).toBe(true);
    expect(giant.discs[0].px).toBeGreaterThan(2);
  });

  it('falls back to the radius when no comparison is listed', () => {
    expect(sizeView({ physical: { radius_rsun: 1.0, size_comparison: null } }).phrase).toBe("About the Sun's width");
  });
});

describe('partial profiles', () => {
  it('returns nothing for unknown parts', () => {
    expect(distanceView(empty)).toBeNull();
    expect(sizeView(empty)).toBeNull();
    expect(temperatureView(empty)).toBeNull();
    expect(speedView(empty)).toBeNull();
    expect(pathModel(empty, NOW)).toBeNull();
    expect(approachModel(empty, NOW)).toBeNull();
    expect(gravityView(empty)).toBeNull();
    expect(whyList(empty)).toEqual([]);
    expect(researcherGroups(empty).map((g) => g.title)).toEqual(['Identity']);
  });

  it('gives a distance range from motion alone, marked as an estimate', () => {
    const d = distanceView({ ...empty, motion: { estimated_distance_pc: [20, 100] } });
    expect(d).toMatchObject({ value: '65–330', sub: 'estimate', light: null });
    expect(d.phrase).toMatch(/^Perhaps 65 to 330 light-years away.*An estimate, not a measurement\.$/);
  });

  it('falls back to the sideways speed, then to the motion across the sky', () => {
    expect(speedView({ motion: { tangential_km_s: 30 } })).toMatchObject({ value: '30', sub: 'sideways, at least' });
    expect(speedView({ motion: { pm: 450 } })).toMatchObject({ value: '0.45', unit: '″/yr', phrase: null });
  });

  it('says BCE for light that left long ago, and "years ago" for far objects', () => {
    expect(distanceView({ distance: { ly: 2500, light_left_year: -474, source: 'gaia_parallax' } }).light)
      .toBe('The light you see left it around 474 BCE.');
    expect(distanceView({ distance: { ly: 26000, light_left_year: -23974 } }).light)
      .toBe('The light you see left it about 26,000 years ago.');
  });

  it('describes companions and a wobble hint', () => {
    const g = gravityView({ gravity: {
      summary: 'One likely companion.', ruwe: 2.31, wobble_hint: true, non_single_star: 1,
      companions: [{ gaia_id: '123', name: null, sep_arcsec: 7.8, sep_au: 640, g_mag: 15.2, period_yr: 9200, bound: 'likely' }],
    } });
    expect(g.companions).toEqual([{ key: '123', name: 'Gaia DR3 123', text: '640 AU apart (7.8″) · one orbit takes roughly 9,200 years · likely bound' }]);
    expect(g.wobble).toBe("Gaia's fit to its motion is poor (RUWE 2.31), which suggests an unseen companion tugging it. Gaia lists it as a non-single star.");
  });

  it('does not repeat what the gravity summary already says', () => {
    const view = (summary) => gravityView({ gravity: { summary, ruwe: 2.31, wobble_hint: true, non_single_star: 1, companions: [] } });
    // The backend's own wording (planetx/profile/service.py _gravity_summary).
    expect(view("No star moving with it was found in Gaia within 30'. Gaia's position fit is poor (RUWE 2.3), which suggests "
      + 'an unseen companion. Gaia lists it as a non-single star (it fitted a binary orbit).').wobble).toBeNull();
    expect(view('Its wobble hints at a companion.').wobble).toBe('Gaia lists it as a non-single star.');
    expect(view('Gaia lists it as a non-single star.').wobble).toMatch(/^Gaia's fit to its motion is poor \(RUWE 2.31\)/);
    expect(view(null)).toMatchObject({ summary: null, wobble: expect.stringMatching(/RUWE 2.31.*non-single star\.$/) });
  });

  it('rejects a colour that is not a hex code', () => {
    expect(temperatureView({ physical: { teff_k: 1200, color_hex: 'red;x' } })).toMatchObject({ color: null, sub: 'deep red' });
    expect(temperatureView({ physical: { teff_k: 1200 } }).phrase).toMatch(/most of its light is infrared/);
    expect(colourWord(20000)).toBe('blue');
  });

  it('names a SIMBAD motion and the luminosity source for researchers', () => {
    const rows = Object.fromEntries(researcherGroups({
      ...empty, motion: { pm: 8100, pm_err: 200, source: 'simbad' }, physical: { luminosity_lsun: 0.001, luminosity_source: 'Gaia FLAME', teff_k: 285.4 },
    }).flatMap((g) => g.rows));
    expect(rows['Total motion']).toBe('8,100 ± 200 mas/yr (SIMBAD)');
    expect(rows.Luminosity).toBe('0.001 L☉ (Gaia FLAME)');
    expect(rows.Teff).toBe('285 K');
  });

  it('draws a path whose points carry no distance, without an approach curve', () => {
    const points = barnard.path.points.map((q) => ({ ...q, dist_ly: null }));
    const p = { ...barnard, path: { ...barnard.path, points, closest_approach: null } };
    expect(pathModel(p, NOW).points).toHaveLength(41);
    expect(approachModel(p, NOW)).toBeNull();
  });

  it('keeps only http(s) links', () => {
    expect(safeLinks({ links: [{ label: 'a', url: 'https://x.test' }, { label: 'b', url: 'javascript:alert(1)' }, { label: 'c' }] }))
      .toEqual([{ label: 'a', url: 'https://x.test' }]);
  });
});

describe('sky geometry', () => {
  it('measures true angular offsets, east and north', () => {
    expect(skyOffset(10, 0, 10, 1).y).toBeCloseTo(1, 9);
    expect(skyOffset(10, 0, 11, 0).x).toBeCloseTo(1, 9);
    const far = skyOffset(0, 60, 180, 60); // over the pole: 60° away, due north
    expect(far.x).toBeCloseTo(0, 9);
    expect(far.y).toBeCloseTo(60, 9);
  });

  it('picks round scale bars', () => {
    expect(scaleBar(70)).toEqual({ deg: 10, label: '10°' });
    expect(scaleBar(0.5).label).toBe('5′');
    expect(scaleBar(0.002).label).toBe('1″');
  });
});

describe('profile requests', () => {
  it('asks for a catalogued star by its Gaia id and SIMBAD name', () => {
    const row = { ra: 269.4473, dec: 4.7685, gaia: { id: '4472832130942575872' }, simbad: { name: "Barnard's Star" }, pmra: -801.551, pmdec: 10362.394, mag: 8.2 };
    expect(profileQuery(knownProfileParams(row))).toBe(
      'ra=269.447300&dec=4.768500&gaia_id=4472832130942575872&name=Barnard%27s+Star&pmra=-801.551&pmdec=10362.394&mag=8.20',
    );
  });

  // Barnard's Star as the backend's catalogue check lists it: its SIMBAD entry, then its Gaia star.
  const barnardWho = {
    name: "Barnard's Star",
    matches: [
      { catalog: 'simbad', name: "Barnard's Star", magnitude: 9.5, pm_ra_cosdec: -0.8016, pm_dec: 10.3624 },
      { catalog: 'gaia', name: 'Gaia DR3 4472832130942575872', magnitude: 8.2, pm_ra_cosdec: -0.8016, pm_dec: 10.3624 },
    ],
  };
  const barnardO = { ra: 269.4473, dec: 4.7685, kind: 'mover', pm_ra_cosdec: -0.82, pm_dec: 10.36, pm_err: 0.03 };

  it('asks for an engine object with its measured motion in mas/yr', () => {
    const p = engineProfileParams(barnardO, barnardWho);
    expect(p).toMatchObject({ ra: 269.4473, dec: 4.7685, gaia_id: '4472832130942575872', name: "Barnard's Star", mag: 9.5 });
    expect(p.pmra).toBeCloseTo(-820, 6);
    expect(p.pmdec).toBeCloseTo(10360, 6);
    expect(p.pm_err).toBeCloseTo(30, 6);
  });

  it('uses the archive refit when it confirmed the motion', () => {
    const o = { ...barnardO, pm_err: 1.2, archive: { verdict: 'confirmed', pm_ra_cosdec: -0.81, pm_dec: 10.37, pm_err: 0.02 } };
    const p = engineProfileParams(o, barnardWho);
    expect([p.pmra, p.pmdec, p.pm_err].map((v) => Math.round(v))).toEqual([-810, 10370, 20]);
  });

  it('never asks for a neighbour\'s profile', () => {
    const ids = (o, who) => { const p = engineProfileParams(o, who); return [p.gaia_id, p.name, p.mag]; };
    const none = [null, null, null];
    const slow = { catalog: 'gaia', name: 'Gaia DR3 123456789', magnitude: 14, pm_ra_cosdec: 0.01, pm_dec: -0.02 };
    const mover = { ...barnardO, pm_ra_cosdec: 1.5, pm_dec: 4.2, pm_err: 0.4 };
    // Named after the star it sits next to (backend classify/check.py), or a false track's matches.
    expect(ids(mover, { name: 'near Gaia DR3 123456789', matches: [slow] })).toEqual(none);
    expect(ids(mover, { name: null, matches: [slow] })).toEqual(none);
    // The lead match moves differently from what we measured.
    expect(ids(mover, { name: slow.name, matches: [slow] })).toEqual(none);
    // A mover's lead without a listed motion cannot be checked.
    expect(ids(mover, { name: 'Some star', matches: [{ catalog: 'simbad', name: 'Some star', magnitude: 12 }] })).toEqual(none);
    // An asteroid lead is not a SIMBAD or Gaia object, even if a star lies behind it.
    expect(ids(mover, { name: '(433) Eros', matches: [{ catalog: 'skybot', name: '(433) Eros' }, slow] })).toEqual(none);
    // The measured motion still goes with every mover.
    const p = engineProfileParams(mover, { name: 'near Gaia DR3 123456789', matches: [slow] });
    expect([p.pmra, p.pmdec, p.pm_err].map((v) => Math.round(v))).toEqual([1500, 4200, 400]);
  });

  it('keeps a SIMBAD lead but not a Gaia star that moves differently from it', () => {
    const who = { ...barnardWho, matches: [barnardWho.matches[0], { ...barnardWho.matches[1], pm_ra_cosdec: -0.8, pm_dec: 9.0 }] };
    const p = engineProfileParams({ ...barnardO, pm_err: 0.6 }, who);
    expect([p.gaia_id, p.name]).toEqual([null, "Barnard's Star"]);
  });

  it('names a static object after its lead, with no motion', () => {
    const who = { name: 'HD 1', matches: [{ catalog: 'simbad', name: 'HD 1', magnitude: 7.1 }, { catalog: 'gaia', name: 'Gaia DR3 42', pm_ra_cosdec: 0.02, pm_dec: 0.01 }] };
    const p = engineProfileParams({ ra: 1, dec: 2, kind: 'static', pm_ra_cosdec: 0.1, pm_dec: -0.05, pm_err: 0.2 }, who);
    expect(p).toMatchObject({ gaia_id: '42', name: 'HD 1', pmra: null, pmdec: null, pm_err: null, mag: 7.1 });
  });

  it('sends no motion for a fake one', () => {
    const p = engineProfileParams({ ...barnardO, pm_ra_cosdec: 0.4, pm_dec: 0.3, motion_verdict: { verdict: 'fake_motion' } }, { matches: [] });
    expect([p.pmra, p.pmdec, p.pm_err]).toEqual([null, null, null]);
  });

  it('leaves out a static object\'s slope and an uncatalogued name', () => {
    const p = engineProfileParams({ ra: 1, dec: 2, kind: 'static', pm_ra_cosdec: 0.4, pm_dec: 0.1, pm_err: 0.3 }, { matches: [] });
    expect(profileQuery(p)).toBe('ra=1.000000&dec=2.000000');
  });

  it('refuses a missing position and trims bad parameters', () => {
    expect(profileQuery({ ra: NaN, dec: 1 })).toBeNull();
    expect(profileQuery({ ra: 1, dec: 91 })).toBeNull();
    const q = new URLSearchParams(profileQuery({ ra: -10, dec: 1, gaia_id: '12; drop', name: 'x'.repeat(150), pmra: 5 }));
    expect(q.get('ra')).toBe('350.000000');
    expect(q.has('gaia_id')).toBe(false);
    expect(q.get('name')).toHaveLength(100);
    expect(q.has('pmra')).toBe(false); // without pmdec
  });

  it('keeps to the backend limits', () => {
    const query = (extra) => new URLSearchParams(profileQuery({ ra: 1, dec: 2, pmra: 100, pmdec: -200, pm_err: 5, ...extra }));
    const motion = (q) => ['pmra', 'pmdec', 'pm_err'].filter((k) => q.has(k));
    expect(motion(query({}))).toEqual(['pmra', 'pmdec', 'pm_err']);
    expect(motion(query({ pm_err: null }))).toEqual(['pmra', 'pmdec']);
    expect(motion(query({ pmra: 20000, pmdec: -20000, pm_err: 20000 }))).toEqual(['pmra', 'pmdec', 'pm_err']);
    for (const bad of [{ pmra: 20001 }, { pmdec: -25000 }, { pm_err: 20001 }, { pm_err: NaN }, { pm_err: Infinity }, { pmra: -Infinity }, { pm_err: -1 }]) {
      expect(motion(query(bad))).toEqual([]);
    }
    expect(query({ gaia_id: '1234567890123456789' }).get('gaia_id')).toBe('1234567890123456789');
    expect(query({ gaia_id: '12345678901234567890' }).has('gaia_id')).toBe(false);
    expect(query({ mag: 40 }).get('mag')).toBe('40.00');
    expect(query({ mag: 41 }).has('mag')).toBe(false);
  });
});
