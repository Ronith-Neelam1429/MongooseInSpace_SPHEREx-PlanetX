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

  it('asks for an engine object with its measured motion in mas/yr', () => {
    const o = { ra: 269.4473, dec: 4.7685, kind: 'known_mover', pm_ra_cosdec: -0.82, pm_dec: 10.36, pm_err: 0.03 };
    const who = { matches: [{ catalog: 'simbad', name: "Barnard's Star", magnitude: 9.5 }, { catalog: 'gaia', name: 'Gaia DR3 4472832130942575872' }] };
    const p = engineProfileParams(o, who);
    expect(p).toMatchObject({ ra: 269.4473, dec: 4.7685, gaia_id: '4472832130942575872', name: "Barnard's Star", mag: 9.5 });
    expect(p.pmra).toBeCloseTo(-820, 6);
    expect(p.pmdec).toBeCloseTo(10360, 6);
    expect(p.pm_err).toBeCloseTo(30, 6);
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
});
