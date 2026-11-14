// Canvas layer drawn over Aladin for the selected field: Gaia DR3 stars sized and shaded by
// G magnitude, their proper motion animated forward in time from epoch 2016.0, SIMBAD objects,
// the query circle, hover labels and the focus marker. Everything is projected with Aladin's own
// world2pix, so it stays registered to the sky while panning and zooming.
import { offsetPoint, propagate } from './coords.js';

export const FAST_PM = 100; // mas/yr; above this a star is drawn in the motion colour
const SIGNAL = [255, 77, 46];
const PERIOD_MS = 7000;
const HOLD = 0.12; // fraction of the cycle spent resting at the end
const HIT_PX = 9;

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2);

/** Disc radius (css px) and opacity for a G magnitude: bright stars larger and more opaque. */
export function starStyle(g) {
  if (g == null || !Number.isFinite(g)) return { r: 0.8, a: 0.35 };
  return { r: clamp(4.4 - 0.27 * (g - 7), 0.7, 4.4), a: clamp(1.05 - 0.055 * (g - 9), 0.22, 1) };
}

export class StarField {
  constructor(canvas, aladin, { onHover } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.aladin = aladin;
    this.onHover = onHover;
    this.gaia = [];
    this.simbad = [];
    this.region = null;
    this.years = 0;
    this.focus = null;
    this.hover = null;
    this.pointer = null;
    this.show = { gaia: true, simbad: true };
    this.playing = !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    this.t0 = performance.now();
    this.pausedPhase = 1;
    this.viewKey = '';
    this.projected = [];
    this.raf = 0;
    this.font = '500 11px "IBM Plex Mono", ui-monospace, monospace';
    this.resizeObs = new ResizeObserver(() => this.resize());
    this.resizeObs.observe(canvas.parentElement);
    this.resize();
    this.loop = this.loop.bind(this);
    this.raf = requestAnimationFrame(this.loop);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.resizeObs.disconnect();
  }

