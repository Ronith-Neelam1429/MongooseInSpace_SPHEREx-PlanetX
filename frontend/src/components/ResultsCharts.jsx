// The Results page's charts, plain SVG in the site's style (see Spectrum.jsx): grey axes, the
// measured colour (--measured, cyan) for what SPHEREx measured, the text colour for references.
// Every chart has an aria-label that says what it shows; marks carry <title> tooltips.
import { useId, useMemo } from 'react';
import { shortName, speed } from '../lib/plain.js';
import {
  SPECTRAL_KINDS, edges, gaussianCurve, histogram, injectionHeatmap, mjdToDate, niceTicks, symlog,
} from '../lib/results.js';

const scale = ([d0, d1], [r0, r1]) => (v) => r0 + ((v - d0) / (d1 - d0)) * (r1 - r0);
// A count in words; a missing number (a result file without that field) shows as a dash.
const fmt = (x) => (Number.isFinite(x) ? x.toLocaleString('en-US') : '–');
const monthYear = (d) => d.toLocaleDateString('en-GB', { month: 'short', year: 'numeric', timeZone: 'UTC' });

/** Plot frame: the box, horizontal grid lines and tick labels. */
function Axes({ x, y, xTicks, yTicks, xLabel, yLabel, box, xFmt = String, yFmt = String }) {
  const [x0, x1] = box.x, [y0, y1] = box.y;
  return (
    <g className="rc-axes">
      <rect className="rc-plot" x={x0} y={y0} width={x1 - x0} height={y1 - y0} />
      {yTicks.map((v) => (
        <g key={`y${v}`}>
          <line className="rc-grid" x1={x0} x2={x1} y1={y(v)} y2={y(v)} />
          <text className="rc-tick" x={x0 - 6} y={y(v) + 3.5} textAnchor="end">{yFmt(v)}</text>
        </g>
      ))}
      {xTicks.map((v) => (
        <text key={`x${v}`} className="rc-tick" x={x(v)} y={y1 + 14} textAnchor="middle">{xFmt(v)}</text>
      ))}
      {xLabel && <text className="rc-tick rc-label" x={x1} y={y1 + 30} textAnchor="end">{xLabel}</text>}
      {yLabel && <text className="rc-tick rc-label" x={2} y={y0 - 6}>{yLabel}</text>}
    </g>
  );
}

// ---------- survey ----------

