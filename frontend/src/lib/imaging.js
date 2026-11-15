// Making images from different telescopes comparable, in the browser. 2MASS, WISE and SPHEREx
// differ in sharpness, noise and stretch, so side by side the same sky looks unrelated. Each image
// is put on one square grid, blurred to the coarsest telescope's sharpness, and stretched from its
// own sky level in units of its own noise. Two such images then overlay as one colour picture:
// the older in red, the newer in cyan, so fixed stars add up to white and a moving one shows as a
// red spot (where it was) next to a cyan one (where it is now).

/** Image sharpness (FWHM, arcsec) as seen here: the telescope's blur, plus pixels for SPHEREx. */
export const FWHM_ARCSEC = { '2mass': 2.9, wise: 6.1, spherex: 7.5 };

/**
 * Gaussian blur of a w×h image with `sigma` pixels, skipping pixels where `mask` is 0: each output
 * is the weighted mean of the valid pixels around it, so no-data edges do not bleed in as black.
 * Returns a new Float32Array; masked pixels come back unchanged.
 */
export function blur(data, mask, w, h, sigma) {
  if (!(sigma > 0.3)) return Float32Array.from(data);
  const r = Math.ceil(3 * sigma);
  const k = new Float32Array(2 * r + 1);
  for (let i = -r; i <= r; i++) k[i + r] = Math.exp(-(i * i) / (2 * sigma * sigma));
  const pass = (src, wt, horizontal) => {
    const out = new Float32Array(w * h), outW = new Float32Array(w * h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        let s = 0, sw = 0;
        for (let i = -r; i <= r; i++) {
          const xx = horizontal ? x + i : x, yy = horizontal ? y : y + i;
          if (xx < 0 || yy < 0 || xx >= w || yy >= h) continue;
          const j = yy * w + xx;
          s += src[j] * k[i + r];
          sw += wt[j] * k[i + r];
        }
        out[y * w + x] = s;
        outW[y * w + x] = sw;
      }
    }
    return [out, outW];
  };
  const weighted = new Float32Array(w * h), wt = new Float32Array(w * h);
  for (let i = 0; i < w * h; i++) {
    wt[i] = mask[i] ? 1 : 0;
    weighted[i] = mask[i] ? data[i] : 0;
  }
  const [s1, w1] = pass(weighted, wt, true);
  const [s2, w2] = pass(s1, w1, false);
  const out = new Float32Array(w * h);
  for (let i = 0; i < w * h; i++) out[i] = mask[i] && w2[i] > 0 ? s2[i] / w2[i] : data[i];
  return out;
}

function quantile(sorted, q) {
  if (!sorted.length) return NaN;
  const i = Math.min(sorted.length - 1, Math.max(0, Math.round(q * (sorted.length - 1))));
  return sorted[i];
}

/**
 * An image in units of its own noise above its sky: (value − median) / (1.4826 × MAD). Masked or
 * non-finite pixels give NaN. Every telescope's image then has sky 0 and noise 1, so a star's
 * value is its signal-to-noise.
 */
export function zmap(data, mask) {
  const vals = [];
  for (let i = 0; i < data.length; i++) if (mask[i] && Number.isFinite(data[i])) vals.push(data[i]);
  const out = new Float32Array(data.length).fill(NaN);
  if (vals.length < 10) return out;
  vals.sort((a, b) => a - b);
  const sky = quantile(vals, 0.5);
  const dev = vals.map((v) => Math.abs(v - sky)).sort((a, b) => a - b);
  // The source PNGs are 8-bit, so a very flat sky can have no spread at all: floor at one level.
  const noise = Math.max(1.4826 * quantile(dev, 0.5), 1e-6 + (vals[vals.length - 1] - vals[0]) / 255);
  for (let i = 0; i < data.length; i++) {
    if (mask[i] && Number.isFinite(data[i])) out[i] = (data[i] - sky) / noise;
  }
  return out;
}

/**
 * A noise-unit image (zmap) on 0..1 for display: asinh, with the sky a little above black so
 * noise still shows as texture, and the top at the image's brightest stars. NaN stays NaN.
 */
