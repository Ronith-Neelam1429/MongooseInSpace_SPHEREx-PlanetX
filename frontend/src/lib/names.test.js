import { describe, expect, it } from 'vitest';
import { propagate } from './coords.js';
import { displayName, identify, identityOf, knownObjects, mapTarget, notableKnown, sameObject } from './names.js';

const NOW = 2026.87;
const ARCSEC = 1 / 3600;

// Barnard's Star as the catalogues give it: Gaia DR3 at epoch 2016.0, SIMBAD at J2000 moved to
// today (which is what querySimbad returns).
const PM = { pmra: -801.551, pmdec: 10362.394 };
const barnardGaia = { id: '4472832130942575872', ra: 269.44850252544, dec: 4.73942005111, ...PM, plx: 546.9759, plxErr: 0.0401, gmag: 8.193974 };
const simbadNow = (o) => ({ ...o, ...propagate(o.ra, o.dec, o.pmra, o.pmdec, NOW - 2000) });
const barnardSimbad = simbadNow({ name: "Barnard's star", type: 'BY Dra Variable', ra: 269.4520769586187, dec: 4.693364966576667, ...PM, mag: 9.51, band: 'V' });
const planet = simbadNow({ name: "Barnard's Star b", type: 'Extra-solar Planet', ra: 269.4520769586187, dec: 4.693364966576667, ...PM, mag: null, band: null });

// The engine's track of it (real run, three passes), about 110" north of the Gaia 2016.0 position.
const barnard = {
  kind: 'mover', ra: 269.4461530928993, dec: 4.768750301049631, ref_mjd: 61102.1, name: null, classification: null, matches: [],
  track: [
    { ra: 269.44590589921665, dec: 4.767292608746408, mjd: 60914.926 },
    { ra: 269.44657729841947, dec: 4.768957064070984, mjd: 61115.265 },
    { ra: 269.44597622987334, dec: 4.77000120896015, mjd: 61276.109 },
  ],
};

/** A static star and an engine object sitting `dArcsec` north of it. */
const star = (id, gmag, ra = 10, dec = 20) => ({ id, ra, dec, pmra: 2, pmdec: -3, plx: 1, plxErr: 0.5, gmag });
const objectAt = (ra, dec, dArcsec = 0) => ({
  kind: 'candidate', ra, dec: dec + dArcsec * ARCSEC, ref_mjd: 61100, name: null, classification: null, matches: [],
  track: [{ ra, dec: dec + dArcsec * ARCSEC, mjd: 60900 }, { ra, dec: dec + dArcsec * ARCSEC, mjd: 61280 }],
});

