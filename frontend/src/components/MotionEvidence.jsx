import { useEffect, useMemo, useState } from 'react';
import { prefersReducedMotion } from '../lib/camera.js';
import {
  brightnessSeries, dayMonthYear, describe, fetchEvidence, fetchSurveyImages, monthYear, motionChart, passSpan,
  predictionMotion, scaleBar, surveyMarks, ticks,
} from '../lib/evidence.js';
import './MotionEvidence.css';

const BLINK_MS = 900;
// Marks on the images, in arcsec on the sky. The ring is wider than SPHEREx's PSF, so it
// frames the object instead of covering it.
const RING_ARCSEC = 9;
const REF_TICK_ARCSEC = [13, 20];
const SCALE_BAR_ARCSEC = 30;
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

const SURVEY_SHORT = { '2mass': '2MASS', wise: 'WISE' };
const surveyView = (image) => `survey:${image.survey}`;

function Compass() {
  return (
    <span className="ev-tag ev-compass" aria-hidden="true">
      <svg width="34" height="30" viewBox="0 0 34 30">
        <path d="M24 26V7M24 26H7M20.5 10.5 24 7l3.5 3.5M10.5 22.5 7 26l3.5 3.5" fill="none" stroke="currentColor" />
        <text x="24" y="5" textAnchor="middle">N</text><text x="2.5" y="29" textAnchor="middle">E</text>
      </svg>
    </span>
  );
}

/** A scale bar of `bar` arcsec in the lower left of an image `side` arcsec wide. */
function ScaleBar({ side, bar }) {
  const half = side / 2;
  return (
    <>
      <svg className="ev-marks" viewBox={`${-half} ${-half} ${side} ${side}`} aria-hidden="true">
        <line className="ev-bar" x1={-0.92 * half} x2={-0.92 * half + bar} y1={0.91 * half} y2={0.91 * half} />
      </svg>
      <span className="ev-tag ev-scale" aria-hidden="true">{bar >= 60 ? `${bar / 60}′` : `${bar}″`}</span>
    </>
  );
}

/** Crosshair ticks around (cx, cy), scaled with the image (`k` = 1 on the SPHEREx patch). */
function Ticks({ cx = 0, cy = 0, k = 1 }) {
  const [tickIn, tickOut] = REF_TICK_ARCSEC.map((v) => v * k);
  return (
    <g className="ev-ref">
      <line x1={cx - tickOut} y1={cy} x2={cx - tickIn} y2={cy} /><line x1={cx + tickIn} y1={cy} x2={cx + tickOut} y2={cy} />
      <line x1={cx} y1={cy - tickOut} x2={cx} y2={cy - tickIn} /><line x1={cx} y1={cy + tickIn} x2={cx} y2={cy + tickOut} />
    </g>
  );
}

/** What the older survey image shows, in words. */
function surveyCaption(image, mark, object) {
  const when = `${image.label} · ${dayMonthYear(image.date)}${image.survey === 'wise' ? ' (mean date of the AllWISE exposures)' : ''}`;
  const years = `${mark.years.toFixed(0)} years before SPHEREx`;
  if (!mark.source) {
    return `${when} · ${years}. The engine found no motion, so the ring is at its SPHEREx position: a fixed star sits in it here too.`;
  }
  const err = mark.then.err >= 1 ? ` (± ${mark.then.err.toFixed(0)}″)` : '';
  const where = mark.inside
    ? `Ring: where ${mark.source} puts ${object.name || 'it'} on that date${err}. Cross: its SPHEREx position.`
    : `Where ${mark.source} puts it on that date lies outside this image. Cross: its SPHEREx position.`;
  return `${when} · ${years}. ${where}`;
}

/**
 * The real images, one per pass, blinking in place, with the measured positions marked; then the
 * same sky in older surveys (2MASS, WISE), when they load, with where the object was predicted.
 */