export function tone(z) {
  const vals = [];
  for (let i = 0; i < z.length; i++) if (!Number.isNaN(z[i])) vals.push(z[i]);
  const out = new Float32Array(z.length).fill(NaN);
  if (!vals.length) return out;
  vals.sort((a, b) => a - b);
  const top = Math.max(15, quantile(vals, 0.999));
  const soft = 3; // noise units where the scale turns from linear to logarithmic
  const scale = Math.asinh(top / soft);
  for (let i = 0; i < z.length; i++) {
    if (Number.isNaN(z[i])) continue;
    out[i] = Math.min(1, Math.max(0, Math.asinh((z[i] + 1.5) / soft) / scale));
  }
  return out;
}

/** zmap then tone: every telescope's image with the same dark sky and faint-star contrast. */
export function stretch(data, mask) {
  return tone(zmap(data, mask));
}

/** Blur sigma (pixels on a grid of `pixelArcsec`) that brings an image from `fwhm` to `target` arcsec. */
export function matchSigma(fwhm, target, pixelArcsec) {
  if (!(target > fwhm)) return 0;
  return Math.sqrt(target * target - fwhm * fwhm) / 2.3548 / pixelArcsec;
}

/** A stretched image (0..1, NaN = no data) as grey RGBA; no data is transparent. */
export function greyRgba(img) {
  const out = new Uint8ClampedArray(img.length * 4);
  for (let i = 0; i < img.length; i++) {
    const v = img[i];
    if (Number.isNaN(v)) continue;
    const g = Math.round(v * 255);
    out[4 * i] = g; out[4 * i + 1] = g; out[4 * i + 2] = g; out[4 * i + 3] = 255;
  }
  return out;
}

/**
 * Two stretched images (0..1, NaN = no data) as one RGBA picture: `older` in red, `newer` in
 * green and blue (cyan). Where only one has data the other channel stays dark; where neither
 * has, the pixel is transparent.
 */
export function overlayRgba(older, newer) {
  const out = new Uint8ClampedArray(older.length * 4);
  for (let i = 0; i < older.length; i++) {
    const a = older[i], b = newer[i];
    if (Number.isNaN(a) && Number.isNaN(b)) continue;
    const r = Number.isNaN(a) ? 0 : Math.round(a * 255);
    const c = Number.isNaN(b) ? 0 : Math.round(b * 255);
    out[4 * i] = r; out[4 * i + 1] = c; out[4 * i + 2] = c; out[4 * i + 3] = 255;
  }
  return out;
}

// ---------- trace: line the images up on the fixed stars, then find the star in each ----------

/** Pixel (col, row) of an n×n grid covering `side` arcsec <-> offsets (east, north) arcsec from its centre. */
export function gridToSky(col, row, n, side) {
  const pix = side / n, c = (n - 1) / 2;
  return { east: (c - col) * pix, north: (c - row) * pix };
}
export function skyToGrid(east, north, n, side) {
  const pix = side / n, c = (n - 1) / 2;
  return { col: c - east / pix, row: c - north / pix };
}

/** Value of a w-wide image at fractional (x, y), bilinear; NaN outside or next to no data. */
export function sample(img, w, h, x, y) {
  const x0 = Math.floor(x), y0 = Math.floor(y);
  if (x0 < 0 || y0 < 0 || x0 + 1 >= w || y0 + 1 >= h) return NaN;
  const fx = x - x0, fy = y - y0, i = y0 * w + x0;
  return (img[i] * (1 - fx) + img[i + 1] * fx) * (1 - fy) + (img[i + w] * (1 - fx) + img[i + w + 1] * fx) * fy;
}

/** The image moved so that out(x, y) = img(x + dx, y + dy). */
export function shiftImage(img, w, h, dx, dy) {
  if (!dx && !dy) return img;
  const out = new Float32Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) out[y * w + x] = sample(img, w, h, x + dx, y + dy);
  return out;
}

const STAR_CLIP = 30; // noise units: bright stars count, but cannot outweigh all the others
const STAR_FLOOR = 3; // noise units: below this a pixel is sky

/**
 * The shift (dx, dy) in pixels that lines `img` up on `ref`, from the fixed stars both show: the
 * peak of their cross-correlation (signal above 3 noise units only, clipped), searched over ±maxPx and refined
 * to a fraction of a pixel. `exclude` (1 = skip) keeps the moving star out of it. `score` is the
 * normalised correlation at the peak (1 = identical); below `minScore`, or with fewer than a handful
 * of stars in common, the images cannot be lined up and the shift is 0 (`ok` false).
 */
