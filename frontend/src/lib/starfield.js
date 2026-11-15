// Canvas layer drawn over Aladin for the selected field: Gaia DR3 stars sized and shaded by
// G magnitude, SIMBAD objects, the motion engine's detections, the query circle, name labels and
// the hover / selection markers. Stars rest where they are today; on request their catalogued
// proper motion is played forward from epoch 2016.0. Everything is projected with Aladin's own
// world2pix, so it stays registered to the sky while panning and zooming.
//
// Each marker is coloured by its class (lib/classes.js); a filter switches classes off, which
// fades their stars to faint context dots and takes them, and their labels, out of picking.
import { decimalYear, offsetPoint, propagate } from './coords.js';
import { CLASS, MEASURED, OTHER, SHOW_ALL, SIGNAL, VARIABLE, engineClass, gaiaClass, simbadClass } from './classes.js';

export { measuredMotion } from './plain.js';
// What the motion engine measured in SPHEREx frames; red stays Gaia's catalogued motion.
export { MEASURED };
const GAIA_EPOCH = 2016.0;
const PERIOD_MS = 7000;
const HOLD = 0.12; // fraction of the cycle spent resting at the end

// Picking. Markers that matter get a larger target: engine results, then labelled and fast
// objects, then other SIMBAD entries. A plain field star must be pointed at directly, so a click
// that misses in a crowded field picks nothing rather than some faint star.
const RANK = { engine: 0, notable: 1, simbad: 2, gaia: 3 };
const HIT_RADIUS = [22, 16, 12, 6]; // css px, by rank
const HIT_CORE = 6; // markers this close to the pointer all count as "under" it

const LABEL_H = 18;
const LABEL_GAP = 2;
// Offsets from the marker to the label's near corner: right or left of it, above or below.
const LABEL_SPOTS = [[12, -22], [12, 5], [-12, -22], [-12, 5]];

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2);

/** Disc radius (css px) and opacity for a G magnitude: bright stars larger and more opaque. */
export function starStyle(g) {
  if (g == null || !Number.isFinite(g)) return { r: 0.8, a: 0.35 };
  return { r: clamp(4.4 - 0.27 * (g - 7), 0.7, 4.4), a: clamp(1.05 - 0.055 * (g - 9), 0.22, 1) };
}

/**
 * The marker a pointer at (x, y) picks, or null. `markers` are { x, y, rank } with rank 0 the most
 * notable. Among markers right under the pointer the most notable wins; otherwise the nearest,
 * measured against each marker's own hit radius, so a notable object a little further away still
 * beats a faint field star, but pointing straight at a faint star picks that star.
 */
export function pickMarker(markers, x, y) {
  let best = null;
  let bestScore = Infinity;
  let bestD = Infinity;
  for (const m of markers) {
    const d = Math.hypot(m.x - x, m.y - y);
    const r = HIT_RADIUS[m.rank];
    if (d > r) continue;
    const score = Math.max(0, d - HIT_CORE) / r;
    const wins = score < bestScore
      || (score === bestScore && (m.rank < best.rank || (m.rank === best.rank && d < bestD)));
    if (wins) { best = m; bestScore = score; bestD = d; }
  }
  return best;
}

/**
 * Where to put a label of width `w` next to the marker at `p`: the first of four spots around it
 * that stays inside `bounds` ({ w, h }) and clear of the `placed` boxes. Returns { x, y, w, h },
 * or null when every spot is taken.
 */
export function placeLabel(p, w, placed, bounds) {
  for (const [dx, dy] of LABEL_SPOTS) {
    const x = dx > 0 ? p.x + dx : p.x + dx - w;
    const y = p.y + dy;
    if (x < 4 || y < 4 || x + w > bounds.w - 4 || y + LABEL_H > bounds.h - 4) continue;
    const hits = (b) => x < b.x + b.w + LABEL_GAP && x + w + LABEL_GAP > b.x && y < b.y + b.h + LABEL_GAP && y + LABEL_H + LABEL_GAP > b.y;
    if (!placed.some(hits)) return { x, y, w, h: LABEL_H };
  }
  return null;
}

