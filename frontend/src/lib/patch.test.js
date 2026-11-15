import { describe, expect, it } from 'vitest';
import { flaggedKnown, otherKnown, patchVerdict } from './patch.js';

const barnard = { key: 'simbad:Barnard', name: "Barnard's Star", pm: 10.39, simbad: { otype: 'BY*', path: '* > BY*' }, gaia: { id: '447' } };
const rdra = { key: 'simbad:R Dra', name: 'R Dra', pm: 0.02, simbad: { otype: 'Mi*', path: '* > Ev* > LP* > Mi*' }, gaia: null };
const plain = { key: 'gaia:1', name: 'Gaia DR3 1', pm: 0.004, simbad: null, gaia: { id: '1' } };
const galaxy = { key: 'simbad:G', name: 'NGC 1', pm: null, simbad: { otype: 'G', path: 'G' }, gaia: null };
const rows = [barnard, rdra, plain, galaxy];

const result = (o, names = []) => ({ o, who: { name: names[0] || null, matches: names.map((name) => ({ name })), checked: true } });
const mover = (extra = {}) => ({ kind: 'mover', is_variable: false, ...extra });

describe('the verdict of a patch', () => {
  it('counts what the engine flagged, a catalogued mover as a known one', () => {
    const results = [
      result(mover(), ["Barnard's Star"]),
      result({ kind: 'candidate' }),
      result({ kind: 'static', is_variable: true }),
    ];
    expect(patchVerdict({ ran: true, results, rows, loaded: true }))
      .toEqual({ text: '1 candidate · 1 known mover · 1 variable', tone: 'measured' });
    expect(patchVerdict({ ran: true, results: [result(mover()), result(mover())], rows, loaded: true }).text).toBe('2 movers');
    expect(patchVerdict({ ran: true, results: [result({ kind: 'static', classification: 'artifact' })], rows, loaded: true }).text).toBe('1 other result');
  });

  it('says plainly when nothing moved', () => {
    expect(patchVerdict({ ran: true, results: [], rows, loaded: true })).toEqual({ text: 'Nothing moved here', tone: 'plain' });
  });

  it('before a run, says what the catalogues list and that it is not measured yet', () => {
    expect(patchVerdict({ ran: false, results: [], rows, loaded: true }))
      .toEqual({ text: '1 catalogued fast mover · 1 variable star · not measured yet', tone: 'signal' });
    expect(patchVerdict({ ran: false, results: [], rows: [plain], loaded: true }))
      .toEqual({ text: 'No catalogued fast movers · not measured yet', tone: 'plain' });
    expect(patchVerdict({ ran: false, results: [], rows: [], loaded: false }).text).toBe('Looking up what is known here');
  });
});

describe('which known objects are listed first', () => {
  it('lists fast movers and variables, once, and leaves the rest for More', () => {
    expect(flaggedKnown(rows, [])).toEqual([barnard, rdra]);
    // Measured by the engine: listed as the measurement, not twice.
    expect(flaggedKnown(rows, [result(mover(), ["Barnard's Star"])])).toEqual([rdra]);
    expect(flaggedKnown(rows, [result(mover(), ['Gaia DR3 447'])])).toEqual([rdra]);
    expect(otherKnown(rows)).toEqual([plain, galaxy]);
    // Measured: the catalogue row moves behind "More" instead of vanishing.
    expect(otherKnown(rows, [result(mover(), ["Barnard's Star"])])).toEqual([barnard, plain, galaxy]);
  });
});
