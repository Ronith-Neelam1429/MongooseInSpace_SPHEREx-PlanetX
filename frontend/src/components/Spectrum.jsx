import { useEffect, useMemo, useRef, useState } from 'react';
import { API_BASE, runSpectrum } from '../lib/services.js';
import { X_TICKS, featureText, progressText, spectrumChart } from '../lib/spectrum.js';
import './Spectrum.css';

const W = 360;
const PLOT_X = [44, 352];
const PLOT_Y = [28, 186];
const H = 212;
const FORMULA = { CH4: 'CH₄', H2O: 'H₂O' };

function fluxLabel(v) {
  if (v === 0) return '0';
  const a = Math.abs(v);
  return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(0) : a >= 1 ? v.toFixed(1) : v.toPrecision(2);
}

/** Flux against wavelength (log axis), the absorption windows shaded behind it. */
function SpectrumChart({ result }) {
  const m = useMemo(() => spectrumChart(result), [result]);
  const lx = (v) => Math.log(v);
  const sx = (v) => PLOT_X[0] + ((lx(v) - lx(m.x[0])) / (lx(m.x[1]) - lx(m.x[0]))) * (PLOT_X[1] - PLOT_X[0]);
  const sy = (v) => PLOT_Y[0] + ((m.y[1] - v) / (m.y[1] - m.y[0])) * (PLOT_Y[1] - PLOT_Y[0]);
  const summary = m.bands.map((b) => `${b.wavelength.toFixed(2)} µm: ${fluxLabel(b.flux)} mJy`).join('; ');
  return (
    <div className="sp-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Spectrum: brightness in mJy against wavelength. ${summary}.`}>
        <defs>
          <clipPath id="sp-clip"><rect x={PLOT_X[0]} y={PLOT_Y[0]} width={PLOT_X[1] - PLOT_X[0]} height={PLOT_Y[1] - PLOT_Y[0]} /></clipPath>
        </defs>
        <rect className="sp-plot" x={PLOT_X[0]} y={PLOT_Y[0]} width={PLOT_X[1] - PLOT_X[0]} height={PLOT_Y[1] - PLOT_Y[0]} />
        {m.features.map((f) => (
          <g key={f.key} className={`sp-window sp-${f.molecule} ${f.detected ? 'sp-seen' : ''}`}>
            <title>{`${f.label}${f.detected ? ': absorption seen' : f.detected === false ? ': no clear absorption' : ': not measured'}`}</title>
            <rect x={sx(f.lo)} y={PLOT_Y[0]} width={sx(f.hi) - sx(f.lo)} height={PLOT_Y[1] - PLOT_Y[0]} />
          </g>
        ))}
        {m.features.map((f, i) => (
          <text key={f.key} className={`sp-wlabel sp-${f.molecule}-text ${f.detected ? 'sp-seen' : ''}`} x={(sx(f.lo) + sx(f.hi)) / 2} y={PLOT_Y[0] - 4 - (i % 2) * 10} textAnchor="middle">
            {FORMULA[result.features[i].molecule] || f.molecule}
          </text>
        ))}
        {m.yTicks.map((v) => (
          <g key={v}>
            <line className={v === 0 ? 'sp-zero' : 'sp-grid'} x1={PLOT_X[0]} x2={PLOT_X[1]} y1={sy(v)} y2={sy(v)} />
            <text className="sp-tick" x={PLOT_X[0] - 6} y={sy(v) + 3.5} textAnchor="end">{fluxLabel(v)}</text>
          </g>
        ))}
        {X_TICKS.map((v) => (
          <text key={v} className="sp-tick" x={sx(v)} y={PLOT_Y[1] + 13} textAnchor="middle">{v}</text>
        ))}
        <text className="sp-tick" x={PLOT_X[1]} y={H - 2} textAnchor="end">wavelength, µm</text>
        <text className="sp-tick" x={4} y={10}>mJy</text>
        <g clipPath="url(#sp-clip)">
          {m.points.map((p, i) => (
            <circle key={i} className={`sp-exposure ${p.outlier ? 'sp-outlier' : ''}`} cx={sx(p.wavelength)} cy={sy(p.flux)} r="1.6" />
          ))}
          {m.bands.map((b) => (
            <g key={b.wavelength} className="sp-band">
              <title>{`${b.lo.toFixed(3)}-${b.hi.toFixed(3)} µm: ${fluxLabel(b.flux)} ± ${fluxLabel(b.err)} mJy (${b.n} exposure${b.n === 1 ? '' : 's'})`}</title>
              <line x1={sx(b.wavelength)} x2={sx(b.wavelength)} y1={sy(b.flux - b.err)} y2={sy(b.flux + b.err)} />
              <circle cx={sx(b.wavelength)} cy={sy(b.flux)} r="2.4" />
            </g>
          ))}
        </g>
      </svg>
    </div>
  );
}