const LABEL_STYLE = {
  known: { bg: 'rgba(0,0,0,0.78)', fg: 'rgba(255,255,255,0.88)' },
  measured: { bg: 'rgba(0,0,0,0.78)', fg: `rgb(${MEASURED})` },
  hover: { bg: '#000', fg: '#fff', border: 'rgba(255,255,255,0.5)' },
  focus: { bg: '#f4f4f2', fg: '#000' },
};

export class StarField {
  /**
   * `onHover(hit)` reports the marker under the pointer ({ kind, obj } or null) when it changes;
   * `describe(kind, obj)` gives the text of its hover label.
   */
  constructor(canvas, aladin, { onHover, describe } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.aladin = aladin;
    this.onHover = onHover;
    this.describe = describe;
    this.gaia = [];
    this.simbad = [];
    this.motion = [];
    this.notable = [];
    this.notableObjs = new Set();
    this.region = null;
    this.years = 0;
    this.nowK = 0;
    this.focus = null;
    this.highlight = null;
    this.hover = null;
    this.pointer = null;
    this.show = { gaia: true, simbad: true, engine: true };
    this.filter = SHOW_ALL;
    this.classOf = null; // (kind 'gaia' | 'simbad', obj) => class id; see setClassifier
    this.playing = false;
    this.t0 = performance.now();
    this.viewKey = '';
    this.markers = [];
    this.labelBoxes = [];
    this.textWidths = new Map();
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
    // Where today falls in the played span [2016.0, 2016.0 + years]; the stars rest there.
    this.nowK = this.years ? (decimalYear() - GAIA_EPOCH) / this.years : 0;
    // Faint first so bright stars are painted on top.
    this.gaia = stars.map((s) => {
      const pm = s.pmra != null && s.pmdec != null ? Math.hypot(s.pmra, s.pmdec) : 0;
      const end = pm && this.years ? propagate(s.ra, s.dec, s.pmra, s.pmdec, this.years) : null;
      return { s, pm, end, cls: this.classify('gaia', s), ...starStyle(s.gmag) };
    }).sort((a, b) => (b.s.gmag ?? 99) - (a.s.gmag ?? 99));
    this.t0 = performance.now();
    this.viewKey = '';
  }

  setSimbad(objects) { this.simbad = objects; this.viewKey = ''; }

  /**
   * How to classify catalogue markers: `classOf('gaia' | 'simbad', obj)` returns a class id. The
   * app passes one that knows which Gaia star is which SIMBAD object, so a Gaia dot on a galaxy
   * is drawn as a galaxy. Without one, Gaia stars are fast or plain and SIMBAD goes by its type.
   */
  setClassifier(classOf) {
    this.classOf = classOf;
    for (const g of this.gaia) g.cls = this.classify('gaia', g.s);
    this.viewKey = '';
  }

  classify(kind, obj) {
    if (kind === 'engine') return engineClass(obj);
    const c = this.classOf?.(kind, obj);
    if (c && CLASS[c]) return c;
    return kind === 'gaia' ? gaiaClass(obj) : simbadClass(obj);
  }

  /** Which classes to show: { [class id]: boolean }; missing ids count as shown. */
  setFilter(filter) { this.filter = { ...SHOW_ALL, ...filter }; }
  shown(cls) { return this.filter[cls] !== false; }
  /** Engine objects to mark: movers, candidates and variables (see MotionObject in the API). */
  setMotion(objects) { this.motion = objects; this.viewKey = ''; }
  /**
   * Objects that always carry a name label and get a larger hit target, most important first:
   * [{ kind, obj, ra, dec, text, tone }] with tone 'measured' (engine) or 'known' (catalogue).
   */
  setNotable(list) {
    this.notable = list;
    this.notableObjs = new Set(list.map((n) => n.obj));
  }
  /** The selected object: { kind, obj, ra, dec, text } (ra/dec are used when its marker is not drawn), or null. */
  setFocus(target) { this.focus = target; }
  /** An object pointed at from outside the map (a list row), marked like a hovered one. */
  setHighlight(target) { this.highlight = target; }
  setVisible(show) { this.show = { ...this.show, ...show }; }