function Blink({ evidence, object, surveys }) {
  const { passes } = evidence;
  const seen = useMemo(() => passes.filter((p) => p.point), [passes]);
  // A pass index, 'difference', or 'survey:<key>' for an older survey's image.
  const [view, setView] = useState(seen[0].index);
  const [playing, setPlaying] = useState(() => passes.length > 1 && !prefersReducedMotion());
  const [marks, setMarks] = useState(true);

  useEffect(() => {
    if (!playing) return undefined;
    // Blinking goes through the SPHEREx passes only: the older images have another scale.
    const timer = setInterval(() => setView((v) => (typeof v === 'number' ? (v + 1) % passes.length : 0)), BLINK_MS);
    return () => clearInterval(timer);
  }, [playing, passes.length]);

  const show = (v) => { setPlaying(false); setView(v); };
  const step = (by) => show(typeof view === 'number' ? (view + by + passes.length) % passes.length : seen[0].index);
  const onKeyDown = (ev) => {
    if (ev.key === 'ArrowRight') { ev.preventDefault(); step(1); }
    if (ev.key === 'ArrowLeft') { ev.preventDefault(); step(-1); }
  };

  const half = evidence.patch_arcsec / 2;
  const older = surveys.status === 'ok' ? surveys.data.images : [];
  const current = typeof view === 'number' ? passes[view] : null;
  const image = older.find((im) => surveyView(im) === view) || null;
  const mark = image ? surveyMarks(object, evidence, surveys.data, image) : null;
  const [firstIndex, lastIndex] = evidence.difference_passes;
  const gaps = passes.some((p) => p.coverage < 1);
  const at = (p) => ({ cx: -p.point.east_arcsec, cy: -p.point.north_arcsec }); // east is left, north is up

  let caption, label, tag;
  if (current) {
    caption = `Pass ${current.index + 1} of ${passes.length} · ${dayMonthYear(current.start)} to ${dayMonthYear(current.end)} · median of ${current.n_frames} exposures${current.point ? '' : ' · the engine did not find the object in this pass'}`;
    label = `SPHEREx image of the sky around the object, pass ${current.index + 1} of ${passes.length}, ${passSpan(current)}`;
    tag = passSpan(current);
  } else if (image) {
    caption = surveyCaption(image, mark, object);
    label = `${image.label} image of the same sky, ${dayMonthYear(image.date)}`;
    tag = `${SURVEY_SHORT[image.survey] || image.survey} · ${image.date.slice(0, 4)}`;
  } else {
    caption = `${passSpan(passes[lastIndex])} minus ${passSpan(passes[firstIndex])}. Grey: no change. White: brighter now. Black: fainter now. An object that moved leaves a dark spot where it was and a bright one where it is.`;
    label = 'Difference image: last pass minus first pass';
    tag = 'Difference';
  }

  let surveyMarksSvg = null;
  if (image && marks) {
    const side = surveys.data.patch_arcsec, h = side / 2;
    // Marks keep their size on the screen: the older images cover more sky than the SPHEREx patch.
    const k = side / evidence.patch_arcsec;
    const ring = RING_ARCSEC * k;
    const then = { cx: -mark.then.east, cy: -mark.then.north };
    const now = { cx: -mark.now.east, cy: -mark.now.north };
    surveyMarksSvg = (
      <svg className="ev-marks" viewBox={`${-h} ${-h} ${side} ${side}`} aria-hidden="true">
        {mark.source && <line className="ev-track" x1={then.cx} y1={then.cy} x2={now.cx} y2={now.cy} />}
        <Ticks {...now} k={k} />
        {mark.inside && <circle className="ev-ring" r={ring} {...then} />}
        {mark.inside && mark.then.err > ring && <circle className="ev-ring ev-was" r={mark.then.err} {...then} />}
      </svg>
    );
  }

  return (
    <div className="ev-view">
      <div className="ev-sky" role="img" aria-label={label}>
        {passes.map((p) => (
          <img key={p.index} className={view === p.index ? 'ev-on' : ''} src={`data:image/png;base64,${p.image_png_b64}`} alt="" draggable="false" />
        ))}
        <img className={view === 'difference' ? 'ev-on' : ''} src={`data:image/png;base64,${evidence.difference_png_b64}`} alt="" draggable="false" />
        {older.map((im) => (
          <img key={im.survey} className={view === surveyView(im) ? 'ev-on' : ''} src={`data:image/png;base64,${im.image_png_b64}`} alt="" draggable="false" />
        ))}
        {marks && !image && (
          <svg className="ev-marks" viewBox={`${-half} ${-half} ${2 * half} ${2 * half}`} aria-hidden="true">
            <Ticks />
            {current?.point && <circle className="ev-ring" r={RING_ARCSEC} {...at(current)} />}
            {!current && (
              <>
                <circle className="ev-ring ev-was" r={RING_ARCSEC} {...at(passes[firstIndex])} />
                <circle className="ev-ring" r={RING_ARCSEC} {...at(passes[lastIndex])} />
              </>
            )}
          </svg>
        )}
        {surveyMarksSvg}
        <span className="ev-tag ev-when">{tag}</span>
        <Compass />
        {image
          ? <ScaleBar side={surveys.data.patch_arcsec} bar={scaleBar(surveys.data.patch_arcsec)} />
          : <ScaleBar side={evidence.patch_arcsec} bar={SCALE_BAR_ARCSEC} />}
      </div>

      {/* Arrow keys step through the passes; every action is also a button. */}
      <div className="ev-controls" role="group" aria-label="Survey passes" onKeyDown={onKeyDown}>
        <button
          type="button" className="ev-iconbtn" disabled={passes.length < 2}
          aria-label={playing ? 'Pause blinking' : 'Blink between passes'} onClick={() => setPlaying(!playing)}
        >
          {playing
            ? <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M1.5 0v12M8.5 0v12" stroke="currentColor" strokeWidth="2" /></svg>
            : <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M0 0l10 6-10 6z" fill="currentColor" /></svg>}
        </button>
        {passes.map((p) => (
          <button
            key={p.index} type="button" className={`ev-opt ${view === p.index ? 'ev-on' : ''}`}
            aria-pressed={view === p.index} aria-label={`Pass ${p.index + 1}, ${passSpan(p)}`} onClick={() => show(p.index)}
          >
            {monthYear(p.point?.date ?? p.start)}
          </button>
        ))}
        <span className="ev-extra">
          <button
            type="button" className={`ev-opt ${view === 'difference' ? 'ev-on' : ''}`}
            aria-pressed={view === 'difference'} onClick={() => show('difference')}
          >
            Difference
          </button>
          <button type="button" className={`ev-opt ${marks ? 'ev-on' : ''}`} aria-pressed={marks} onClick={() => setMarks(!marks)}>
            Marks
          </button>
        </span>
      </div>
      {surveys.status !== 'none' && (
        <div className="ev-controls ev-older" role="group" aria-label="Older surveys">
          <span className="ev-older-title">Older surveys</span>
          {older.map((im) => (
            <button
              key={im.survey} type="button" className={`ev-opt ${view === surveyView(im) ? 'ev-on' : ''}`}
              aria-pressed={view === surveyView(im)} aria-label={`${im.label}, ${dayMonthYear(im.date)}`} onClick={() => show(surveyView(im))}
            >
              {SURVEY_SHORT[im.survey] || im.survey} {im.date.slice(0, 4)}
            </button>
          ))}
          {surveys.status === 'loading' && <span className="ev-older-state">loading 2MASS and WISE</span>}
          {surveys.status === 'error' && <span className="ev-older-state">unavailable ({surveys.error})</span>}
          {surveys.status === 'ok' && surveys.data.unavailable.map((g) => (
            <span key={g.survey} className="ev-older-state" title={g.reason}>{SURVEY_SHORT[g.survey] || g.survey}: unavailable</span>
          ))}
        </div>
      )}
      <p className="ev-caption" aria-live={playing ? 'off' : 'polite'}>{caption}</p>
      <p className="ev-note">
        {image
          ? <>Archive image from IRSA, north up, east left, resampled without smoothing to {image.pixel_arcsec.toFixed(1)}″ pixels and centred between the two marks.{' '}</>
          : <>Real SPHEREx images, north up, east left, all on one grey scale. Each image pixel is half a SPHEREx
            pixel ({evidence.patch_pixel_arcsec.toFixed(1)}″) and shows measured values only, nothing smoothed in.{' '}</>}
        <span className="ev-key"><i className="ev-key-ring" />{image ? 'predicted position on the date shown' : 'measured position in the pass shown'}</span>{' '}
        <span className="ev-key"><i className="ev-key-ref" />{image ? 'SPHEREx position' : 'where it was in the first pass'}</span>
        {!image && object.saturated && ' The white core is saturated: too bright for the detector, so drawn white.'}
        {(image || gaps) && ' Hatched: no data.'}
      </p>
    </div>
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
function Chart({ model, brightness }) {
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

/**
 * The evidence behind one engine object: a verdict in plain words, the real SPHEREx images of it
 * per pass (blinking, with a difference view) and its measured positions over time against the
 * field's noise. Fills its container's width (340-900 px); no fixed positioning.
 *
 * Props:
 *   object: a MotionObject from the engine result (see backend/planetx/api/schemas.py)
 *   field:  { ra, dec, size_deg } of the run it came from (`result.field` from runMotion)
 */
export default function MotionEvidence({ object, field }) {
  // The request depends on these numbers only, so new instances of the same object and field
  // (a parent re-render) do not fetch again.
  const ra = field?.ra, dec = field?.dec, sizeDeg = field?.size_deg;
  const id = object?.id, objectRa = object?.ra, objectDec = object?.dec;
  const key = object && field ? `${ra},${dec},${sizeDeg},${id},${objectRa},${objectDec}` : null;
  const [state, setState] = useState({ key: null });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!key) return undefined;
    const ctrl = new AbortController();
    fetchEvidence({ ra, dec, size_deg: sizeDeg }, { id, ra: objectRa, dec: objectDec }, {
      signal: ctrl.signal,
      onProgress: (job) => { if (!ctrl.signal.aborted) setState({ key, status: 'loading', job }); },
    }).then(
      (evidence) => { if (!ctrl.signal.aborted) setState({ key, status: evidence ? 'ok' : 'none', evidence }); },
      (err) => { if (!ctrl.signal.aborted) setState({ key, status: 'error', error: err?.message || String(err) }); },
    );
    return () => ctrl.abort();
  }, [key, attempt, ra, dec, sizeDeg, id, objectRa, objectDec]);

  const verdict = useMemo(
    () => (state.key === key && state.status === 'ok' ? describe(object, state.evidence) : null),
    [state, key, object],
  );
  const model = useMemo(
    () => (state.key === key && state.status === 'ok' ? motionChart(object, state.evidence) : null),
    [state, key, object],
  );
  // Older survey images: asked for once the SPHEREx evidence is in, and never in its way. They
  // depend on the object only through the motion that places it (numbers, so a new instance of
  // the same object does not ask again).
  const evidence = state.key === key && state.status === 'ok' ? state.evidence : null;
  const { pmEast, pmNorth, err: pmErr, source } = predictionMotion(object);
  const [older, setOlder] = useState({ evidence: null });
  useEffect(() => {
    if (!evidence) return undefined;
    const ctrl = new AbortController();
    fetchSurveyImages({ pmEast, pmNorth, err: pmErr, source }, evidence, { signal: ctrl.signal }).then(
      (data) => { if (!ctrl.signal.aborted) setOlder({ evidence, status: data ? 'ok' : 'none', data }); },
      (err) => { if (!ctrl.signal.aborted) setOlder({ evidence, status: 'error', error: err?.message || String(err) }); },
    );
    return () => ctrl.abort();
  }, [evidence, pmEast, pmNorth, pmErr, source]);
  const surveys = older.evidence === evidence ? older : { status: 'loading' };

  const brightness = useMemo(() => {
    if (state.key !== key || state.status !== 'ok' || !object.is_variable) return null;
    const series = brightnessSeries(state.evidence);
    return series.length ? series : null;
  }, [state, key, object]);

  if (!key) return null;
  // Answers for a previous object are ignored: until this object's arrive, it is loading.
  const now = state.key === key ? state : { status: 'loading' };

  return (
    <section className="ev" aria-label="Evidence for this object">
      <p className="ev-kicker">Evidence{object.name ? ` · ${object.name}` : ''}</p>
      {now.status === 'loading' && (
        <p className="ev-state" role="status">
          <span className="ev-ticker" aria-hidden="true" />
          {now.job ? rerunText(now.job) : 'Loading the images of this object'}
        </p>
      )}
      {now.status === 'none' && <p className="ev-state">The images need the PlanetX API, which is not connected.</p>}
      {now.status === 'error' && (
        <p className="ev-state ev-err" role="alert">
          The evidence could not be loaded: {now.error}.{' '}
          <button type="button" className="ev-textbtn" onClick={() => { setState({ key: null }); setAttempt(attempt + 1); }}>Retry</button>
        </p>
      )}
      {now.status === 'ok' && (
        <>
          <h4 className={`ev-headline ev-tone-${verdict.tone}`}>{verdict.headline}</h4>
          {verdict.lines.map((line) => <p key={line} className="ev-line">{line}</p>)}
          <div className="ev-body">
            <Blink key={key} evidence={now.evidence} object={object} surveys={surveys} />
            <Chart model={model} brightness={brightness} />
          </div>
        </>
      )}
    </section>
  );
}
