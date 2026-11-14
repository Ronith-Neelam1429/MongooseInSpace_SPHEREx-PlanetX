// HiPS backgrounds. Each survey lists mirrors (IRSA first, then ESA, then CDS); the first mirror
// whose `properties` file answers is used, so one data centre being down does not blank the map.
export const SURVEYS = [
  {
    id: 'allwise', label: 'AllWISE (IR color)',
    urls: [
      'https://irsa.ipac.caltech.edu/data/hips/CDS/AllWISE/RGB-W4-W2-W1',
      'https://skies.esac.esa.int/AllWISEColor',
      'https://alasky.cds.unistra.fr/AllWISE/RGB-W4-W2-W1',
    ],
  },
  {
    id: '2mass', label: '2MASS (near-IR color)',
    urls: [
      'https://irsa.ipac.caltech.edu/data/hips/CDS/2MASS/Color',
      'https://alasky.cds.unistra.fr/2MASS/Color',
      'https://alaskybis.cds.unistra.fr/2MASS/Color',
    ],
  },
  {
    id: 'dss', label: 'DSS2 (optical color)',
    urls: [
      'https://irsa.ipac.caltech.edu/data/hips/CDS/DSS2/color',
      'https://skies.esac.esa.int/DSSColor',
      'https://alasky.cds.unistra.fr/DSS/DSSColor',
    ],
  },
];

const resolved = new Map();

/** Resolve a survey id to the first reachable mirror URL (cached). */
export async function resolveSurveyUrl(id) {
  const s = SURVEYS.find((x) => x.id === id) || SURVEYS[0];
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
      // try next mirror
    } finally {
      clearTimeout(t);
    }
  }
  return s.urls[0];
}