  /** Play the catalogued motion from 2016.0 over the span, looping; when off the stars rest at today. */
  setPlaying(on) {
    if (on === this.playing) return;
    if (on) this.t0 = performance.now();
    this.playing = on;
  }

  /** Fraction of the span shown: eased 0..1 with a rest at the end while playing, today's otherwise. */
  progress() {
    if (!this.playing) return this.nowK;
    const p = ((performance.now() - this.t0) % PERIOD_MS) / PERIOD_MS;
    return p > 1 - HOLD ? 1 : ease(p / (1 - HOLD));
  }

  /** The year currently shown. */
  currentYear() { return GAIA_EPOCH + this.years * this.progress(); }

  setPointer(p) {
    this.pointer = p;
    const hit = p ? this.hitTest(p.x, p.y) : null;
    if (hit?.obj !== this.hover?.obj) {
      this.hover = hit && { ...hit, text: this.describe?.(hit.kind, hit.obj) };
      this.onHover?.(hit);
    }
  }

  /** The object at canvas position (x, y): a name label under the pointer, else the best marker. */
  hitTest(x, y) {
    for (const b of this.labelBoxes) {
      if (x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) return { kind: b.kind, obj: b.obj };
    }
    const m = pickMarker(this.markers, x, y);
    return m ? { kind: m.kind, obj: m.obj } : null;
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
    this.simbadPx = this.simbad.map((o) => ({ o, p: this.project(o.ra, o.dec), cls: this.classify('simbad', o) })).filter((x) => inView(x.p));
    this.motionPx = this.motion.map((o) => {
      const pts = o.track.map((t) => this.project(t.ra, t.dec));
      return { o, pts, last: pts[pts.length - 1], cls: engineClass(o) };
    }).filter((m) => m.pts.every(Boolean) && inView(m.last));
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
    if (!this.region && !this.gaia.length && !this.simbad.length && !this.motion.length) {
      this.markers = [];
      this.labelBoxes = [];
      return;
    }
    this.reproject();
    const k = this.progress();
    const markers = [];
    const rank = (kind, obj, fast) => (kind === 'engine' ? RANK.engine : fast || this.notableObjs.has(obj) ? RANK.notable : RANK[kind]);
    // With an object selected, everything else steps back.
    const rest = this.focus ? 0.45 : 1;

    if (this.regionPx) this.drawRegion();

    ctx.globalAlpha = rest;
    if (this.show.gaia && this.cache) {
      // With ordinary stars filtered out, each fast star shows its full predicted path instead.
      const paths = !this.shown('star');
      const showFast = this.shown('fast');
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
        if (c.g.cls === 'fast') { if (showFast) fast.push(c); continue; }
        if (!this.shown(c.g.cls)) continue;
        ctx.moveTo(c.a[0], c.a[1]);
        ctx.lineTo(x, y);
      }
      ctx.stroke();
      if (paths) for (const c of fast) this.drawPath(c);
      for (const c of fast) this.drawFastTrail(c, k);

      // Stars, binned by colour and opacity to keep state changes low. Plain stars are white,
      // shaded by brightness; every other class is drawn in its own colour.
      const bins = new Map();
      const faded = [];
      for (const c of this.cache) {
        const x = c.b ? c.x : c.a[0], y = c.b ? c.y : c.a[1];
        const { cls } = c.g;
        if (!this.shown(cls)) { faded.push([x, y, c.g.r]); continue; }
        const key = cls === 'star' ? Math.round(c.g.a * 10) : cls;
        if (!bins.has(key)) bins.set(key, []);
        bins.get(key).push([x, y, cls === 'star' ? c.g.r : Math.max(c.g.r, 1.6)]);
        markers.push({ kind: 'gaia', obj: c.g.s, x, y, rank: rank('gaia', c.g.s, cls === 'fast') });
      }
      if (faded.length) {
        ctx.beginPath();
        for (const [x, y, r] of faded) { ctx.moveTo(x + r, y); ctx.arc(x, y, r, 0, Math.PI * 2); }
        ctx.fillStyle = 'rgba(255,255,255,0.12)';
        ctx.fill();
      }
      for (const [key, list] of bins) {
        ctx.beginPath();
        for (const [x, y, r] of list) { ctx.moveTo(x + r, y); ctx.arc(x, y, r, 0, Math.PI * 2); }
        ctx.fillStyle = typeof key === 'number' ? `rgba(255,255,255,${key / 10})` : `rgb(${CLASS[key].rgb})`;
        ctx.fill();
      }
    }