/** Patches finished in each of the last 24 hours, thin bars, the current hour last. */
export function HourBars({ hours }) {
  const W = 480, H = 120, box = { x: [30, 474], y: [12, 92] };
  const max = Math.max(1, ...hours.map((h) => h.done));
  const y = scale([0, max], [box.y[1], box.y[0]]);
  const step = (box.x[1] - box.x[0]) / hours.length;
  const total = hours.reduce((s, h) => s + h.done, 0);
  const ticks = niceTicks(0, max, 2).filter(Number.isInteger);
  return (
    <svg className="rc" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Patches finished per hour over the last 24 hours: ${total} in all, at most ${max} in one hour.`}>
      {ticks.map((v) => (
        <g key={v}>
          <line className="rc-grid" x1={box.x[0]} x2={box.x[1]} y1={y(v)} y2={y(v)} />
          <text className="rc-tick" x={box.x[0] - 6} y={y(v) + 3.5} textAnchor="end">{v}</text>
        </g>
      ))}
      {hours.map((h, i) => {
        const hh = h.start.getUTCHours().toString().padStart(2, '0');
        return (
          <g key={h.start.toISOString()}>
            <title>{`${hh}:00 UTC: ${h.done} finished${h.failed ? `, ${h.failed} failed` : ''}`}</title>
            <rect className="rc-hit" x={box.x[0] + i * step} y={box.y[0]} width={step} height={box.y[1] - box.y[0]} />
            {h.done > 0 && <rect className="rc-bar" x={box.x[0] + i * step + 1} y={y(h.done)} width={Math.max(1, step - 2)} height={box.y[1] - y(h.done)} />}
            {i % 6 === 0 && <text className="rc-tick" x={box.x[0] + i * step + step / 2} y={box.y[1] + 14} textAnchor="middle">{hh}h</text>}
          </g>
        );
      })}
      <text className="rc-tick" x={box.x[1]} y={box.y[1] + 14} textAnchor="end">now</text>
      <text className="rc-tick rc-label" x={box.x[1]} y={H - 2} textAnchor="end">hour (UTC)</text>
    </svg>
  );
}

/** Horizontal bars with a label and count each; used for candidates by class and fake-motion causes. */
export function BarList({ rows, label }) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <ul className="rc-barlist" aria-label={label}>
      {rows.map((r) => (
        <li key={r.key ?? r.cause}>
          <span className="rc-barlist-label">{r.label}</span>
          <span className="rc-barlist-track" aria-hidden="true"><span style={{ width: `${(100 * r.count) / max}%` }} /></span>
          <span className="rc-barlist-count tabular">{fmt(r.count)}</span>
          {r.sentence && <span className="rc-barlist-note">{r.sentence}</span>}
        </li>
      ))}
    </ul>
  );
}

// ---------- does SPHEREx agree with Gaia? ----------

const PM_TICKS = [0, 0.01, 0.1, 1, 10];
const pmTick = (v) => (v === 0 ? '0' : v < 1 ? String(v) : String(v));

/** SPHEREx's motion against Gaia's for every matched star, symlog axes, the y = x line. */
export function GaiaScatter({ points }) {
  const W = 480, H = 340, box = { x: [44, 470], y: [16, 296] };
  const c = 0.01;
  const top = symlog(Math.max(10, ...points.map((p) => Math.max(p.gaia, p.spherex))) * 1.2, c);
  const x = (v) => scale([0, top], box.x)(symlog(v, c));
  const y = (v) => scale([0, top], [box.y[1], box.y[0]])(symlog(v, c));
  const movers = points.filter((p) => p.mover).length;
  const clip = useId();
  return (
    <svg className="rc" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`Scatter of ${points.length} stars: speed measured by SPHEREx against speed from Gaia, both in arcseconds per year on stretched log scales. ${movers} were called moving by our engine.`}>
      <defs><clipPath id={clip}><rect x={box.x[0]} y={box.y[0]} width={box.x[1] - box.x[0]} height={box.y[1] - box.y[0]} /></clipPath></defs>
      <rect className="rc-band" x={box.x[0]} y={box.y[0]} width={x(0.05) - box.x[0]} height={box.y[1] - box.y[0]} />
      <text className="rc-tick rc-bandlabel" x={x(0.05) - 4} y={box.y[1] - 6} textAnchor="end">Gaia: still</text>
      <Axes x={x} y={y} xTicks={PM_TICKS} yTicks={PM_TICKS} box={box} xFmt={pmTick} yFmt={pmTick}
        xLabel="Gaia speed, ″/yr" yLabel="SPHEREx, ″/yr" />
      <g clipPath={`url(#${clip})`}>
        <line className="rc-ref" x1={x(0)} y1={y(0)} x2={x(1e3)} y2={y(1e3)} />
        {points.map((p, i) => (
          <circle key={i} className={p.mover ? 'rc-dot-on' : 'rc-dot-off'} cx={x(p.gaia)} cy={y(p.spherex)} r={p.mover ? 3 : 2.2}>
            <title>{`Gaia ${speed(p.gaia)}″/yr, SPHEREx ${speed(p.spherex)} ± ${speed(p.err)}″/yr${Number.isFinite(p.gMag) ? `, G ${p.gMag.toFixed(1)}` : ''}: ${p.mover ? 'called moving' : 'called still'}`}</title>
          </circle>
        ))}
      </g>
      <text className="rc-tick rc-reflabel" x={x(0.2) + 6} y={y(0.2) + 4}>same speed</text>
    </svg>
  );
}

/**
 * A histogram of `values` over [lo, hi), with an optional ideal curve (the unit Gaussian for pulls).
 * Values outside the range are counted in the caption by the caller.
 */
export function Histogram({ values, lo, hi, bins, xLabel, ticks, ideal = false, label }) {
  const W = 480, H = 220, box = { x: [40, 470], y: [14, 180] };
  const h = useMemo(() => histogram(values, lo, hi, bins), [values, lo, hi, bins]);
  const curve = ideal ? gaussianCurve(h.n, h.width, lo, hi) : null;
  const max = Math.max(1, ...h.bins.map((b) => b.count), ...(curve ? curve.map((p) => p.y) : []));
  const x = scale([lo, hi], box.x);
  const y = scale([0, max * 1.08], [box.y[1], box.y[0]]);
  const yTicks = niceTicks(0, max * 1.08, 3).filter(Number.isInteger);
  return (
    <svg className="rc" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label}>
      <Axes x={x} y={y} xTicks={ticks} yTicks={yTicks} box={box} xLabel={xLabel} yLabel="stars" />
      {h.bins.map((b) => (
        <g key={b.lo}>
          <title>{`${b.lo.toFixed(2)} to ${b.hi.toFixed(2)}: ${b.count}`}</title>
          <rect className="rc-hit" x={x(b.lo)} y={box.y[0]} width={x(b.hi) - x(b.lo)} height={box.y[1] - box.y[0]} />
          {b.count > 0 && <rect className="rc-bar" x={x(b.lo) + 1} y={y(b.count)} width={Math.max(1, x(b.hi) - x(b.lo) - 2)} height={box.y[1] - y(b.count)} />}
        </g>
      ))}
      <line className="rc-zero" x1={x(0)} x2={x(0)} y1={box.y[0]} y2={box.y[1]} />
      {curve && <polyline className="rc-ideal" points={curve.map((p) => `${x(p.x)},${y(p.y)}`).join(' ')} />}
    </svg>
  );
}