describe('identify', () => {
  it("names Barnard's Star from SIMBAD, comparing positions at the dates of the track", () => {
    const who = identify(barnard, { simbad: [planet, barnardSimbad], gaia: [star('1', 15, 269.45, 4.77), barnardGaia] }, NOW);
    expect(who.name).toBe("Barnard's star");
    expect(who.checked).toBe(true);
    expect(who.matches.map((m) => m.catalog)).toEqual(['simbad', 'gaia']);
    const gaia = who.matches[1];
    expect(gaia.name).toBe('Gaia DR3 4472832130942575872');
    expect(gaia.sep_arcsec).toBeLessThan(3);
    expect(Math.hypot(gaia.pm_ra_cosdec, gaia.pm_dec)).toBeCloseTo(10.393, 3); // arcsec/yr
    expect(gaia.distance_ly).toBeCloseTo(5.96, 2);
  });

  it('falls back to the Gaia designation when SIMBAD has nothing there', () => {
    expect(identify(barnard, { simbad: [], gaia: [barnardGaia] }, NOW).name).toBe('Gaia DR3 4472832130942575872');
    expect(identify(barnard, { simbad: null, gaia: [barnardGaia] }, NOW).name).toBe('Gaia DR3 4472832130942575872');
  });

  it('says uncatalogued only when a list was there to look in', () => {
    const far = objectAt(50, -30);
    const none = identify(far, { simbad: [barnardSimbad], gaia: [barnardGaia] }, NOW);
    expect(none).toEqual({ name: null, matches: [], checked: true });
    expect(displayName(none)).toBe('Uncatalogued object');
    const unchecked = identify(far, { simbad: null, gaia: null }, NOW);
    expect(unchecked.checked).toBe(false);
    expect(displayName(unchecked)).toBe('Unnamed object');
  });

  it('matches within about one SPHEREx pixel and no further', () => {
    const s = star('7', 14);
    expect(identify(objectAt(10, 20, 6), { simbad: [], gaia: [s] }, NOW).name).toBe('Gaia DR3 7');
    expect(identify(objectAt(10, 20, 9), { simbad: [], gaia: [s] }, NOW).name).toBeNull();
  });

  it('prefers a bright star over a faint one that is a little nearer', () => {
    const faint = star('faint', 20.5, 10, 20 + 1.5 * ARCSEC);
    const bright = star('bright', 12, 10, 20 - 3 * ARCSEC);
    expect(identify(objectAt(10, 20), { simbad: [], gaia: [faint, bright] }, NOW).name).toBe('Gaia DR3 bright');
  });

  it('does not borrow the name of a different SIMBAD object further away in the same pixel', () => {
    const galaxy = { name: 'LEDA 1', type: 'Galaxy', ra: 10, dec: 20 + 5 * ARCSEC, pmra: null, pmdec: null, mag: null };
    const who = identify(objectAt(10, 20), { simbad: [galaxy], gaia: [star('near', 15, 10, 20 + 0.4 * ARCSEC)] }, NOW);
    expect(who.name).toBe('Gaia DR3 near');
    expect(who.matches).toHaveLength(1);
  });

  it("uses the API's name and matches when the backend ran its catalogue check", () => {
    const matches = [{ catalog: 'skybot', name: '(433) Eros', kind: 'Asteroid', sep_arcsec: 1.2, pm_ra_cosdec: null, pm_dec: null, magnitude: null }];
    const named = identify({ ...barnard, name: '(433) Eros', classification: 'known_asteroid', matches }, { simbad: [barnardSimbad], gaia: [barnardGaia] }, NOW);
    expect(named).toEqual({ name: '(433) Eros', matches, checked: true });
    // Checked by the backend and found nowhere: uncatalogued, whatever the browser's lists hold.
    const unknown = identify({ ...barnard, classification: 'unknown_candidate' }, { simbad: [barnardSimbad], gaia: [barnardGaia] }, NOW);
    expect(unknown).toEqual({ name: null, matches: [], checked: true });
  });
});