  resize() {
    const el = this.canvas.parentElement;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.w = el.clientWidth;
    this.h = el.clientHeight;
    this.canvas.width = Math.round(this.w * dpr);
    this.canvas.height = Math.round(this.h * dpr);
    this.canvas.style.width = `${this.w}px`;
    this.canvas.style.height = `${this.h}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.viewKey = '';
  }

  setRegion(region) { this.region = region; this.viewKey = ''; }

  setGaia(stars, years) {
    this.years = years || 0;
    // Faint first so bright stars are painted on top.
    this.gaia = stars.map((s) => {
      const pm = s.pmra != null && s.pmdec != null ? Math.hypot(s.pmra, s.pmdec) : 0;
      const end = pm && this.years ? propagate(s.ra, s.dec, s.pmra, s.pmdec, this.years) : null;
      return { s, pm, end, ...starStyle(s.gmag) };
    }).sort((a, b) => (b.s.gmag ?? 99) - (a.s.gmag ?? 99));
    this.t0 = performance.now();
    this.viewKey = '';
  }

  setSimbad(objects) { this.simbad = objects; this.viewKey = ''; }
  setFocus(obj) { this.focus = obj; }
  setVisible(show) { this.show = { ...this.show, ...show }; }

  setPlaying(on) {
    if (on === this.playing) return;
    if (on) this.t0 = performance.now() - this.phaseTime() * PERIOD_MS;
    else this.pausedPhase = 1;
    this.playing = on;
  }

  phaseTime() { return ((performance.now() - this.t0) % PERIOD_MS) / PERIOD_MS; }

  /** Animation progress 0..1 (eased, with a rest at the end); 1 when paused. */
  progress() {
    if (!this.playing) return this.pausedPhase;
    const p = this.phaseTime();
    return p > 1 - HOLD ? 1 : ease(p / (1 - HOLD));
  }

  /** Years after epoch 2016.0 currently shown. */
  currentYears() { return this.years * this.progress(); }

  setPointer(p) {
    this.pointer = p;
    const hit = p ? this.hitTest(p.x, p.y) : null;
    const key = hit ? `${hit.kind}:${hit.kind === 'gaia' ? hit.obj.id : hit.obj.name}` : null;
    if (key !== this.hoverKey) {
      this.hoverKey = key;
      this.hover = hit;
      this.onHover?.(hit);
    }
  }

  hitTest(x, y) {
    let best = null;
    let bestD = HIT_PX * HIT_PX;
    for (const p of this.projected) {
      const dx = p.x - x, dy = p.y - y;
      const d = dx * dx + dy * dy;
      if (d <= bestD) { bestD = d; best = p; }
    }
    return best ? { kind: best.kind, obj: best.obj } : null;
  }

  project(ra, dec) {
    try {
      const p = this.aladin.world2pix(ra, dec);
      if (!p || !Number.isFinite(p[0]) || !Number.isFinite(p[1])) return null;
      return p;
    } catch {
      return null;
    }
  }

  /** Re-project catalogue positions only when the view changes. */
  reproject() {
    const [ra, dec] = this.aladin.getRaDec();
    const fov = this.aladin.getFov()[0];
    const key = `${ra.toFixed(7)},${dec.toFixed(7)},${fov.toFixed(7)},${this.w},${this.h}`;
    if (key === this.viewKey) return;
    this.viewKey = key;
    const pad = 40;
    const inView = (p) => p && p[0] > -pad && p[1] > -pad && p[0] < this.w + pad && p[1] < this.h + pad;
    this.cache = this.gaia.map((g) => {
      const a = this.project(g.s.ra, g.s.dec);
      if (!inView(a)) return null;
      const b = g.end ? this.project(g.end.ra, g.end.dec) : null;
      return { g, a, b };
    }).filter(Boolean);
    this.simbadPx = this.simbad.map((o) => ({ o, p: this.project(o.ra, o.dec) })).filter((x) => inView(x.p));
    this.regionPx = null;
    if (this.region) {
      const pts = [];
      for (let i = 0; i <= 96; i++) {
        const q = offsetPoint(this.region.ra, this.region.dec, this.region.radius, (i / 96) * 360);
        pts.push(this.project(q.ra, q.dec));
      }
      this.regionPx = pts;
    }
  }

  loop() {
    this.raf = requestAnimationFrame(this.loop);
    const { ctx } = this;
    ctx.clearRect(0, 0, this.w, this.h);
    if (!this.region && !this.gaia.length && !this.simbad.length) { this.projected = []; return; }
    this.reproject();
    const k = this.progress();
    const projected = [];

    if (this.regionPx) this.drawRegion();

    if (this.show.gaia && this.cache) {
      // Trails first (under stars), batched: slow stars in one faint path.
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.strokeStyle = 'rgba(255,255,255,0.28)';
      ctx.lineWidth = 1;
      const fast = [];
      for (const c of this.cache) {
        if (!c.b) continue;
        const x = c.a[0] + (c.b[0] - c.a[0]) * k, y = c.a[1] + (c.b[1] - c.a[1]) * k;
        c.x = x; c.y = y;
        if (Math.hypot(x - c.a[0], y - c.a[1]) < 1.5) continue;
        if (c.g.pm >= FAST_PM) { fast.push(c); continue; }
        ctx.moveTo(c.a[0], c.a[1]);
        ctx.lineTo(x, y);
      }
      ctx.stroke();
      for (const c of fast) this.drawFastTrail(c, k);

      // Stars, binned by opacity to keep state changes low.
      const bins = new Map();
      for (const c of this.cache) {
        const x = c.b ? c.x : c.a[0], y = c.b ? c.y : c.a[1];
        const isFast = c.g.pm >= FAST_PM;
        const key = isFast ? 'fast' : Math.round(c.g.a * 10);
        if (!bins.has(key)) bins.set(key, []);
        bins.get(key).push([x, y, c.g.r]);
        projected.push({ kind: 'gaia', obj: c.g.s, x, y });
      }
      for (const [key, list] of bins) {
        ctx.beginPath();
        for (const [x, y, r] of list) { ctx.moveTo(x + r, y); ctx.arc(x, y, r, 0, Math.PI * 2); }
        ctx.fillStyle = key === 'fast' ? `rgb(${SIGNAL})` : `rgba(255,255,255,${key / 10})`;
        ctx.fill();
      }
    }

    if (this.show.simbad && this.simbadPx) {
      ctx.beginPath();
      ctx.strokeStyle = 'rgba(255,255,255,0.8)';
      ctx.lineWidth = 1;
      for (const { o, p } of this.simbadPx) {
        const x = Math.round(p[0]) + 0.5, y = Math.round(p[1]) + 0.5;
        ctx.rect(x - 4, y - 4, 8, 8);
        projected.push({ kind: 'simbad', obj: o, x: p[0], y: p[1] });
      }
      ctx.stroke();
    }

    this.projected = projected;
    if (this.focus) this.drawFocus();
    if (this.hover) this.drawLabel(this.hover);
  }

  drawRegion() {
    const { ctx } = this;
    ctx.save();
    ctx.beginPath();
    let pen = false;
    for (const p of this.regionPx) {
      if (!p) { pen = false; continue; }
      if (pen) ctx.lineTo(p[0], p[1]); else ctx.moveTo(p[0], p[1]);
      pen = true;
    }
    ctx.setLineDash([2, 5]);
    ctx.strokeStyle = 'rgba(255,255,255,0.55)';
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.restore();
  }

  drawFastTrail(c, k) {
    const { ctx } = this;
    const [x0, y0] = c.a;
    const grad = ctx.createLinearGradient(x0, y0, c.x, c.y);
    grad.addColorStop(0, `rgba(${SIGNAL},0)`);
    grad.addColorStop(1, `rgba(${SIGNAL},0.9)`);
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(c.x, c.y);
    ctx.strokeStyle = grad;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    // Ring at the catalogue (2016.0) position so the start point stays readable.
    ctx.beginPath();
    ctx.arc(x0, y0, Math.max(2.5, c.g.r + 1.5), 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(${SIGNAL},${0.35 + 0.4 * (1 - k)})`;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  pixelOf(obj) {
    const hit = this.projected.find((p) => p.obj === obj);
    if (hit) return [hit.x, hit.y];
    return this.project(obj.ra, obj.dec);
  }

  drawFocus() {
    const p = this.pixelOf(this.focus);
    if (!p) return;
    const { ctx } = this;
    const [x, y] = [Math.round(p[0]) + 0.5, Math.round(p[1]) + 0.5];
    const s = 11, l = 5;
    ctx.beginPath();
    for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      ctx.moveTo(x + sx * s, y + sy * (s - l));
      ctx.lineTo(x + sx * s, y + sy * s);
      ctx.lineTo(x + sx * (s - l), y + sy * s);
    }
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1.25;
    ctx.stroke();
  }

