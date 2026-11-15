import { describe, expect, it } from 'vitest';
import {
  alignShift, blur, buildTrace, gridFromFloats, gridToSky, greyRgba, locate, matchSigma, meanStack, overlayRgba, shiftImage, skyToGrid, stretch, timeColor,
  traceRgba, unstretch, zmap,
} from './imaging.js';

/** A w×w sky of level `sky` with ±`noise` hash noise and a star of `peak` at (sx, sy). */
function field(w, { sky = 50, noise = 4, peak = 0, sx = 0, sy = 0, width = 1.5 } = {}) {
  const data = new Float32Array(w * w);
  for (let y = 0; y < w; y++) {
    for (let x = 0; x < w; x++) {
      const n = (((x * 73856093) ^ (y * 19349663)) % 1000) / 1000 - 0.5;
      const star = peak * Math.exp(-((x - sx) ** 2 + (y - sy) ** 2) / (2 * width * width));
      data[y * w + x] = sky + 2 * noise * n + star;
    }
  }
  return data;
}
const ones = (n) => new Uint8Array(n).fill(1);

describe('blur', () => {
  it('keeps total light and spreads a point', () => {
    const w = 21, data = new Float32Array(w * w);
    data[10 * w + 10] = 100;
    const out = blur(data, ones(w * w), w, w, 2);
    const sum = out.reduce((a, b) => a + b, 0);
    expect(sum).toBeCloseTo(100, 0);
    expect(out[10 * w + 10]).toBeLessThan(10);
    expect(out[10 * w + 12]).toBeGreaterThan(0);
  });

  it('does not pull masked (no data) pixels in as black', () => {
    const w = 10, data = new Float32Array(w * w).fill(80), mask = ones(w * w);
    for (let y = 0; y < w; y++) { mask[y * w] = 0; data[y * w] = 0; }
    const out = blur(data, mask, w, w, 1.5);
    expect(out[5 * w + 1]).toBeCloseTo(80, 4);
    expect(out[5 * w]).toBe(0); // masked pixels unchanged
  });

  it('is a copy when sigma is tiny', () => {
    const data = Float32Array.from([1, 2, 3, 4]);
    expect(Array.from(blur(data, ones(4), 2, 2, 0))).toEqual([1, 2, 3, 4]);
  });
});

describe('stretch', () => {
  it('puts two images with different sky and noise on the same scale', () => {
    const a = stretch(field(40, { sky: 30, noise: 2, peak: 200, sx: 20, sy: 20 }), ones(1600));
    const b = stretch(field(40, { sky: 120, noise: 10, peak: 1000, sx: 20, sy: 20 }), ones(1600));
    const median = (img) => [...img].sort((x, y) => x - y)[800];
    expect(Math.abs(median(a) - median(b))).toBeLessThan(0.05);
    expect(a[20 * 40 + 20]).toBeGreaterThan(0.9);
    expect(b[20 * 40 + 20]).toBeGreaterThan(0.9);
  });

  it('gives NaN where there is no data', () => {
    const mask = ones(1600);
    mask[0] = 0;
    const out = stretch(field(40, { peak: 100, sx: 5, sy: 5 }), mask);
    expect(Number.isNaN(out[0])).toBe(true);
    expect(Number.isNaN(out[1])).toBe(false);
  });
});

describe('matchSigma', () => {
  it('blurs a sharp image up to the coarse one and leaves the coarse one alone', () => {
    // sqrt(7.5² − 2.9²) / 2.3548 / 1 ≈ 2.94 px
    expect(matchSigma(2.9, 7.5, 1)).toBeCloseTo(2.94, 2);
    expect(matchSigma(7.5, 7.5, 1)).toBe(0);
  });
});

describe('overlayRgba', () => {
  it('puts the older image in red and the newer in cyan', () => {
    const older = Float32Array.from([1, 0, 1, NaN]);
    const newer = Float32Array.from([0, 1, 1, NaN]);
    const px = Array.from(overlayRgba(older, newer));
    expect(px.slice(0, 4)).toEqual([255, 0, 0, 255]); // was here: red
    expect(px.slice(4, 8)).toEqual([0, 255, 255, 255]); // is here now: cyan
    expect(px.slice(8, 12)).toEqual([255, 255, 255, 255]); // never moved: white
    expect(px[15]).toBe(0); // no data: transparent
  });
});