describe('knownObjects', () => {
  const tyc = { name: 'TYC 425-262-1', type: 'Spectroscopic Binary', ra: 269.46, dec: 4.70, pmra: null, pmdec: null, mag: 11.5, band: 'V' };
  const faintNamed = { name: 'ATO J269.3920+04.7625', type: 'Eclipsing Binary', ra: 269.392, dec: 4.7625, pmra: null, pmdec: null, mag: 16.5, band: 'G' };
  const runaway = { id: '99', ra: 269.41, dec: 4.72, pmra: 150, pmdec: 0, plx: 20, plxErr: 0.1, gmag: 13 };
  const fieldStar = star('5', 18, 269.43, 4.75);
  const known = knownObjects([planet, tyc, barnardSimbad, faintNamed], [fieldStar, barnardGaia, runaway], NOW);

  it('lists one row per place: a star and its planets share a row led by the star', () => {
    expect(known.rows.map((r) => r.name)).toEqual(["Barnard's star", 'Gaia DR3 99', 'TYC 425-262-1', 'ATO J269.3920+04.7625']);
    expect(known.rows[0].also.map((o) => o.name)).toEqual(["Barnard's Star b"]);
  });

  it('joins a SIMBAD entry with its Gaia star for the motion and distance', () => {
    const row = known.rows[0];
    expect(row.gaia).toBe(barnardGaia);
    expect(row.pm).toBeCloseTo(10.393, 3);
    expect(row.pa).toBeCloseTo(355.6, 1);
    expect(row.distanceLy).toBeCloseTo(5.96, 2);
    expect(row.dec).toBeCloseTo(barnardSimbad.dec, 4); // today's position, not the 2016.0 one
  });

  it('puts fast movers first, then the brightest, and lists fast Gaia stars SIMBAD lacks', () => {
    expect(known.rows[1]).toMatchObject({ name: 'Gaia DR3 99', simbad: null, gaia: runaway, pm: 0.15 });
    expect(known.rows[2].mag).toBe(11.5);
  });

  it('finds the row of a marker picked on the map', () => {
    expect(known.of('simbad', planet)).toBe(known.rows[0]);
    expect(known.of('gaia', barnardGaia)).toBe(known.rows[0]);
    const row = known.of('gaia', fieldStar); // an ordinary field star is not listed, but still has a name
    expect(row).toMatchObject({ name: 'Gaia DR3 5', type: 'Star', gaia: fieldStar, key: 'gaia:5' });
  });

  it('labels only the fast movers and the bright named objects', () => {
    expect(notableKnown(known.rows).map((r) => r.name)).toEqual(["Barnard's star", 'Gaia DR3 99', 'TYC 425-262-1']);
  });
});

describe('selections', () => {
  const row = knownObjects([barnardSimbad], [barnardGaia], NOW).rows[0];

  it('compares engine objects by identity and known objects by key', () => {
    const o = { id: 1 };
    expect(sameObject({ kind: 'engine', obj: o }, { kind: 'engine', obj: o })).toBe(true);
    expect(sameObject({ kind: 'engine', obj: o }, { kind: 'engine', obj: { id: 1 } })).toBe(false);
    expect(sameObject({ kind: 'known', obj: row }, { kind: 'known', obj: { ...row } })).toBe(true);
    expect(sameObject({ kind: 'known', obj: row }, { kind: 'engine', obj: o })).toBe(false);
    expect(sameObject(null, { kind: 'engine', obj: o })).toBe(false);
  });

  it('tracks an engine object at its latest pass and a known object by its drawn marker', () => {
    expect(mapTarget({ kind: 'engine', obj: barnard })).toEqual({ kind: 'engine', obj: barnard, ra: 269.44597622987334, dec: 4.77000120896015 });
    expect(mapTarget({ kind: 'known', obj: row })).toEqual({ kind: 'gaia', obj: barnardGaia, ra: row.ra, dec: row.dec });
    const simbadOnly = knownObjects([barnardSimbad], [], NOW).rows[0];
    expect(mapTarget({ kind: 'known', obj: simbadOnly })).toMatchObject({ kind: 'simbad', obj: barnardSimbad });
  });
});

describe('the identity of an object opened from anywhere', () => {
  const lists = { simbad: [barnardSimbad, planet], gaia: [barnardGaia] };

  it('uses the identity its list row carries', () => {
    const who = { name: 'Listed name', matches: [], checked: true };
    expect(identityOf(barnard, [{ o: barnard, who }], lists, NOW)).toBe(who);
  });

  it('matches an object in no open list against the catalogues, keeping its name', () => {
    // A static star opened through "See the measurement" while the tracked list is closed.
    const still = { ...barnard, kind: 'static' };
    const who = identityOf(still, [{ o: barnard, who: { name: 'other', matches: [], checked: true } }], lists, NOW);
    expect(who.name).toBe("Barnard's star");
    expect(who.matches.map((m) => m.catalog)).toEqual(['simbad', 'gaia']);
    expect(displayName(who)).toBe("Barnard's star");
  });

  it('says no catalogue was there when none is loaded', () => {
    expect(identityOf(barnard, [], { simbad: null, gaia: null }, NOW)).toEqual({ name: null, matches: [], checked: false });
  });
});
