// The sky background (HiPS): SPHEREx itself, IRSA's DR1 all-sky cube, 102 spectral frames from
// 0.75 to 5.0 µm.

/** Wavelength (µm) of each SPHEREx DR1 cube frame; frame = 17 * (band - 1) + (channel - 1). */
export const SPHEREX_WAVELENGTHS = [
  0.753, 0.771, 0.790, 0.809, 0.828, 0.848, 0.868, 0.889, 0.910, 0.932, 0.955, 0.978, 1.001, 1.025, 1.050, 1.075, 1.102,
  1.113, 1.139, 1.167, 1.195, 1.224, 1.254, 1.284, 1.315, 1.346, 1.378, 1.412, 1.446, 1.481, 1.517, 1.554, 1.592,
  1.631, 1.656, 1.695, 1.735, 1.775, 1.818, 1.861, 1.905, 1.949, 1.995, 2.041, 2.089, 2.139, 2.189, 2.240, 2.292,
  2.345, 2.399, 2.445, 2.506, 2.575, 2.646, 2.719, 2.794, 2.871, 2.951, 3.033, 3.118, 3.204, 3.293, 3.383, 3.476,
  3.570, 3.667, 3.768, 3.833, 3.867, 3.901, 3.935, 3.970, 4.005, 4.040, 4.076, 4.112, 4.148, 4.184, 4.220, 4.257,
  4.294, 4.331, 4.368, 4.405, 4.435, 4.467, 4.500, 4.533, 4.566, 4.600, 4.635, 4.669, 4.704, 4.739, 4.773, 4.808,
  4.843, 4.879, 4.915, 4.951, 4.988,
];
// Frames 50 and 51 are the dichroic channels (band 3 ch 17, band 4 ch 1) and are blank in DR1.
const BLANK = new Set([50, 51]);
export const SPHEREX_FRAMES = SPHEREX_WAVELENGTHS.map((um, frame) => ({ frame, um, band: Math.floor(frame / 17) + 1 }))
  .filter((f) => !BLANK.has(f.frame));
export const DEFAULT_FRAME = 33; // 1.63 µm (band 2), close to the classic H band

export const SPHEREX_SKY = {
  label: 'SPHEREx',
  urls: ['https://irsa.ipac.caltech.edu/data/hips/SPHEREx/spherex_allsky_cube'],
};

let resolved = null;

/** The first SPHEREx HiPS mirror whose `properties` answers (cached once found). */
export async function resolveSkyUrl() {
  if (resolved) return resolved;
  for (const url of SPHEREX_SKY.urls) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 6000);
    try {
      const res = await fetch(`${url}/properties`, { signal: ctrl.signal });
      if (res.ok) {
        resolved = url;
        return url;
      }
    } catch {
      // try the next mirror
    } finally {
      clearTimeout(t);
    }
  }
  throw new Error(`${SPHEREX_SKY.label} imagery is unreachable`);
}

/** Waits (ms) before each new try at the sky imagery: IRSA often answers 502/503 for a moment. */
export const SKY_RETRY_MS = [2000, 5000, 10000];

/**
 * Run `task` (a function returning a promise) until it succeeds, waiting `delays[i]` ms before
 * try i + 2. `onFailure(err, willRetry)` hears every failure, `onSuccess(value)` the first
 * success. Returns a function that cancels what is still pending (no callback runs after it).
 */
export function retrying(task, delays, { onSuccess, onFailure }) {
  let cancelled = false;
  let timer = 0;
  const attempt = (n) => {
    Promise.resolve().then(task).then(
      (value) => { if (!cancelled) onSuccess(value); },
      (err) => {
        if (cancelled) return;
        const willRetry = n < delays.length;
        onFailure(err, willRetry);
        if (willRetry) timer = setTimeout(() => attempt(n + 1), delays[n]);
      },
    );
  };
  attempt(0);
  return () => { cancelled = true; clearTimeout(timer); };
}