describe('greyRgba', () => {
  it('draws grey with transparent no-data', () => {
    expect(Array.from(greyRgba(Float32Array.from([0.5, NaN])))).toEqual([128, 128, 128, 255, 0, 0, 0, 0]);
  });
});

// ---------- trace ----------

/** A noise-unit field of fixed stars plus one star at (mx, my), the whole field moved by (ox, oy). */
function starField(n, { mx, my, ox = 0, oy = 0, seed = 1, mover = 40 }) {
  const stars = [[12, 15, 30], [40, 9, 25], [50, 44, 40], [20, 50, 20], [33, 27, 15], [8, 35, 35], [55, 22, 18]];
  const z = new Float32Array(n * n);
  let r = seed;
  const rnd = () => { r = (r * 16807) % 2147483647; return r / 2147483647 - 0.5; };
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      let v = 0.8 * rnd();
      for (const [sx, sy, a] of stars) v += a * Math.exp(-((x - sx - ox) ** 2 + (y - sy - oy) ** 2) / (2 * 1.5 ** 2));
      if (mover) v += mover * Math.exp(-((x - mx - ox) ** 2 + (y - my - oy) ** 2) / (2 * 1.5 ** 2));
      z[y * n + x] = v;
    }
  }
  return z;
}

describe('gridToSky / skyToGrid', () => {
  it('are inverse, with east to the left and north up', () => {
    const p = gridToSky(0, 0, 100, 200);
    expect(p.east).toBeCloseTo(99, 6); // top-left corner pixel: east and north of the centre
    expect(p.north).toBeCloseTo(99, 6);
    const g = skyToGrid(p.east, p.north, 100, 200);
    expect(g.col).toBeCloseTo(0, 6);
    expect(g.row).toBeCloseTo(0, 6);
  });
});

describe('alignShift', () => {
  it('finds a sub-pixel offset from the fixed stars and ignores the moving one', () => {
    const n = 64;
    const ref = starField(n, { mx: 30, my: 10, seed: 3 });
    // The same field 2.4 px right and 1.6 px down; the moving star has also gone elsewhere.
    const img = starField(n, { mx: 30, my: 40, ox: 2.4, oy: 1.6, seed: 7 });
    const exclude = new Uint8Array(n * n);
    for (let y = 0; y < n; y++) for (let x = 25; x < 36; x++) exclude[y * n + x] = 1;
    const s = alignShift(ref, img, n, n, exclude, 5);
    expect(s.ok).toBe(true);
    expect(s.dx).toBeCloseTo(2.4, 0);
    expect(s.dy).toBeCloseTo(1.6, 0);
    expect(Math.abs(s.dx - 2.4)).toBeLessThan(0.3);
  });

  it('does not invent a shift from pure noise', () => {
    const n = 48, a = new Float32Array(n * n), b = new Float32Array(n * n);
    let r = 5;
    const rnd = () => { r = (r * 16807) % 2147483647; return r / 2147483647 - 0.5; };
    for (let i = 0; i < n * n; i++) { a[i] = rnd(); b[i] = rnd(); }
    const s = alignShift(a, b, n, n, new Uint8Array(n * n), 4);
    expect(s.ok).toBe(false);
    expect([s.dx, s.dy]).toEqual([0, 0]);
  });
});

describe('locate', () => {
  it('finds a star near its predicted spot to a fraction of a pixel', () => {
    const n = 64, z = starField(n, { mx: 30.3, my: 20.7 });
    const f = locate(z, n, n, 32, 22, 5, 3);
    expect(f.seen).toBe(true);
    expect(f.x).toBeCloseTo(30.3, 0);
    expect(f.y).toBeCloseTo(20.7, 0);
    expect(f.snr).toBeGreaterThan(20);
  });

  it('says a star is not there when only noise is', () => {
    const n = 64, z = starField(n, { mx: 30, my: 20, mover: 0 });
    const f = locate(z, n, n, 30, 20, 3, 3);
    expect(f.seen).toBe(false);
  });
});