// ---------- what can we find? ----------

const magTick = (v) => (Number.isInteger(v) ? String(v) : v.toFixed(1));

/** Recovered fraction of planted movers by speed (log) and brightness, with the 50% and 90% lines. */
export function InjectionHeatmap({ doc }) {
  const m = useMemo(() => injectionHeatmap(doc), [doc]);
  const W = 480, H = 300, box = { x: [44, 470], y: [14, 256] };
  if (!m.speeds.length || !m.mags.length) return <p className="line dim">The grid is empty.</p>;
  const xe = edges(m.speeds, true), ye = edges(m.mags);
  const x = (v) => scale([Math.log(xe[0]), Math.log(xe[xe.length - 1])], box.x)(Math.log(v));
  // Bright (small magnitude) at the top.
  const y = scale([ye[0], ye[ye.length - 1]], box.y);
  const yIndex = new Map(m.mags.map((v, i) => [v, i]));
  const xIndex = new Map(m.speeds.map((v, i) => [v, i]));
  const path = (pts) => {
    const segs = [];
    let cur = [];
    for (const p of pts) {
      if (p.pm == null) { if (cur.length) segs.push(cur); cur = []; continue; }
      cur.push(`${x(Math.min(Math.max(p.pm, xe[0]), xe[xe.length - 1]))},${y(p.mag)}`);
    }
    if (cur.length) segs.push(cur);
    return segs.map((s) => (s.length === 1 ? `M${s[0]}h0.01` : `M${s.join('L')}`)).join(' ');
  };
  return (
    <svg className="rc" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`Heatmap of the share of planted moving stars we recovered, by speed from ${m.speeds[0]} to ${m.speeds[m.speeds.length - 1]} arcseconds a year and brightness from magnitude ${m.mags[0]} to ${m.mags[m.mags.length - 1]}. A solid line marks where half are found, a dashed line where nine in ten are.`}>
      {m.cells.map((c) => {
        const i = xIndex.get(c.pm), j = yIndex.get(c.mag);
        const f = c.fraction;
        return (
          <rect key={`${c.pm}|${c.mag}`} className={f == null ? 'rc-cell-none' : 'rc-cell'}
            x={x(xe[i]) + 1} y={y(ye[j]) + 1} width={x(xe[i + 1]) - x(xe[i]) - 2} height={y(ye[j + 1]) - y(ye[j]) - 2}
            style={f == null ? undefined : { fillOpacity: 0.06 + 0.94 * f }}>
            <title>{`${c.pm}″/yr, magnitude ${magTick(c.mag)}: ${f == null ? 'not measured' : `${Math.round(100 * f)}% found`} (${c.recovered ?? '–'} of ${c.injected ?? '–'})`}</title>
          </rect>
        );
      })}
      <path className="rc-line50" d={path(m.lines[50])} />
      <path className="rc-line90" d={path(m.lines[90])} />
      {m.speeds.map((v) => <text key={v} className="rc-tick" x={x(v)} y={box.y[1] + 14} textAnchor="middle">{v}</text>)}
      {m.mags.map((v) => <text key={v} className="rc-tick" x={box.x[0] - 6} y={y(v) + 3.5} textAnchor="end">{magTick(v)}</text>)}
      <text className="rc-tick rc-label" x={box.x[1]} y={box.y[1] + 30} textAnchor="end">speed, ″/yr (planted)</text>
      <text className="rc-tick rc-label" x={box.x[0] - 6} y={box.y[0] - 4} textAnchor="end">mag</text>
    </svg>
  );
}

