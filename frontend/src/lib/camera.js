// Camera motion for the Aladin view: smooth fly-to between positions and the idle globe spin.
import { separation } from './coords.js';

const D2R = Math.PI / 180;
const R2D = 180 / Math.PI;
const MAX_FOV = 180;

export const prefersReducedMotion = () => !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

const toVec = (ra, dec) => [Math.cos(dec * D2R) * Math.cos(ra * D2R), Math.cos(dec * D2R) * Math.sin(ra * D2R), Math.sin(dec * D2R)];
const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);

/**
 * Path from one view to another: great-circle interpolation of the centre and logarithmic
 * interpolation of the field of view, widening mid-flight when the hop is long compared with
 * both fields (so the sky doesn't smear past at high zoom). Returns { at(t), duration } with t in [0, 1].
 */
export function flightPath(from, to) {
  const sep = separation(from.ra, from.dec, to.ra, to.dec);
  const a = toVec(from.ra, from.dec), b = toVec(to.ra, to.dec);
  const omega = sep * D2R;
  const lf0 = Math.log(from.fov), lf1 = Math.log(to.fov);
  const peak = Math.log(Math.min(MAX_FOV, Math.max(from.fov, to.fov, sep * 1.6)));
  const lift = Math.max(0, peak - Math.max(lf0, lf1));
  const zoomSpan = Math.abs(lf1 - lf0) / Math.LN10; // decades of zoom
  const duration = Math.min(2.6, 0.55 + sep / 70 + zoomSpan * 0.32 + lift * 0.18);

  const at = (t) => {
    const e = ease(Math.max(0, Math.min(1, t)));
    let ra = to.ra, dec = to.dec;
    if (omega > 1e-9 && e < 1) {
      const s = Math.sin(omega);
      const wa = Math.sin((1 - e) * omega) / s, wb = Math.sin(e * omega) / s;
      const v = [0, 1, 2].map((i) => wa * a[i] + wb * b[i]);
      ra = ((Math.atan2(v[1], v[0]) * R2D) % 360 + 360) % 360;
      dec = Math.asin(Math.max(-1, Math.min(1, v[2] / Math.hypot(...v)))) * R2D;
    }
    const fov = Math.exp(lf0 + (lf1 - lf0) * e + lift * Math.sin(Math.PI * e));
    return { ra, dec, fov: Math.min(MAX_FOV, fov) };
  };
  return { at, duration };
}

/** Drives Aladin's camera; one flight or spin at a time, cancelled by any user gesture. */
export class Camera {
  constructor(aladin, el) {
    this.aladin = aladin;
    this.raf = 0;
    this.mode = null;
    const interrupt = () => this.stop();
    for (const ev of ['pointerdown', 'wheel', 'touchstart']) el.addEventListener(ev, interrupt, { passive: true, capture: true });
  }

  stop() {
    cancelAnimationFrame(this.raf);
    this.mode = null;
    this.onDone = null;
  }

  view() {
    const [ra, dec] = this.aladin.getRaDec();
    return { ra: ((ra % 360) + 360) % 360, dec, fov: this.aladin.getFov()[0] };
  }

  set({ ra, dec, fov }) {
    this.aladin.gotoRaDec(ra, dec);
    if (fov != null) this.aladin.setFoV(fov);
  }

  /** Fly to (ra, dec, fov); `onDone` runs when the camera arrives (not if interrupted). */
  flyTo(target, onDone) {
    this.stop();
    if (prefersReducedMotion()) { this.set(target); onDone?.(); return; }
    const path = flightPath(this.view(), target);
    const t0 = performance.now();
    this.mode = 'fly';
    this.onDone = onDone;
    const tick = (now) => {
      const t = (now - t0) / (path.duration * 1000);
      this.set(t >= 1 ? target : path.at(t));
      if (t < 1) { this.raf = requestAnimationFrame(tick); return; }
      const done = this.onDone;
      this.mode = null;
      this.onDone = null;
      done?.();
    };
    this.raf = requestAnimationFrame(tick);
  }

  /** Slowly turn the globe eastward until the user interacts. */
  spin(degPerSec = 3.5) {
    this.stop();
    if (prefersReducedMotion()) return;
    let last = performance.now();
    this.mode = 'spin';
    const tick = (now) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      const { ra, dec } = this.view();
      this.aladin.gotoRaDec((ra + degPerSec * dt) % 360, dec);
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }
}