/** What the spectrum suggests, then each absorption band's depth. */
function SpectrumResult({ result }) {
  const n = result.counts;
  const tried = n.ok + n.saturated + n.masked + n.off_image;
  return (
    <>
      <p className={`sp-summary ${result.suggestion === 'brown_dwarf' ? 'measured' : ''}`}>{result.summary}</p>
      {result.bands.length > 0 && <SpectrumChart result={result} />}
      <p className="sp-note">
        {result.bands.length} of 102 SPHEREx bands from {n.ok} of {tried} exposures
        {n.saturated > 0 && ` · ${n.saturated} saturated`}. Large dots: bands; small dots: single exposures.
      </p>
      <details className="more">
        <summary>Absorption bands</summary>
        <dl className="kv sp-features">
          {result.features.map((f) => (
            <div key={f.key} className="sp-feature">
              <dt>{f.label}</dt>
              <dd className={f.detected ? 'measured' : ''}>{featureText(f)}</dd>
            </div>
          ))}
        </dl>
        <p className="sp-note">
          Depth against a straight line (in log flux and log wavelength) through the flux peaks either side;
          a dip counts when it is at least 15% deep and 3× its error. Methane together with water suggests a
          cool brown dwarf; it does not prove one.
        </p>
        {result.notes.map((t) => <p key={t} className="sp-note">{t}</p>)}
      </details>
    </>
  );
}

/** The SPHEREx spectrum of one object, measured when asked (it fetches tens of exposures). */
export default function Spectrum({ target }) {
  const key = `${target.ra.toFixed(6)},${target.dec.toFixed(6)},${target.pmra ?? 0},${target.pmdec ?? 0}`;
  // State belongs to one object: a card that switches to another object starts idle again.
  const [held, setHeld] = useState({ key, status: 'idle' });
  const state = held.key === key ? held : { status: 'idle' };
  const ctrl = useRef(null);

  // Leaving the object (or the card) stops polling; the backend drops a run nobody polls.
  useEffect(() => () => ctrl.current?.abort(), [key]);

  const start = () => {
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    const set = (s) => { if (!c.signal.aborted) setHeld({ key, ...s }); };
    set({ status: 'loading', job: null });
    runSpectrum(target, { signal: c.signal, onProgress: (job) => set({ status: 'loading', job }) })
      .then((result) => set({ status: 'ok', result }))
      .catch((err) => set({ status: 'error', error: err?.message || String(err) }));
  };

  if (!API_BASE) return <p className="state">The spectrum needs the PlanetX API, which is not connected.</p>;
  if (state.status === 'idle') {
    return (
      <div className="sp-start">
        <button type="button" className="sp-button" onClick={start}>Show spectrum</button>
        <p className="sp-note">
          Measures this object in about 70 SPHEREx exposures, each at its own wavelength from 0.75 to 5 µm.
          About a minute the first time.
        </p>
      </div>
    );
  }
  if (state.status === 'loading') {
    return <p className="state" role="status"><span className="ticker" aria-hidden="true" />{progressText(state.job)}</p>;
  }
  if (state.status === 'error') {
    return (
      <p className="state err" role="alert">
        The spectrum could not be measured: {state.error}.{' '}
        <button type="button" className="textbtn" onClick={start}>Retry</button>
      </p>
    );
  }
  return <SpectrumResult result={state.result} />;
}