describe('shiftImage', () => {
  it('moves an image by whole and fractional pixels', () => {
    const w = 4, img = Float32Array.from([0, 1, 2, 3, 0, 1, 2, 3, 0, 1, 2, 3, 0, 1, 2, 3]);
    const out = shiftImage(img, w, w, 0.5, 0);
    expect(out[1]).toBeCloseTo(1.5, 6);
  });
});

describe('meanStack', () => {
  it('averages in noise units and skips no data', () => {
    const out = meanStack([Float32Array.from([2, NaN, NaN]), Float32Array.from([4, 3, NaN])]);
    expect(out[0]).toBeCloseTo(3 * Math.SQRT2, 6);
    expect(out[1]).toBe(3);
    expect(Number.isNaN(out[2])).toBe(true);
  });
});

describe('buildTrace', () => {
  it('lines images up and finds the moving star in each', () => {
    const n = 64;
    const a = starField(n, { mx: 30, my: 50, ox: 1.5, oy: -1, seed: 2 }); // old image, misregistered
    const b = starField(n, { mx: 30, my: 12, seed: 9 }); // new image, the reference
    const items = [
      { key: 'old', z: a, cover: 1, at: { x: 30, y: 50 } },
      { key: 'new', z: b, cover: 1, at: { x: 30, y: 12 } },
    ];
    const t = buildTrace(items, n, 3, 4);
    expect(t.refKey).toBe('new');
    const [old, now] = t.items;
    expect(old.shift.ok).toBe(true);
    expect(old.shift.dx).toBeCloseTo(1.5, 0);
    expect(old.found.seen).toBe(true);
    expect(old.found.x).toBeCloseTo(30, 0);
    expect(old.found.y).toBeCloseTo(50, 0);
    expect(now.found.y).toBeCloseTo(12, 0);
    expect(t.backdrop).toHaveLength(n * n);
  });
});

describe('traceRgba', () => {
  it('paints the star in its colour over a dim grey sky', () => {
    const w = 9, backdrop = new Float32Array(w * w).fill(0.5);
    const z = new Float32Array(w * w);
    z[4 * w + 4] = 20;
    const px = traceRgba(backdrop, w, [{ z, x: 4, y: 4, radius: 3, snr: 20, rgb: [255, 0, 0] }]);
    const at = (x, y) => Array.from(px.slice(4 * (y * w + x), 4 * (y * w + x) + 4));
    expect(at(4, 4)).toEqual([255, 0, 0, 255]); // the star: its colour
    expect(at(0, 0)).toEqual([77, 77, 77, 255]); // the sky: dim grey
  });
});

describe('timeColor', () => {
  it('runs from red (oldest) to cyan (newest)', () => {
    expect(timeColor(0)).toEqual([255, 77, 61]);
    expect(timeColor(1)).toEqual([62, 232, 255]);
  });
});

describe('zmap', () => {
  it('puts sky at 0 and noise at 1', () => {
    const data = field(40, { sky: 100, noise: 5 });
    const z = zmap(data, ones(1600));
    const sorted = [...z].sort((a, b) => a - b);
    expect(Math.abs(sorted[800])).toBeLessThan(0.1);
  });
});

describe('unstretch', () => {
  it('inverts the API PNG stretch', () => {
    // grey = 255 * asinh(s / 0.1) / asinh(10)
    for (const s of [0, 0.01, 0.2, 1]) {
      const grey = (255 * Math.asinh(s / 0.1)) / Math.asinh(10);
      expect(unstretch(grey)).toBeCloseTo(s, 6);
    }
  });
});

describe('gridFromFloats', () => {
  it('resamples onto a finer grid edge to edge and keeps no data out', () => {
    const px = Float32Array.from([0, 10, 20, NaN]); // 2×2, bottom right missing
    const { data, mask } = gridFromFloats(px, 2, 2, 4);
    expect(data[0]).toBe(0); // top-left corner
    expect(data[3]).toBe(10); // top-right corner
    expect(mask[15]).toBe(0); // bottom-right: next to no data
    expect(mask[0]).toBe(1);
  });
});