  drawLabel(hit) {
    const p = this.pixelOf(hit.obj);
    if (!p) return;
    const o = hit.obj;
    const text = hit.kind === 'simbad'
      ? `${o.name}${o.type ? `  ${o.type}` : ''}`
      : `G ${o.gmag != null ? o.gmag.toFixed(1) : '–'}  µ ${o.pmra != null ? Math.hypot(o.pmra, o.pmdec).toFixed(1) : '–'} mas/yr`;
    const { ctx } = this;
    ctx.font = this.font;
    const w = ctx.measureText(text).width + 12;
    let x = p[0] + 12, y = p[1] - 22;
    if (x + w > this.w - 8) x = p[0] - 12 - w;
    if (y < 8) y = p[1] + 10;
    ctx.fillStyle = '#000';
    ctx.fillRect(x, y, w, 18);
    ctx.strokeStyle = 'rgba(255,255,255,0.35)';
    ctx.lineWidth = 1;
    ctx.strokeRect(Math.round(x) + 0.5, Math.round(y) + 0.5, Math.round(w) - 1, 17);
    ctx.fillStyle = '#fff';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, x + 6, y + 9.5);
  }
}

/** Pick a "nice" span of years so the fastest star travels about half the region radius. */
export function arrowYears(stars, radiusDeg) {
  let max = 0;
  for (const s of stars) {
    if (s.pmra != null && s.pmdec != null) max = Math.max(max, Math.hypot(s.pmra, s.pmdec));
  }
  if (!max) return null;
  const target = (radiusDeg * 3.6e6 * 0.5) / Math.max(max, 20);
  const exp = Math.floor(Math.log10(target));
  let best = 1;
  for (let e = exp - 1; e <= exp + 1; e++) {
    for (const n of [1, 2, 5]) {
      const v = n * 10 ** e;
      if (v <= target && v >= 1) best = Math.max(best, v);
    }
  }
  return best;
}