    if (this.show.simbad && this.simbadPx) {
      // Squares, one path per class colour; a filtered-out class is not drawn at all.
      const byClass = new Map();
      for (const e of this.simbadPx) {
        if (!this.shown(e.cls)) continue;
        if (!byClass.has(e.cls)) byClass.set(e.cls, []);
        byClass.get(e.cls).push(e);
      }
      ctx.lineWidth = 1;
      for (const [cls, list] of byClass) {
        ctx.beginPath();
        ctx.strokeStyle = cls === 'star' ? 'rgba(255,255,255,0.8)' : `rgb(${CLASS[cls].rgb})`;
        for (const { o, p } of list) {
          const x = Math.round(p[0]) + 0.5, y = Math.round(p[1]) + 0.5;
          ctx.rect(x - 4, y - 4, 8, 8);
          markers.push({ kind: 'simbad', obj: o, x: p[0], y: p[1], rank: rank('simbad', o, cls === 'fast') });
        }
        ctx.stroke();
      }
    }

    if (this.show.engine && this.motionPx) {
      for (const m of this.motionPx) {
        if (!this.shown(m.cls)) continue;
        ctx.globalAlpha = m.o === this.focus?.obj ? 1 : rest;
        this.drawMeasured(m);
        markers.push({ kind: 'engine', obj: m.o, x: m.last[0], y: m.last[1], rank: RANK.engine });
      }
    }

    this.markers = markers;
    let pointed = this.hover || this.highlight;
    if (pointed?.obj === this.focus?.obj) pointed = null;
    const labelled = this.drawNotable(pointed, rest);
    ctx.globalAlpha = 1;
    if (pointed) this.drawPointed(pointed, labelled);
    if (this.focus) this.drawFocus();
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