// ---------- only SPHEREx can do this ----------

const SHAPES = {
  reference_star: (x, y) => <circle cx={x} cy={y} r="2.2" />,
  mover: (x, y) => <circle cx={x} cy={y} r="3.4" />,
  known_brown_dwarf: (x, y) => <rect x={x - 3.5} y={y - 3.5} width="7" height="7" transform={`rotate(45 ${x} ${y})`} />,
};
const kindClass = (k) => `rc-kind-${k}`;

/** A legend swatch drawn with the chart's own shape for that kind. */
export function KindSwatch({ kind }) {
  return <svg className={`rc-swatch ${kindClass(kind)}`} width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">{SHAPES[kind](6, 6)}</svg>;
}

/** Methane depth against water depth: ordinary stars sit near zero, brown dwarfs far from it. */
export function SpectralScatter({ points }) {
  const W = 480, H = 340, box = { x: [44, 470], y: [16, 296] };
  const all = points.flatMap((p) => [p.methane, p.water]);
  const lo = Math.min(-0.1, ...all) - 0.05, hi = Math.max(0.6, ...all) + 0.05;
  const x = scale([lo, hi], box.x), y = scale([lo, hi], [box.y[1], box.y[0]]);
  const ticks = [0, 0.2, 0.4, 0.6, 0.8].filter((v) => v >= lo && v <= hi);
  const DIP = 0.15;
  const named = points.filter((p) => p.kind === 'known_brown_dwarf' && p.name);
  return (
    <svg className="rc" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`Scatter of ${points.length} objects: depth of the methane dip against depth of the water dip in their SPHEREx spectra. Ordinary stars cluster near zero; known brown dwarfs sit in the upper right, where both dips are deep.`}>
      <rect className="rc-band" x={x(DIP)} y={box.y[0]} width={box.x[1] - x(DIP)} height={y(DIP) - box.y[0]} />
      <text className="rc-tick rc-bandlabel" x={box.x[1] - 6} y={y(DIP) + 12} textAnchor="end">↑ both dips: suggests a brown dwarf</text>
      <Axes x={x} y={y} xTicks={ticks} yTicks={ticks} box={box} xFmt={(v) => `${Math.round(v * 100)}%`} yFmt={(v) => `${Math.round(v * 100)}%`}
        xLabel="water dip depth" yLabel="methane" />
      <line className="rc-zero" x1={x(0)} x2={x(0)} y1={box.y[0]} y2={box.y[1]} />
      <line className="rc-zero" x1={box.x[0]} x2={box.x[1]} y1={y(0)} y2={y(0)} />
      {points.map((p, i) => (
        <g key={i} className={kindClass(p.kind)}>
          <title>{`${p.name ? shortName(p.name) : SPECTRAL_KINDS[p.kind].label.replace(/s$/, '').replace(/^Our /, '')}: methane ${Math.round(p.methane * 100)}%, water ${Math.round(p.water * 100)}%`}</title>
          {p.kind !== 'reference_star' && (p.methaneErr > 0 || p.waterErr > 0) && (
            <g className="rc-err">
              <line x1={x(p.water - p.waterErr)} x2={x(p.water + p.waterErr)} y1={y(p.methane)} y2={y(p.methane)} />
              <line x1={x(p.water)} x2={x(p.water)} y1={y(p.methane - p.methaneErr)} y2={y(p.methane + p.methaneErr)} />
            </g>
          )}
          {SHAPES[p.kind](x(p.water), y(p.methane))}
        </g>
      ))}
      {/* Names right of their marker, or left of it in the right part of the plot so they stay inside. */}
      {named.map((p) => {
        const left = x(p.water) > box.x[0] + 0.6 * (box.x[1] - box.x[0]);
        return (
          <text key={p.name} className="rc-tick rc-name" x={x(p.water) + (left ? -8 : 8)} y={y(p.methane) + 3} textAnchor={left ? 'end' : 'start'}>
            {shortName(p.name)}
          </text>
        );
      })}
    </svg>
  );
}

