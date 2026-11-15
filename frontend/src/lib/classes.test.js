import { describe, expect, it } from 'vitest';
import { SHOW_ALL, SHOW_MOVING, countClasses, engineClass, gaiaClass, isAll, isMovingOnly, knownClass, simbadClass } from './classes.js';

// Types and paths as SIMBAD's otypedef table gives them.
const simbad = (otype, path, pm = {}) => ({ otype, path, pmra: null, pmdec: null, ...pm });

describe('simbadClass', () => {
  it('sorts types by their place in the SIMBAD hierarchy', () => {
    expect(simbadClass(simbad('*', '*'))).toBe('star');
    expect(simbadClass(simbad('BD*', '* > LM* > BD*'))).toBe('star');
    expect(simbadClass(simbad('G', 'G'))).toBe('galaxy');
    expect(simbadClass(simbad('QSO', 'G > AGN > QSO'))).toBe('galaxy');
    expect(simbadClass(simbad('HII', 'ISM > HII'))).toBe('galaxy');
    expect(simbadClass(simbad('OpC', 'Cl* > OpC'))).toBe('galaxy');
    expect(simbadClass(simbad('PN', '* > Ev* > PN'))).toBe('galaxy');
    expect(simbadClass(simbad('X', 'X'))).toBe('other');
    expect(simbadClass(simbad('Rad', 'Rad'))).toBe('other');
    expect(simbadClass(simbad('?', ''))).toBe('other');
  });

  it('finds variables anywhere in the tree, candidates included', () => {
    expect(simbadClass(simbad('Mi*', '* > Ev* > AB* > Mi*'))).toBe('variable'); // R Dra
    expect(simbadClass(simbad('Mi?', '* > Ev* > AB* > Mi*'))).toBe('variable');
    expect(simbadClass(simbad('EB*', '* > ** > EB*'))).toBe('variable');
    expect(simbadClass(simbad('Or*', '* > Y*O > Or*'))).toBe('variable'); // RW Aur
    expect(simbadClass(simbad('var', null))).toBe('variable');
    // A carbon star is evolved, but not listed as variable.
    expect(simbadClass(simbad('C*', '* > Ev* > C*'))).toBe('star');
  });

  it('puts fast motion first, whatever the type', () => {
    expect(simbadClass(simbad('BY*', '* > ** > BY*', { pmra: -802, pmdec: 10362 }))).toBe('fast'); // Barnard's Star
    expect(simbadClass(simbad('*', '*', { pmra: 80, pmdec: 80 }))).toBe('fast');
    expect(simbadClass(simbad('*', '*', { pmra: 60, pmdec: 50 }))).toBe('star');
  });

  it('falls back to the code when the path is missing', () => {
    expect(simbadClass(simbad('*', null))).toBe('star');
    expect(simbadClass(simbad('WD*', null))).toBe('star');
    expect(simbadClass(simbad('G', null))).toBe('galaxy');
    expect(simbadClass(simbad('Opt', null))).toBe('other');
  });
});

describe('gaiaClass and knownClass', () => {
  it('splits Gaia stars at 0.1″/yr', () => {
    expect(gaiaClass({ pmra: 100, pmdec: 0 })).toBe('fast');
    expect(gaiaClass({ pmra: 99, pmdec: 0 })).toBe('star');
    expect(gaiaClass({ pmra: null, pmdec: null })).toBe('star');
  });

  it('classifies a row by its motion first, then its SIMBAD type', () => {
    expect(knownClass({ pm: 10.4, simbad: simbad('BY*', '* > ** > BY*') })).toBe('fast');
    expect(knownClass({ pm: 0.01, simbad: simbad('Mi*', '* > Ev* > AB* > Mi*') })).toBe('variable');
    expect(knownClass({ pm: null, simbad: simbad('G', 'G') })).toBe('galaxy');
    expect(knownClass({ pm: 0.02, simbad: null })).toBe('star');
    // SIMBAD's own motion is not double-counted when the row (via Gaia) says slow.
    expect(knownClass({ pm: 0.05, simbad: simbad('*', '*', { pmra: 200, pmdec: 0 }) })).toBe('star');
  });
});

