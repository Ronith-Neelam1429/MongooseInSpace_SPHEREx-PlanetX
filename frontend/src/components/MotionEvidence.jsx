import { useMemo } from 'react';
import {
  brightnessSeries, dayMonthTime, dayMonthYear, monthYear, motionChart, passSpan, proofTally, ticks,
} from '../lib/evidence.js';
import { useChecks, useEvidence, useVerdict } from './useEvidence.js';
import { engineViews } from '../lib/pictures.js';
import { VERDICT_HINT, motionVerdict } from '../lib/plain.js';
import SkyPictures from './SkyPictures.jsx';
import './MotionEvidence.css';

// Marks on the single exposures, in arcsec on the sky. The ring is wider than SPHEREx's PSF, so
// it frames the object instead of covering it.
const RING_ARCSEC = 9;
const REF_TICK_ARCSEC = [13, 20];
// Detector bands from the SPHEREx archive (docs/API.md), in microns.
const BAND_RANGE = { D1: '0.75–1.1 µm', D2: '1.1–1.65 µm', D3: '1.6–2.4 µm', D4: '2.4–3.85 µm', D5: '3.8–4.4 µm', D6: '4.4–5.0 µm' };
const RERUN_STAGE = {
  queued: 'waiting for the engine', listing: 'listing exposures', analysing: 'measuring motion',
  checking: 'checking archival catalogues',
};

const signed = (v, digits = 1) => `${v < 0 ? '−' : '+'}${Math.abs(v).toFixed(digits)}`;

function rerunText(job) {
  const stage = job.stage === 'downloading' ? `fetching frames ${job.frames_done}/${job.frames_total}` : RERUN_STAGE[job.stage] || 'working';
  return `The engine no longer holds this field. Running it again: ${stage}`;
}

function bandText(band) {
  const [detector, kind] = band.split('-');
  const range = BAND_RANGE[detector] ? `${detector} · ${BAND_RANGE[detector]}` : detector;
  return kind ? `${range} (halo of the saturated star)` : range;
}


const STATUS = {
  pass: { mark: '✓', label: 'Passed' },
  weak: { mark: '~', label: 'Partly' },
  fail: { mark: '✗', label: 'Failed' },
  unknown: { mark: '–', label: 'Cannot tell' },
};

/** The verdict on whether the motion is real, as a short tag (see motionVerdict in lib/plain.js). */
export function VerdictTag({ mv, label }) {
  return <span className={`badge mv-badge mv-${mv.tone}`} title={VERDICT_HINT[mv.tone]}>{label || mv.label}</span>;
}

/** Why: the backend's reason, the likely cause of a fake (or the suspected one), and what decided it. */
export function VerdictReason({ mv, className = 'line' }) {
  const cause = mv.causeText && mv.verdict !== 'moving'
    ? `${mv.verdict === 'fake_motion' ? 'Most likely cause' : 'Suspected problem'}: ${mv.causeText}.`
    : null;
  return (
    <p className={`${className} mv-reason`}>
      {mv.reason}
      {cause && <> <span className="mv-cause">{cause}</span></>}
      {mv.decidedBy && <span className="mv-decided"> Decided by {mv.decidedBy}.</span>}
    </p>
  );
}

/**
 * Independent tests that the motion is real and belongs to one object, each with its numbers.
 * `verdict` (motionVerdict of the evidence) is the backend's verdict after these tests.
 */