export function alignShift(ref, img, w, h, exclude, maxPx, minScore = 0.25) {
  const a = new Float32Array(w * h), b = new Float32Array(w * h);
  for (let i = 0; i < w * h; i++) {
    // Only what stands out of the noise counts: plain sky would correlate with any other sky.
    a[i] = exclude[i] || !(ref[i] > STAR_FLOOR) ? 0 : Math.min(ref[i], STAR_CLIP) - STAR_FLOOR;
    b[i] = exclude[i] || !(img[i] > STAR_FLOOR) ? 0 : Math.min(img[i], STAR_CLIP) - STAR_FLOOR;
  }
  const score = (dx, dy) => {
    let ab = 0, aa = 0, bb = 0;
    for (let y = Math.max(0, -dy); y < Math.min(h, h - dy); y++) {
      for (let x = Math.max(0, -dx); x < Math.min(w, w - dx); x++) {
        const u = a[y * w + x], v = b[(y + dy) * w + x + dx];
        ab += u * v; aa += u * u; bb += v * v;
      }
    }
    return aa > 0 && bb > 0 ? ab / Math.sqrt(aa * bb) : 0;
  };
  const r = Math.max(1, Math.round(maxPx));
  let best = { dx: 0, dy: 0, s: -Infinity };
  // Coarse search on every other shift, then every shift around the best.
  for (let dy = -r; dy <= r; dy += 2) for (let dx = -r; dx <= r; dx += 2) {
    const s = score(dx, dy);
    if (s > best.s) best = { dx, dy, s };
  }
  const grid = new Map();
  const at = (dx, dy) => {
    const k = `${dx},${dy}`;
    if (!grid.has(k)) grid.set(k, score(dx, dy));
    return grid.get(k);
  };
  for (let dy = best.dy - 1; dy <= best.dy + 1; dy++) for (let dx = best.dx - 1; dx <= best.dx + 1; dx++) {
    if (Math.abs(dx) > r || Math.abs(dy) > r) continue;
    const s = at(dx, dy);
    if (s > best.s) best = { dx, dy, s };
  }
  if (!(best.s >= minScore)) return { dx: 0, dy: 0, score: Math.max(0, best.s), ok: false };
  // Parabola through the peak and its neighbours, per axis.
  const vertex = (m, c0, p) => {
    const d = m - 2 * c0 + p;
    return d < 0 ? Math.max(-0.5, Math.min(0.5, (m - p) / (2 * d))) : 0;
  };
  const fx = vertex(at(best.dx - 1, best.dy), best.s, at(best.dx + 1, best.dy));
  const fy = vertex(at(best.dx, best.dy - 1), best.s, at(best.dx, best.dy + 1));
  return { dx: best.dx + fx, dy: best.dy + fy, score: best.s, ok: true };
}

/**
 * The star near (x0, y0) in a noise-unit image: the brightest pixel within `radius` pixels, then
 * the light-weighted centre of what is above half that peak within `fwhm` of it. `snr` is the peak
 * in noise units; `seen` whether it reaches `minSnr`. Null when there is no data there.
 */
export function locate(z, w, h, x0, y0, radius, fwhm, minSnr = 5) {
  let peak = -Infinity, px = -1, py = -1;
  const r2 = radius * radius;
  for (let y = Math.max(0, Math.floor(y0 - radius)); y <= Math.min(h - 1, Math.ceil(y0 + radius)); y++) {
    for (let x = Math.max(0, Math.floor(x0 - radius)); x <= Math.min(w - 1, Math.ceil(x0 + radius)); x++) {
      if ((x - x0) ** 2 + (y - y0) ** 2 > r2) continue;
      const v = z[y * w + x];
      if (v > peak) { peak = v; px = x; py = y; }
    }
  }
  if (px < 0) return null;
  if (!(peak >= minSnr)) return { x: x0, y: y0, snr: Math.max(0, peak), seen: false };
  let sx = 0, sy = 0, sw = 0;
  const half = peak / 2, rr = Math.max(1, fwhm) ** 2;
  for (let y = Math.max(0, Math.floor(py - fwhm)); y <= Math.min(h - 1, Math.ceil(py + fwhm)); y++) {
    for (let x = Math.max(0, Math.floor(px - fwhm)); x <= Math.min(w - 1, Math.ceil(px + fwhm)); x++) {
      if ((x - px) ** 2 + (y - py) ** 2 > rr) continue;
      const v = z[y * w + x] - half;
      if (v > 0) { sx += v * x; sy += v * y; sw += v; }
    }
  }
  return { x: sw > 0 ? sx / sw : px, y: sw > 0 ? sy / sw : py, snr: peak, seen: true };
}