  /** Full predicted track (catalogue epoch to end of the span), dashed. */
  drawPath(c) {
    const { ctx } = this;
    ctx.save();
    ctx.setLineDash([2, 4]);
    ctx.beginPath();
    ctx.moveTo(c.a[0], c.a[1]);
    ctx.lineTo(c.b[0], c.b[1]);
    ctx.strokeStyle = `rgba(${SIGNAL},0.45)`;
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
    ctx.strokeStyle = `rgba(${SIGNAL},${0.35 + 0.4 * (1 - clamp(k, 0, 1))})`;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  /**
   * One engine object: its measured per-pass positions joined in time order, ending in a ring at
   * the latest pass. Candidates are dashed; variables get a diamond.
   */
  drawMeasured({ o, pts, last, cls }) {
    const { ctx } = this;
    const [x, y] = last;
    const colour = `rgb(${cls === 'other' ? OTHER : MEASURED})`;
    ctx.save();
    ctx.strokeStyle = colour;
    ctx.fillStyle = colour;
    ctx.lineWidth = 1.25;
    if (o.kind !== 'static') {
      if (o.kind === 'candidate') ctx.setLineDash([3, 3]);
      ctx.beginPath();
      pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(x, y, 9, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      for (const p of pts) { ctx.moveTo(p[0] + 1.5, p[1]); ctx.arc(p[0], p[1], 1.5, 0, Math.PI * 2); }
      ctx.fill();
    }
    if (o.is_variable) {
      const r = o.kind === 'static' ? 8 : 14;
      if (cls !== 'other') ctx.strokeStyle = `rgb(${VARIABLE})`;
      ctx.beginPath();
      ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y);
      ctx.closePath();
      ctx.stroke();
    }
    ctx.restore();
  }

  /** Canvas position of a target: its drawn marker when there is one, else its own coordinates. */
  pixelOf(target) {
    const m = this.markers.find((p) => p.obj === target.obj);
    if (m) return [m.x, m.y];
    return target.ra != null ? this.project(target.ra, target.dec) : null;
  }

  textWidth(text) {
    let w = this.textWidths.get(text);
    if (w === undefined) {
      if (this.textWidths.size > 400) this.textWidths.clear();
      this.ctx.font = this.font;
      w = this.ctx.measureText(text).width + 12;
      this.textWidths.set(text, w);
    }
    return w;
  }

  drawLabel(box, text, { bg, fg, border }) {
    const { ctx } = this;
    ctx.fillStyle = bg;
    ctx.fillRect(box.x, box.y, box.w, LABEL_H);
    if (border) {
      ctx.strokeStyle = border;
      ctx.lineWidth = 1;
      ctx.strokeRect(Math.round(box.x) + 0.5, Math.round(box.y) + 0.5, Math.round(box.w) - 1, LABEL_H - 1);
    }
    ctx.font = this.font;
    ctx.fillStyle = fg;
    ctx.textBaseline = 'middle';
    ctx.fillText(text, box.x + 6, box.y + 9.5);
  }

  /**
   * Name labels of the notable objects, most important first; one that would overlap an earlier
   * label is left out. The boxes double as hit targets, so the label of the `pointed` object stays
   * where it is and only lights up (returns true when it did). The selected object is skipped: it
   * gets its own label.
   */
  drawNotable(pointed, alpha) {
    const boxes = [];
    const bounds = { w: this.w, h: this.h };
    let lit = false;
    for (const n of this.notable) {
      if (!this.show[n.kind] || n.obj === this.focus?.obj || !this.shown(this.classify(n.kind, n.obj))) continue;
      const p = this.pixelOf(n);
      if (!p || p[0] < 0 || p[1] < 0 || p[0] > this.w || p[1] > this.h) continue;
      const box = placeLabel({ x: p[0], y: p[1] }, this.textWidth(n.text), boxes, bounds);
      if (!box) continue;
      const isPointed = n.obj === pointed?.obj;
      this.ctx.globalAlpha = isPointed ? 1 : alpha;
      this.drawLabel(box, n.text, isPointed ? LABEL_STYLE.hover : LABEL_STYLE[n.tone]);
      boxes.push({ ...box, kind: n.kind, obj: n.obj });
      lit = lit || isPointed;
    }
    this.labelBoxes = boxes;
    return lit;
  }

  /** A label that must show: beside the marker, pulled back inside the canvas. */
  anchoredLabel(p, text) {
    const w = this.textWidth(text);
    let x = p[0] + 14, y = p[1] - 24;
    if (x + w > this.w - 8) x = p[0] - 14 - w;
    if (y < 8) y = p[1] + 8;
    return { x: clamp(x, 4, Math.max(4, this.w - w - 4)), y, w };
  }

  /**
   * The hovered marker, or the one a list row points at: a ring, and its name unless its own
   * label is already lit (`labelled`).
   */
  drawPointed(target, labelled) {
    const p = this.pixelOf(target);
    if (!p) return;
    const { ctx } = this;
    ctx.beginPath();
    ctx.arc(p[0], p[1], 13, 0, Math.PI * 2);
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1.5;
    ctx.stroke();
    if (!labelled && target.text) this.drawLabel(this.anchoredLabel(p, target.text), target.text, LABEL_STYLE.hover);
  }

  /** The selected object: corner brackets and its name on white, like the selected list row. */
  drawFocus() {
    const p = this.pixelOf(this.focus);
    if (!p) return;
    const { ctx } = this;
    const [x, y] = [Math.round(p[0]) + 0.5, Math.round(p[1]) + 0.5];
    const s = 17, l = 7;
    ctx.beginPath();
    for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      ctx.moveTo(x + sx * s, y + sy * (s - l));
      ctx.lineTo(x + sx * s, y + sy * s);
      ctx.lineTo(x + sx * (s - l), y + sy * s);
    }
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.stroke();
    if (this.focus.text) this.drawLabel(this.anchoredLabel([p[0] + 6, p[1] - 4], this.focus.text), this.focus.text, LABEL_STYLE.focus);
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