export function Proof({ checks, verdict }) {
  const tally = proofTally(checks);
  const parts = [`${tally.pass} of ${tally.decided} tests passed`];
  if (tally.weak) parts.push(`${tally.weak} partly`);
  if (tally.unknown) parts.push(`${tally.unknown} the data cannot settle`);
  return (
    <section className="ev-proof" aria-labelledby="ev-proof-title">
      <div className="ev-proof-head">
        <h5 id="ev-proof-title">Is the motion real?</h5>
        <span>{parts.join(' · ')}</span>
      </div>
      <p className="ev-note">
        Each test looks at a different part of the data, so a fake mover (noise, a glitch, two stars joined into
        one track) fails some of them.
      </p>
      {verdict && (
        <div className="mv-after">
          <p className="ev-note">With these tests and the other telescopes: <VerdictTag mv={verdict} /></p>
          <VerdictReason mv={verdict} className="ev-note" />
        </div>
      )}
      <ol className="ev-checks">
        {checks.map((c) => (
          <li key={c.key} className={`ev-check ev-check-${c.status in STATUS ? c.status : 'unknown'}`}>
            <span className="ev-mark" aria-hidden="true">{(STATUS[c.status] || STATUS.unknown).mark}</span>
            <div className="ev-check-body">
              <p className="ev-check-title">
                {c.title}<span className="ev-sr"> {(STATUS[c.status] || STATUS.unknown).label}.</span>
              </p>
              <p className="ev-check-summary">{c.summary}</p>
              {c.details?.length > 0 && (
                <details>
                  <summary>Numbers</summary>
                  <ul className="ev-check-details">{c.details.map((d, i) => <li key={i}>{d}</li>)}</ul>
                </details>
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

/** A few single exposures per pass, unprocessed but for background and alignment, with the detection marked. */
export function Exposures({ evidence }) {
  const rows = evidence.passes.filter((p) => p.exposures?.length);
  if (!rows.length) return null;
  const half = evidence.exposure_arcsec / 2;
  const first = evidence.passes.find((p) => p.point);
  const [tickIn, tickOut] = REF_TICK_ARCSEC;
  return (
    <section className="ev-exp" aria-labelledby="ev-exp-title">
      <h5 id="ev-exp-title">Single exposures, as the detector took them</h5>
      <p className="ev-note">
        One SPHEREx exposure per image: nothing combined or smoothed, only its background removed and its pointing
        aligned, on the same grey scale as above. Each is {evidence.exposure_arcsec.toFixed(0)}″ across, north up.{' '}
        <span className="ev-key"><i className="ev-key-ring" />where the engine found it in that exposure</span>{' '}
        <span className="ev-key"><i className="ev-key-ref" />where it was in {monthYear(first.point.date)}</span>
      </p>
      {rows.map((p) => (
        <div key={p.index} className="ev-exp-row">
          <p className="ev-exp-label">
            {passSpan(p)} · {p.exposures.length} of the {p.n_cover} exposures that cover it
          </p>
          <ul className="ev-exp-strip">
            {p.exposures.map((e) => (
              <li key={`${e.mjd}-${e.detector}`}>
                <div className="ev-exp-img">
                  <img
                    src={`data:image/png;base64,${e.image_png_b64}`} draggable="false"
                    alt={`Exposure of ${e.time} UTC, detector D${e.detector}${e.point ? '' : ': the engine did not find the object in it'}`}
                  />
                  <svg className="ev-marks" viewBox={`${-half} ${-half} ${2 * half} ${2 * half}`} aria-hidden="true">
                    <g className="ev-ref">
                      <line x1={-tickOut} y1="0" x2={-tickIn} y2="0" /><line x1={tickIn} y1="0" x2={tickOut} y2="0" />
                      <line x1="0" y1={-tickOut} x2="0" y2={-tickIn} /><line x1="0" y1={tickIn} x2="0" y2={tickOut} />
                    </g>
                    {e.point && <circle className="ev-ring" r={RING_ARCSEC} cx={-e.point.east_arcsec} cy={-e.point.north_arcsec} />}
                  </svg>
                </div>
                <p className="ev-exp-meta">
                  {dayMonthTime(e.time)}<br />
                  D{e.detector}{e.wavelength_um ? ` · ${e.wavelength_um.toFixed(2)} µm` : ''}
                  {!e.point && <><br /><span className="ev-dim">not found</span></>}
                </p>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}

// Chart geometry, in SVG user units (the chart scales with its container).
const W = 360;
const PLOT = [44, 352];
const PANEL_H = 112;
const PANEL_GAP = 26;
const AXIS_H = 22;

function Panel({ top, title, x, y, yTicks, yLabel, children, refs = [] }) {
  const sy = (v) => top + ((y[1] - v) / (y[1] - y[0])) * PANEL_H;
  return (
    <g>
      <text className="ev-panel-title" x={PLOT[0]} y={top - 8}>{title}</text>
      <rect className="ev-plot" x={PLOT[0]} y={top} width={PLOT[1] - PLOT[0]} height={PANEL_H} />
      {yTicks.map((v) => (
        <g key={v}>
          <line className={v === 0 ? 'ev-zero' : 'ev-grid'} x1={PLOT[0]} x2={PLOT[1]} y1={sy(v)} y2={sy(v)} />
          <text className="ev-tick" x={PLOT[0] - 6} y={sy(v) + 3.5} textAnchor="end">{yLabel(v)}</text>
        </g>
      ))}
      {refs.filter((r) => r.value > y[0] && r.value < y[1]).map((r) => (
        <g key={r.value}>
          <line className="ev-pixel" x1={PLOT[0]} x2={PLOT[1]} y1={sy(r.value)} y2={sy(r.value)} />
          <text className="ev-tick ev-pixel-label" x={PLOT[0] + 5} y={sy(r.value) - 4}>{r.label}</text>
        </g>
      ))}
      {children(sy, x)}
    </g>
  );
}

/** Offsets east and north against date, with errors, the fit, and the fixed stars' scatter. */
function ChartView({ model, brightness }) {
  const { x, y, points, band, lines, pixel } = model;
  const sx = (t) => PLOT[0] + ((t - x[0]) / (x[1] - x[0])) * (PLOT[1] - PLOT[0]);
  const yTicks = ticks(y[0], y[1], 4);
  const axes = [
    { key: 'north', title: 'North ↑ / south ↓ of the first position' },
    { key: 'east', title: 'East ← / west → of the first position' },
  ];
  const tops = axes.map((_, i) => 20 + i * (PANEL_H + PANEL_GAP));
  const brightTop = 20 + axes.length * (PANEL_H + PANEL_GAP);
  const bottom = (brightness ? brightTop + PANEL_H : tops[tops.length - 1] + PANEL_H);
  const clip = (top) => `ev-clip-${top}`;
  const labelled = points.length <= 4 ? points : [points[0], points[points.length - 1]];

  let bright = null;
  if (brightness) {
    const values = brightness.flatMap((s) => s.points.flatMap((p) => [p.value - p.err, p.value + p.err]));
    const lo = Math.max(0, Math.min(1, ...values)), hi = Math.max(1, ...values);
    const pad = (hi - lo) * 0.12 || 0.1;
    bright = { y: [Math.max(0, lo - pad), hi + pad] };
    bright.ticks = ticks(bright.y[0], bright.y[1], 3);
  }

  const summary = points.map((p) => `${dayMonthYear(p.date)}: ${signed(p.north)} arcsec north, ${signed(p.east)} arcsec east, error ${p.sigma.toFixed(1)}`).join('; ');

  return (
    <div className="ev-chart">
      <svg viewBox={`0 0 ${W} ${bottom + AXIS_H}`} role="img" aria-label={`Position over time relative to the first pass. ${summary}.`}>
        <defs>
          {[...tops, brightTop].map((top) => (
            <clipPath key={top} id={clip(top)}><rect x={PLOT[0]} y={top} width={PLOT[1] - PLOT[0]} height={PANEL_H} /></clipPath>
          ))}
        </defs>
        {axes.map((axis, i) => (
          <Panel
            key={axis.key} top={tops[i]} title={axis.title} x={x} y={y} yTicks={yTicks}
            yLabel={(v) => (v === 0 ? '0' : `${signed(v, 0)}″`)}
            refs={[{ value: pixel, label: '1 SPHEREx pixel' }, { value: -pixel, label: '1 pixel' }]}
          >
            {(sy) => (
              <g clipPath={`url(#${clip(tops[i])})`}>
                {band && (
                  <polygon
                    className="ev-band"
                    points={[...band.map((b) => `${sx(b.mjd)},${sy(b.sigma)}`), ...band.slice().reverse().map((b) => `${sx(b.mjd)},${sy(-b.sigma)}`)].join(' ')}
                  />
                )}
                {lines.map((l) => (
                  <line key={l.kind} className={`ev-fit ev-fit-${l.kind}`} x1={sx(x[0])} x2={sx(x[1])} y1={sy(l[axis.key][0])} y2={sy(l[axis.key][1])} />
                ))}
                {points.map((p) => (
                  <g key={p.index} className="ev-point">
                    <title>{`${dayMonthYear(p.date)}: ${signed(p[axis.key], 2)}″ ± ${p.sigma.toFixed(2)}″`}</title>
                    <line x1={sx(p.mjd)} x2={sx(p.mjd)} y1={sy(p[axis.key] - p.sigma)} y2={sy(p[axis.key] + p.sigma)} />
                    <line x1={sx(p.mjd) - 3} x2={sx(p.mjd) + 3} y1={sy(p[axis.key] - p.sigma)} y2={sy(p[axis.key] - p.sigma)} />
                    <line x1={sx(p.mjd) - 3} x2={sx(p.mjd) + 3} y1={sy(p[axis.key] + p.sigma)} y2={sy(p[axis.key] + p.sigma)} />
                    <circle cx={sx(p.mjd)} cy={sy(p[axis.key])} r="3" />
                  </g>
                ))}
              </g>
            )}
          </Panel>
        ))}
        {bright && (
          <Panel
            top={brightTop} title="Brightness (first pass = 1)" x={x} y={bright.y} yTicks={bright.ticks}
            yLabel={(v) => `×${v.toFixed(v < 10 && v % 1 ? 1 : 0)}`}
          >
            {(sy) => (
              <g clipPath={`url(#${clip(brightTop)})`}>
                {brightness.map((s, i) => (
                  <g key={s.band} className={`ev-flux ev-flux-${i % 3}`}>
                    <polyline points={s.points.map((p) => `${sx(p.mjd)},${sy(p.value)}`).join(' ')} />
                    {s.points.map((p) => (
                      <g key={p.index} className="ev-point">
                        <title>{`${s.band}: ×${p.value.toFixed(2)} ± ${p.err.toFixed(2)}`}</title>
                        <line x1={sx(p.mjd)} x2={sx(p.mjd)} y1={sy(p.value - p.err)} y2={sy(p.value + p.err)} />
                        <circle cx={sx(p.mjd)} cy={sy(p.value)} r="2.5" />
                      </g>
                    ))}
                  </g>
                ))}
              </g>
            )}
          </Panel>
        )}
        {labelled.map((p, i) => (
          <g key={p.index}>
            <line className="ev-grid" x1={sx(p.mjd)} x2={sx(p.mjd)} y1={bottom} y2={bottom + 4} />
            <text
              className="ev-tick" x={sx(p.mjd)} y={bottom + 16}
              textAnchor={i === 0 ? 'start' : i === labelled.length - 1 ? 'end' : 'middle'}
              dx={i === 0 ? -8 : i === labelled.length - 1 ? 8 : 0}
            >
              {monthYear(p.date)}
            </text>
          </g>
        ))}
      </svg>
      <ul className="ev-legend">
        <li><i className="ev-swatch ev-swatch-point" />measured in each pass, ±1σ</li>
        {lines.map((l) => <li key={l.kind}><i className={`ev-swatch ev-swatch-${l.kind}`} />{l.label}</li>)}
        {band && <li><i className="ev-swatch ev-swatch-band" />how far the field’s fixed stars seem to shift (1σ)</li>}
        {brightness?.map((s, i) => <li key={s.band}><i className={`ev-swatch ev-swatch-flux-${i % 3}`} />{bandText(s.band)}</li>)}
      </ul>
    </div>
  );
}


/** Where the motion chart comes from: positions over time, plus brightness for a variable. */
export function Chart({ object, evidence }) {
  const model = useMemo(() => motionChart(object, evidence), [object, evidence]);
  const brightness = useMemo(() => {
    if (!object.is_variable) return null;
    const series = brightnessSeries(evidence);
    return series.length ? series : null;
  }, [object, evidence]);
  return <ChartView model={model} brightness={brightness} />;
}


/** The telescope pictures of an engine object: 2MASS, WISE and each SPHEREx pass, oldest first. */
export function EnginePictures({ object, ev, title }) {
  const surveys = ev.surveys;
  const pictures = useMemo(
    () => (ev.status === 'ok' ? engineViews(object, ev.evidence, surveys.status === 'ok' ? surveys.data : null) : null),
    [object, ev.status, ev.evidence, surveys.status, surveys.data],
  );
  if (ev.status === 'none') return <p className="ev-state">The images need the AstroSearchX API, which is not connected.</p>;
  if (ev.status === 'error') {
    return (
      <p className="ev-state ev-err" role="alert">
        The images could not be loaded: {ev.error}.{' '}
        <button type="button" className="ev-textbtn" onClick={ev.retry}>Retry</button>
      </p>
    );
  }
  if (!pictures) {
    return <SkyPictures views={[]} notes={[{ key: 'l', text: ev.job ? rerunText(ev.job) : 'Loading the SPHEREx images of this object' }]} title={title} />;
  }
  const notes = [];
  if (surveys.status === 'loading') notes.push({ key: 's', text: 'Loading 2MASS (about 2000) and WISE (about 2010)…' });
  if (surveys.status === 'error') notes.push({ key: 's', text: `2MASS and WISE are unavailable: ${surveys.error}.` });
  for (const g of surveys.status === 'ok' ? surveys.data.unavailable : []) notes.push({ key: `g:${g.survey}`, text: `${g.label}: ${g.reason}` });
  return (
    <>
      <SkyPictures key={ev.key} views={pictures.views} frames={pictures.frames} initialFrame={pictures.initial} extras={pictures.extras} notes={notes} title={title} />
      <p className="ev-note">
        Real images, north up, east left, nothing smoothed in. SPHEREx: the median of each pass, on one grey scale.{' '}
        <span className="ev-key"><i className="ev-key-ring" />where it was on that date</span>{' '}
        <span className="ev-key"><i className="ev-key-ref" />reference position</span>
        {object.saturated && ' The white core is saturated: too bright for the detector.'}
      </p>
    </>
  );
}

/**
 * Everything at once, for a page that wants the evidence in one piece: pictures, the verdict,
 * the proof checklist, the chart and the single exposures. Fills its container's width.
 */
export default function MotionEvidence({ object, field }) {
  const ev = useEvidence(object, field);
  const verdict = useVerdict(object, ev);
  const checks = useChecks(object, ev);
  const mv = (ev.status === 'ok' && motionVerdict(ev.evidence)) || motionVerdict(object);
  if (!ev.key) return null;
  return (
    <section className="ev" aria-label="Evidence for this object">
      <EnginePictures object={object} ev={ev} />
      {mv && (
        <>
          <h4 className={`ev-headline mv-text-${mv.tone}`}>{mv.label}</h4>
          <VerdictReason mv={mv} className="ev-line" />
        </>
      )}
      {verdict && (
        <>
          {!mv && <h4 className={`ev-headline ev-tone-${verdict.tone}`}>{verdict.headline}</h4>}
          {verdict.lines.map((line) => <p key={line} className="ev-line">{line}</p>)}
        </>
      )}
      {checks && <Proof checks={checks} verdict={ev.status === 'ok' ? motionVerdict(ev.evidence) : null} />}
      {ev.status === 'ok' && <Chart object={object} evidence={ev.evidence} />}
      {ev.status === 'ok' && <Exposures evidence={ev.evidence} />}
    </section>
  );
}