/** Pixel-wise mean of noise-unit images, skipping no data; NaN where none has data. */
export function meanStack(images) {
  const n = images[0].length, out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let s = 0, k = 0;
    for (const im of images) if (!Number.isNaN(im[i])) { s += im[i]; k++; }
    // Mean of k images with noise 1 has noise 1/√k: scale back to noise units.
    out[i] = k ? (s / k) * Math.sqrt(k) : NaN;
  }
  return out;
}

/**
 * The trace picture: `backdrop` (0..1, NaN = no data) in dim grey, the fixed sky; then each found
 * copy of the star painted in its own colour from its own image (`z`, noise units) inside a soft
 * disc of `radius` pixels around (x, y). `stamps`: [{ z, x, y, radius, rgb: [r, g, b] }].
 */
export function traceRgba(backdrop, w, stamps, dim = 0.6) {
  const out = new Uint8ClampedArray(backdrop.length * 4);
  for (let i = 0; i < backdrop.length; i++) {
    const v = backdrop[i];
    if (Number.isNaN(v)) continue;
    const g = Math.round(v * 255 * dim);
    out[4 * i] = g; out[4 * i + 1] = g; out[4 * i + 2] = g; out[4 * i + 3] = 255;
  }
  for (const { z, x, y, radius, rgb, snr } of stamps) {
    const top = Math.max(5, snr);
    for (let yy = Math.max(0, Math.floor(y - radius)); yy <= Math.min(backdrop.length / w - 1, Math.ceil(y + radius)); yy++) {
      for (let xx = Math.max(0, Math.floor(x - radius)); xx <= Math.min(w - 1, Math.ceil(x + radius)); xx++) {
        const d = Math.hypot(xx - x, yy - y) / radius;
        const i = yy * w + xx;
        if (d >= 1 || Number.isNaN(z[i])) continue;
        const edge = d < 0.7 ? 1 : (1 - d) / 0.3;
        const a = Math.min(1, Math.max(0, z[i] / top)) ** 0.6 * edge;
        if (!(a > 0)) continue;
        for (let c = 0; c < 3; c++) out[4 * i + c] = Math.round(out[4 * i + c] * (1 - a) + rgb[c] * a);
        out[4 * i + 3] = 255;
      }
    }
  }
  return out;
}

/** Colour for a moment `t` (0 = oldest image, 1 = newest) on a red-amber-green-cyan ramp. */
export function timeColor(t) {
  const stops = [[255, 77, 61], [255, 179, 64], [150, 230, 90], [62, 232, 255]];
  const u = Math.min(1, Math.max(0, t)) * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(u)), f = u - i;
  return stops[i].map((v, c) => Math.round(v + (stops[i + 1][c] - v) * f));
}

// ---------- browser only ----------

// The API's PNG stretch (render_png in backend/planetx/spherex/cutouts.py): grey = asinh(s / k) /
// asinh(1 / k) with s the value scaled 0..1 between its black and white levels.
const PNG_SOFTENING = 0.1;

/** A PNG grey level (0..255) back to the linear value it was stretched from (0..1 between black and white). */
export function unstretch(grey) {
  return PNG_SOFTENING * Math.sinh((grey / 255) * Math.asinh(1 / PNG_SOFTENING));
}

/**
 * Float pixels (w×h, NaN = no data) resampled onto an n×n grid covering the same square of sky,
 * bilinear: { data, mask }. A grid pixel next to no data has no data.
 */
