import { describe, expect, it } from 'vitest';
import { pickMarker, placeLabel } from './starfield.js';

// Ranks: 0 engine result, 1 labelled or fast object, 2 other SIMBAD entry, 3 plain field star.
const marker = (name, rank, x, y) => ({ name, rank, x, y });
const pick = (markers, x, y) => pickMarker(markers, x, y)?.name ?? null;

describe('pickMarker', () => {
  it('picks nothing when no marker is within its hit radius', () => {
    expect(pick([], 100, 100)).toBeNull();
    expect(pick([marker('star', 3, 100, 100)], 108, 100)).toBeNull();
    expect(pick([marker('result', 0, 100, 100)], 124, 100)).toBeNull();
  });

  it('gives notable objects a larger target than field stars', () => {
    expect(pick([marker('result', 0, 100, 100)], 120, 100)).toBe('result');
    expect(pick([marker('named', 1, 100, 100)], 115, 100)).toBe('named');
    expect(pick([marker('simbad', 2, 100, 100)], 111, 100)).toBe('simbad');
    expect(pick([marker('star', 3, 100, 100)], 105, 100)).toBe('star');
  });

  it('prefers the most notable of several markers under the pointer', () => {
    // An engine result, its SIMBAD square and its Gaia dot sit on top of each other.
    const stack = [marker('star', 3, 100, 100), marker('simbad', 2, 101, 100), marker('result', 0, 103, 101)];
    expect(pick(stack, 100, 100)).toBe('result');
    expect(pick(stack.slice(0, 2), 100, 100)).toBe('simbad');
  });

  it('prefers a notable object nearby over a faint star that is a little closer', () => {
    const markers = [marker('star', 3, 100, 100), marker('result', 0, 112, 100)];
    expect(pick(markers, 107, 100)).toBe('result'); // star is out of its own reach here
    // 9 px from a SIMBAD square, 11 px from an engine result: the result's larger target wins.
    expect(pick([marker('simbad', 2, 100, 100), marker('result', 0, 120, 100)], 109, 100)).toBe('result');
  });

  it('still picks a faint star that is pointed at directly', () => {
    const markers = [marker('star', 3, 100, 100), marker('result', 0, 115, 100)];
    expect(pick(markers, 101, 100)).toBe('star');
    expect(pick(markers, 110, 100)).toBe('result');
  });

  it('picks the nearer of two equally notable markers', () => {
    const markers = [marker('a', 1, 100, 100), marker('b', 1, 110, 100)];
    expect(pick(markers, 103, 100)).toBe('a');
    expect(pick(markers, 107, 100)).toBe('b');
    expect(pick([marker('a', 2, 100, 100), marker('b', 2, 104, 100)], 103, 100)).toBe('b'); // both under the pointer
  });
});

describe('placeLabel', () => {
  const bounds = { w: 800, h: 600 };
  const overlaps = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;

  it('puts a label up and to the right of its marker by default', () => {
    expect(placeLabel({ x: 400, y: 300 }, 100, [], bounds)).toEqual({ x: 412, y: 278, w: 100, h: 18 });
  });

  it('moves to another side to stay clear of labels already placed', () => {
    const first = placeLabel({ x: 400, y: 300 }, 100, [], bounds);
    const second = placeLabel({ x: 405, y: 303 }, 100, [first], bounds);
    expect(second).not.toBeNull();
    expect(overlaps(first, second)).toBe(false);
  });

  it('keeps labels inside the canvas', () => {
    const right = placeLabel({ x: 780, y: 300 }, 100, [], bounds);
    expect(right.x + right.w).toBeLessThanOrEqual(bounds.w);
    const top = placeLabel({ x: 400, y: 5 }, 100, [], bounds);
    expect(top.y).toBeGreaterThanOrEqual(0);
  });

  it('gives up rather than overlap when every side is taken', () => {
    const taken = [{ x: 250, y: 250, w: 300, h: 100 }];
    expect(placeLabel({ x: 400, y: 300 }, 100, taken, bounds)).toBeNull();
  });
});
