import { parseCoords } from './coords.js';

/** Read ?ra=&dec=&fov= (also accepts ?target=<name or coords>). */
export function readViewFromUrl(search = window.location.search) {
  const p = new URLSearchParams(search);
  const ra = parseFloat(p.get('ra'));
  const dec = parseFloat(p.get('dec'));
  const fov = parseFloat(p.get('fov'));
  const view = {};
  if (Number.isFinite(ra) && Number.isFinite(dec) && ra >= 0 && ra < 360 && Math.abs(dec) <= 90) {
    view.ra = ra;
    view.dec = dec;
  } else if (p.get('target')) {
    const c = parseCoords(p.get('target'));
    if (c) Object.assign(view, c);
    else view.name = p.get('target');
  }
  if (Number.isFinite(fov) && fov > 0 && fov <= 360) view.fov = fov;
  return view;
}

export function viewToSearch({ ra, dec, fov }, existing = window.location.search) {
  const p = new URLSearchParams(existing);
  p.delete('target');
  p.set('ra', ra.toFixed(5));
  p.set('dec', dec.toFixed(5));
  p.set('fov', fov < 1 ? fov.toFixed(4) : fov.toFixed(2));
  return `?${p}`;
}

let pending = null;
/** Debounced history.replaceState so the URL always restores the current view. */
export function writeViewToUrl(view) {
  clearTimeout(pending);
  pending = setTimeout(() => {
    const next = viewToSearch(view);
    if (next !== window.location.search) {
      window.history.replaceState(null, '', `${window.location.pathname}${next}${window.location.hash}`);
    }
  }, 250);
}