export function gridFromFloats(pixels, w, h, n) {
  const data = new Float32Array(n * n), mask = new Uint8Array(n * n);
  for (let r = 0; r < n; r++) {
    // Pixel centres line up: the grid's edges are the image's edges.
    const y = Math.min(h - 1, Math.max(0, ((r + 0.5) * h) / n - 0.5));
    const y0 = Math.min(Math.max(0, h - 2), Math.floor(y)), y1 = Math.min(h - 1, y0 + 1), fy = y - y0;
    for (let c = 0; c < n; c++) {
      const x = Math.min(w - 1, Math.max(0, ((c + 0.5) * w) / n - 0.5));
      const x0 = Math.min(Math.max(0, w - 2), Math.floor(x)), x1 = Math.min(w - 1, x0 + 1), fx = x - x0;
      // Neighbours with no weight are skipped, so a pixel exactly on good data next to no data stays.
      let v = 0;
      for (const [i, wt] of [[y0 * w + x0, (1 - fx) * (1 - fy)], [y0 * w + x1, fx * (1 - fy)], [y1 * w + x0, (1 - fx) * fy], [y1 * w + x1, fx * fy]]) {
        if (wt > 0) v += pixels[i] * wt;
      }
      if (Number.isFinite(v)) { data[r * n + c] = v; mask[r * n + c] = 1; }
    }
  }
  return { data, mask };
}

/**
 * Load an image (URL) onto an n×n grid: { data, mask (1 = has data) }. The images all cover the
 * same square of sky, so scaling each to n×n puts them on one grid. With `linear`, grey levels are
 * turned back into linear values (see unstretch), so noise and stars keep their real proportions.
 */
export async function loadGrid(src, n, { linear = false } = {}) {
  // createImageBitmap decodes off screen; HTMLImageElement.decode() stalls while the page is not
  // being drawn (a background tab).
  const img = await createImageBitmap(await (await fetch(src)).blob());
  const canvas = document.createElement('canvas');
  canvas.width = n; canvas.height = n;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(img, 0, 0, n, n);
  img.close();
  const px = ctx.getImageData(0, 0, n, n).data;
  const data = new Float32Array(n * n), mask = new Uint8Array(n * n);
  for (let i = 0; i < n * n; i++) {
    data[i] = linear ? unstretch(px[4 * i]) : px[4 * i];
    mask[i] = px[4 * i + 3] > 127 ? 1 : 0;
  }
  return { data, mask };
}

/** Distance from point (x, y) to the polyline `pts` ([{ x, y }]), in pixels. */
function toPath(x, y, pts) {
  if (pts.length === 1) return Math.hypot(x - pts[0].x, y - pts[0].y);
  let best = Infinity;
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i];
    const dx = b.x - a.x, dy = b.y - a.y, len = dx * dx + dy * dy;
    const t = len ? Math.min(1, Math.max(0, ((x - a.x) * dx + (y - a.y) * dy) / len)) : 0;
    best = Math.min(best, Math.hypot(x - a.x - t * dx, y - a.y - t * dy));
  }
  return best;
}

/**
 * Everything the trace view needs from images of one patch on an n×n grid. `items`:
 * [{ key, z (noise units, matched sharpness), cover (0..1 of the grid with data), at: { x, y }
 * (pixel where the catalogue puts the star on that image's date) }], oldest first. `fwhm` and
 * `maxShift` in pixels.
 *
 * The newest image that covers the patch is the reference. Every other image is lined up on it
 * using the fixed stars only (a band around the star's path is left out), then the star is looked
 * for near its catalogue spot. Returns { refKey, backdrop (0..1: all lined-up images averaged, the
 * sky that does not move), items: [{ key, shift: { dx, dy, ok, score }, z (lined up), found }] }.
 */
export function buildTrace(items, n, fwhm, maxShift) {
  const full = items.filter((it) => it.cover >= 0.9);
  const ref = full.length ? full[full.length - 1] : items.reduce((a, b) => (b.cover >= a.cover ? b : a));
  const path = items.map((it) => it.at);
  const exclude = new Uint8Array(n * n);
  const band = 2.5 * fwhm;
  for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) if (toPath(x, y, path) < band) exclude[y * n + x] = 1;
  const out = items.map((it) => {
    const shift = it === ref ? { dx: 0, dy: 0, score: 1, ok: true } : alignShift(ref.z, it.z, n, n, exclude, maxShift);
    const z = shiftImage(it.z, n, n, shift.dx, shift.dy);
    const found = locate(z, n, n, it.at.x, it.at.y, 1.3 * fwhm, fwhm);
    return { key: it.key, shift, z, found };
  });
  return { refKey: ref.key, backdrop: tone(meanStack(out.map((o) => o.z))), items: out };
}