describe('engineClass', () => {
  const o = (over) => ({ kind: 'static', is_variable: false, classification: null, ...over });
  it('uses the motion class when no catalogue check ran', () => {
    expect(engineClass(o({ kind: 'mover' }))).toBe('mover');
    expect(engineClass(o({ kind: 'candidate' }))).toBe('candidate');
    expect(engineClass(o({ is_variable: true }))).toBe('variable');
    expect(engineClass(o({}))).toBe('star');
  });
  it('lets the catalogue verdict override', () => {
    expect(engineClass(o({ kind: 'mover', classification: 'artifact' }))).toBe('other');
    expect(engineClass(o({ kind: 'mover', classification: 'unknown_candidate' }))).toBe('candidate');
    expect(engineClass(o({ kind: 'mover', classification: 'known_asteroid' }))).toBe('mover');
    expect(engineClass(o({ kind: 'mover', is_variable: true }))).toBe('mover');
    expect(engineClass(o({ kind: 'candidate', classification: 'known_asteroid' }))).toBe('mover');
    expect(engineClass(o({ kind: 'candidate', classification: 'static' }))).toBe('star');
    // An uncatalogued brightness change without motion is drawn (and filtered) as a variable.
    expect(engineClass(o({ kind: 'static', is_variable: true, classification: 'unknown_candidate' }))).toBe('variable');
  });
  it('lets the motion verdict settle what moves', () => {
    const v = (verdict, certainty = null) => ({ verdict, certainty, cause: null, reason: '', decided_by: 'spherex' });
    // An engine "mover" whose motion is fake is never drawn as a mover.
    expect(engineClass(o({ kind: 'mover', classification: 'unknown_candidate', motion_verdict: v('fake_motion') }))).toBe('other');
    expect(engineClass(o({ kind: 'mover', is_variable: true, motion_verdict: v('fake_motion') }))).toBe('variable');
    expect(engineClass(o({ kind: 'mover', classification: 'unknown_candidate', motion_verdict: v('moving', 'confirmed') }))).toBe('mover');
    expect(engineClass(o({ kind: 'mover', motion_verdict: v('moving', 'candidate') }))).toBe('candidate');
    expect(engineClass(o({ kind: 'static', motion_verdict: v('unsure') }))).toBe('candidate');
    expect(engineClass(o({ kind: 'static', motion_verdict: v('not_moving') }))).toBe('star');
    expect(engineClass(o({ kind: 'static', classification: 'variable_star', motion_verdict: v('not_moving') }))).toBe('variable');
  });
});

describe('countClasses', () => {
  it('counts each place once: a Gaia star in a row is not counted again', () => {
    const joined = { pmra: 5, pmdec: 5 };
    const lone = { pmra: 500, pmdec: 0 };
    const plain = { pmra: 1, pmdec: 1 };
    const rows = [
      { pm: 0.007, simbad: simbad('G', 'G'), gaia: joined },
      { pm: 0.5, simbad: null, gaia: lone },
    ];
    const n = countClasses(rows, [joined, lone, plain], [{ kind: 'candidate', is_variable: false }]);
    expect(n).toEqual({ candidate: 1, mover: 0, fast: 1, variable: 0, star: 1, galaxy: 1, other: 0 });
  });
});

describe('presets', () => {
  it('recognises the moving and all presets', () => {
    expect(isMovingOnly(SHOW_MOVING)).toBe(true);
    expect(isMovingOnly(SHOW_ALL)).toBe(false);
    expect(isAll(SHOW_ALL)).toBe(true);
    expect(isAll({ ...SHOW_ALL, star: false })).toBe(false);
    expect(isMovingOnly({ ...SHOW_MOVING, variable: true })).toBe(false);
  });
});
