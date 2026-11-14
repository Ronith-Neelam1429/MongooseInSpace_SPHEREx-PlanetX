// Aladin overlays for the selected region: query circle, SIMBAD markers, Gaia stars + proper-motion arrows.
import { propagate } from './coords.js';

const NICE = [1, 2, 5];

/** Pick a "nice" number of years so the fastest arrows are a readable fraction of the region. */
export function arrowYears(stars, radiusDeg) {
  const pms = stars.map((s) => Math.hypot(s.pmra || 0, s.pmdec || 0)).filter((v) => v > 0).sort((a, b) => a - b);
  if (!pms.length) return null;
  const ref = Math.max(pms[pms.length - 1], 20); // fastest star, mas/yr
  const target = (radiusDeg * 3.6e6 * 0.3) / ref; // years for ref star to cover 30% of the radius
  const exp = Math.floor(Math.log10(target));
  let best = 1;
  for (let e = exp - 1; e <= exp + 1; e++) {
    for (const n of NICE) {
      const v = n * 10 ** e;
      if (v <= target && v >= 1) best = Math.max(best, v);
    }
  }
  return best;
}

export function createOverlays(A, aladin) {
  const region = A.graphicOverlay({ name: 'Selected region', color: '#ffd166', lineWidth: 2 });
  const arrowsSlow = A.graphicOverlay({ name: 'Gaia PM arrows', color: 'rgba(120, 200, 255, 0.75)', lineWidth: 1.2 });
  const arrowsFast = A.graphicOverlay({ name: 'Gaia PM arrows (fast)', color: '#ff5c8a', lineWidth: 2.5 });
  const gaia = A.catalog({ name: 'Gaia DR3', shape: 'circle', sourceSize: 7, color: 'rgba(120, 200, 255, 0.9)', onClick: 'showTable' });
  const simbad = A.catalog({ name: 'SIMBAD', shape: 'square', sourceSize: 12, color: '#7cf29c', onClick: 'showTable' });
  const focus = A.catalog({ name: 'Focus', shape: 'cross', sourceSize: 22, color: '#ffffff' });
  [region, arrowsSlow, arrowsFast].forEach((o) => aladin.addOverlay(o));
  [gaia, simbad, focus].forEach((c) => aladin.addCatalog(c));

  return {
    setRegion(ra, dec, radiusDeg) {
      region.removeAll();
      region.add(A.circle(ra, dec, radiusDeg, { color: '#ffd166', lineWidth: 2 }));
    },
    setSimbad(objects) {
      simbad.clear();
      simbad.addSources(objects.map((o) => A.source(o.ra, o.dec, {
        name: o.name, type: o.type, mag: o.mag ?? '', ra: o.ra.toFixed(6), dec: o.dec.toFixed(6),
      })));
    },
    setGaia(stars, years) {
      gaia.clear();
      arrowsSlow.removeAll();
      arrowsFast.removeAll();
      gaia.addSources(stars.map((s) => A.source(s.ra, s.dec, {
        source_id: s.id, G: s.gmag ?? '', pmRA: s.pmra ?? '', pmDE: s.pmdec ?? '', plx: s.plx ?? '',
      })));
      if (!years) return;
      for (const s of stars) {
        if (s.pmra == null || s.pmdec == null) continue;
        const pm = Math.hypot(s.pmra, s.pmdec);
        const end = propagate(s.ra, s.dec, s.pmra, s.pmdec, years);
        (pm >= 100 ? arrowsFast : arrowsSlow).add(A.vector(s.ra, s.dec, end.ra, end.dec));
      }
    },
    setFocus(ra, dec) {
      focus.clear();
      if (ra != null) focus.addSources([A.source(ra, dec, {})]);
    },
    showSimbad(v) { if (v) simbad.show(); else simbad.hide(); },
    showGaia(v) {
      for (const o of [gaia, arrowsSlow, arrowsFast]) { if (v) o.show(); else o.hide(); }
    },
    clear() {
      region.removeAll(); arrowsSlow.removeAll(); arrowsFast.removeAll();
      gaia.clear(); simbad.clear(); focus.clear();
    },
  };
}
