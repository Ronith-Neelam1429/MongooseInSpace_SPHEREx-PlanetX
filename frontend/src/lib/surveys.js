// Sky backgrounds (HiPS). The first is SPHEREx itself: IRSA's DR1 all-sky cube, 102 spectral
// frames from 0.75 to 5.0 µm. Others list mirrors (IRSA, ESA, CDS); the first that answers wins.

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

export const SURVEYS = [
  {
    id: 'spherex', label: 'SPHEREx', cube: true,
    urls: ['https://irsa.ipac.caltech.edu/data/hips/SPHEREx/spherex_allsky_cube'],
  },
  {
    id: 'allwise', label: 'AllWISE',
    urls: [
      'https://irsa.ipac.caltech.edu/data/hips/CDS/AllWISE/RGB-W4-W2-W1',
      'https://skies.esac.esa.int/AllWISEColor',
      'https://alasky.cds.unistra.fr/AllWISE/RGB-W4-W2-W1',
    ],
  },
  {
    id: '2mass', label: '2MASS',
    urls: [
      'https://irsa.ipac.caltech.edu/data/hips/CDS/2MASS/Color',
      'https://alasky.cds.unistra.fr/2MASS/Color',
      'https://alaskybis.cds.unistra.fr/2MASS/Color',
    ],
  },
  {
    id: 'dss', label: 'DSS2',
    urls: [
      'https://irsa.ipac.caltech.edu/data/hips/CDS/DSS2/color',
      'https://skies.esac.esa.int/DSSColor',
      'https://alasky.cds.unistra.fr/DSS/DSSColor',
    ],
  },
];

export const surveyById = (id) => SURVEYS.find((s) => s.id === id) || SURVEYS[0];

const resolved = new Map();

/** Resolve a survey to the first mirror whose `properties` answers (cached). */
export async function resolveSurveyUrl(id) {
  const s = surveyById(id);
  if (resolved.has(s.id)) return resolved.get(s.id);
  for (const url of s.urls) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 6000);
    try {
      const res = await fetch(`${url}/properties`, { signal: ctrl.signal });
      if (res.ok) {
        resolved.set(s.id, url);
        return url;
      }
    } catch {
      // try the next mirror
    } finally {
      clearTimeout(t);
    }
  }
  throw new Error(`${s.label} imagery is unreachable`);
}