/** R Dra's brightness by date (top) or folded on its period (phase). */
export function LightCurveChart({ curve, folded = false }) {
  const W = 480, H = 240, box = { x: [52, 470], y: [14, 196] };
  const pts = folded ? curve.folded : curve.points;
  const xs = folded ? pts.map((p) => p.phase) : pts.map((p) => p.mjd);
  const xd = folded ? [0, 1] : [Math.min(...xs) - 10, Math.max(...xs) + 10];
  const top = Math.max(...pts.map((p) => p.flux + p.err)) * 1.1 || 1; // all-zero fluxes still get an axis
  const x = scale(xd, box.x), y = scale([0, top], [box.y[1], box.y[0]]);
  const yTicks = niceTicks(0, top, 4);
  let xTicks;
  if (folded) xTicks = [0, 0.25, 0.5, 0.75, 1];
  else {
    const first = mjdToDate(xd[0]);
    xTicks = [];
    for (let d = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 1)); d <= mjdToDate(xd[1]); d = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 3, 1))) {
      xTicks.push(d.getTime() / 864e5 + 40587);
    }
  }
  const label = folded
    ? `R Draconis brightness folded on a ${curve.period}-day period: the visits from different passes line up into one cycle.`
    : `R Draconis brightness relative to its average, measured by SPHEREx on ${pts.length} dates; it changes by a factor of ${curve.ratio?.toFixed(1)}.`;
  // Fluxes are relative: each point divided by its own wavelength's mean level (the file's notes).
  return (
    <svg className="rc" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label}>
      <Axes x={x} y={y} xTicks={xTicks} yTicks={yTicks} box={box}
        xFmt={folded ? (v) => v.toFixed(2) : (v) => monthYear(mjdToDate(v))} yFmt={(v) => fmt(v)}
        xLabel={folded ? `phase (period ${Math.round(curve.period)} days)` : 'date'} yLabel="× average" />
      {pts.map((p, i) => (
        <g key={i} className="rc-pt">
          <title>{`${mjdToDate(p.mjd).toISOString().slice(0, 10)}: ${p.flux.toFixed(2)} ± ${p.err.toFixed(2)} × average${Number.isFinite(p.wavelength) ? ` at ${p.wavelength.toFixed(2)} µm` : ''}`}</title>
          {p.err > 0 && <line x1={x(folded ? p.phase : p.mjd)} x2={x(folded ? p.phase : p.mjd)} y1={y(p.flux - p.err)} y2={y(p.flux + p.err)} />}
          <circle cx={x(folded ? p.phase : p.mjd)} cy={y(p.flux)} r="2.6" />
        </g>
      ))}
    </svg>
  );
}

// ---------- the precision headline ----------

/**
 * Barnard's Star's speed on one number line: SPHEREx alone (wide bar), with older surveys (a sliver)
 * and Gaia (a tick); below, the same zoomed 60 times so the sliver can be seen.
 */
export function PrecisionStrip({ p, gain }) {
  const W = 480, H = 176;
  const rows = [
    { key: 'wide', domain: [7.5, 13.5], ticks: [8, 9, 10, 11, 12, 13], y: 44, label: 'full view' },
    { key: 'zoom', domain: [10.39 - 0.05, 10.39 + 0.05].map((v) => +v.toFixed(2)), ticks: [10.35, 10.37, 10.39, 10.41, 10.43], y: 128, label: `zoomed ${Math.round(gain / 10) * 10}×` },
  ];
  const X = [18, 462];
  const clip = useId();
  return (
    <svg className="rc rc-precision" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`Barnard's Star's speed: SPHEREx alone ${p.spherex.pm} plus or minus ${p.spherex.err} arcseconds a year; with older surveys ${p.fused.pm.toFixed(2)} plus or minus ${p.fused.err}; Gaia ${p.gaia.pm.toFixed(2)}.`}>
      <defs><clipPath id={clip}><rect x={X[0]} y="0" width={X[1] - X[0]} height={H} /></clipPath></defs>
      {rows.map((r) => {
        const x = scale(r.domain, X);
        return (
          <g key={r.key}>
            <text className="rc-tick rc-label" x={X[0]} y={r.y - 26}>{r.label}</text>
            <line className="rc-grid" x1={X[0]} x2={X[1]} y1={r.y + 16} y2={r.y + 16} />
            {r.ticks.map((t) => (
              <g key={t}>
                <line className="rc-grid" x1={x(t)} x2={x(t)} y1={r.y + 13} y2={r.y + 19} />
                <text className="rc-tick" x={x(t)} y={r.y + 31} textAnchor="middle">{t}</text>
              </g>
            ))}
            <g clipPath={`url(#${clip})`}>
              <g className="rc-interval rc-interval-wide">
                <title>{`${p.spherex.label}: ${p.spherex.pm} ± ${p.spherex.err}″/yr`}</title>
                <rect x={x(p.spherex.pm - p.spherex.err)} y={r.y - 12} width={x(p.spherex.pm + p.spherex.err) - x(p.spherex.pm - p.spherex.err)} height="8" />
              </g>
              <g className="rc-interval rc-interval-fused">
                <title>{`${p.fused.label}: ${p.fused.pm.toFixed(3)} ± ${p.fused.err}″/yr`}</title>
                <rect x={x(p.fused.pm - p.fused.err)} y={r.y} width={Math.max(1.5, x(p.fused.pm + p.fused.err) - x(p.fused.pm - p.fused.err))} height="8" />
              </g>
              <line className="rc-gaia" x1={x(p.gaia.pm)} x2={x(p.gaia.pm)} y1={r.y - 16} y2={r.y + 12}>
                <title>{`Gaia DR3: ${p.gaia.pm.toFixed(3)}″/yr`}</title>
              </line>
            </g>
          </g>
        );
      })}
      <text className="rc-tick rc-label" x={X[1]} y={H - 2} textAnchor="end">speed, ″/yr</text>
    </svg>
  );
}
