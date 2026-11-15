import { useEffect, useMemo, useRef, useState } from 'react';
import { prefersReducedMotion } from '../lib/camera.js';
import { epochNow } from '../lib/coords.js';
import { fetchPatchSurveys, scaleBar } from '../lib/evidence.js';
import {
  angleText, fetchPatchFrames, frameImageUrl, imageSrc, isRemote, knownMotion, knownViews, offFrame, picturePatch, stepKey,
  wantedKeys,
} from '../lib/pictures.js';
import { API_BASE } from '../lib/services.js';
import './SkyPictures.css';

const STEP_MS = 1000;
// Marks, in arcsec on the sky: never smaller than SPHEREx's PSF, so a ring frames the object.
const RING_MIN_ARCSEC = 9;

/** What one picture shows, in words. */
function caption(view, newest) {
  const parts = [`${view.telescope} · ${view.band} · ${view.when}`];
  if (view.note) parts.push(view.note);
  let text = `${parts.join(' · ')}.`;
  const tone = view.marks.tone === 'catalogue' ? 'the catalogue motion puts it' : view.telescope === 'SPHEREx' && view.key !== 'difference' ? 'the engine measured it' : 'its motion puts it';
  if (view.key === 'difference') {
    return `${text} Grey: no change. White: brighter now. Black: fainter now. Something that moved leaves a dark spot where it was and a bright one where it is.`;
  }
  if (view.missed) return `${text} The engine did not find the object in this pass.`;
  if (view.marks.ring) text += ` Ring: where ${tone} on this date.`;
  else if (view.outside) text += ` On this date ${tone} outside this picture.`;
  if (view.marks.ref) {
    text += view.marks.tone === 'catalogue'
      ? ` Cross: where it is in the newest picture${newest ? ` (${newest.when})` : ''}.`
      : view.telescope === 'SPHEREx' ? ' Cross: where it was in the first pass.' : ' Cross: its SPHEREx position.';
  }
  return text;
}

function Compass() {
  return (
    <span className="pic-tag pic-compass" aria-hidden="true">
      <svg width="34" height="30" viewBox="0 0 34 30">
        <path d="M24 26V7M24 26H7M20.5 10.5 24 7l3.5 3.5M10.5 22.5 7 26l3.5 3.5" fill="none" stroke="currentColor" />
        <text x="24" y="5" textAnchor="middle">N</text><text x="2.5" y="29" textAnchor="middle">E</text>
      </svg>
    </span>
  );
}

/**
 * Marks in the shared coordinates (arcsec, east is left, north is up), seen through `frame`
 * ({ east, north, side }): what falls outside it is cut off.
 */
function Marks({ view, frame }) {
  const { marks } = view;
  const { side } = frame;
  const h = side / 2;
  const r = Math.max(RING_MIN_ARCSEC, side * 0.04);
  const [tin, tout] = [r * 1.35, r * 2.2];
  const p = (o) => ({ x: -o.east, y: -o.north });
  const ref = marks.ref && p(marks.ref);
  return (
    <svg className={`pic-marks pic-tone-${marks.tone}`} viewBox={`${-frame.east - h} ${-frame.north - h} ${side} ${side}`} aria-hidden="true">
      {marks.line && <line className="pic-track" x1={p(marks.line[0]).x} y1={p(marks.line[0]).y} x2={p(marks.line[1]).x} y2={p(marks.line[1]).y} />}
      {ref && (
        <g className="pic-ref">
          <line x1={ref.x - tout} y1={ref.y} x2={ref.x - tin} y2={ref.y} /><line x1={ref.x + tin} y1={ref.y} x2={ref.x + tout} y2={ref.y} />
          <line x1={ref.x} y1={ref.y - tout} x2={ref.x} y2={ref.y - tin} /><line x1={ref.x} y1={ref.y + tin} x2={ref.x} y2={ref.y + tout} />
        </g>
      )}
      {marks.was && <circle className="pic-ring pic-was" r={r} cx={p(marks.was).x} cy={p(marks.was).y} />}
      {marks.ring && <circle className="pic-ring" r={r} cx={p(marks.ring).x} cy={p(marks.ring).y} />}
      {marks.ring && marks.err > r && <circle className="pic-ring pic-was" r={marks.err} cx={p(marks.ring).x} cy={p(marks.ring).y} />}
    </svg>
  );
}

/** Where an image's box sits in the frame shown, as CSS percentages (beyond 0-100%: cropped). */
function placement(box, frame) {
  const pct = (v) => `${(v / frame.side) * 100}%`;
  return {
    left: pct(frame.side / 2 - (box.east - frame.east) - box.side / 2),
    top: pct(frame.side / 2 - (box.north - frame.north) - box.side / 2),
    width: pct(box.side), height: pct(box.side),
  };
}

/** An arrow at the frame's edge toward where the object was on this date, when that is outside the frame. */
function OffFrame({ view, frame, wider }) {
  const off = offFrame(view.at, frame);
  if (!off) return null;
  return (
    // The words go on the inner side of the arrow, clear of the edge (and the scale bar).
    <span className={`pic-off pic-tone-${view.marks.tone} ${off.y > 0.5 ? 'pic-off-low' : ''}`} style={{ left: `${off.x * 100}%`, top: `${off.y * 100}%` }}>
      <svg width="14" height="14" viewBox="0 0 14 14" style={{ transform: `rotate(${off.angle}deg)` }} aria-hidden="true">
        <path d="M7 13V2M2.5 6.5 7 2l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.75" />
      </svg>
      <span className="pic-off-label">
        {view.date.slice(0, 4)} position is {angleText(off.distance)} {off.direction}{wider ? ' · zoom out' : ''}
      </span>
    </span>
  );
}

/** The view a slot shows now: itself, or the exposure of the same pass it fell back on. */
function effective(view, state) {
  const alt = state?.alt || 0;
  return alt && view.alternates?.[alt - 1] ? { ...view, ...view.alternates[alt - 1], key: view.key } : view;
}

/**
 * The square with the pictures stacked in it, each at its place in the shared frame; only the
 * current one is visible. A remote image is only asked for once wanted (lib/pictures wantedKeys).
 */
function Stage({ all, frame, wider, current, wanted, showMarks, images, onImage }) {
  const bar = scaleBar(frame.side);
  const state = images[current.key];
  const shown = effective(current, state);
  // An arrow to an off-frame position near the bottom takes the scale bar's corner: the bar moves up.
  const low = state?.status === 'ok' && showMarks && (offFrame(shown.at, frame)?.y ?? 0) > 0.5;
  return (
    <span className="pic-sky">
      {all.filter((v) => !isRemote(v.src) || wanted.has(v.key)).map((v) => {
        const s = images[v.key];
        const e = effective(v, s);
        return (
          <img
            key={`${v.key}:${s?.attempt || 0}:${s?.alt || 0}`}
            className={v.key === current.key && s?.status === 'ok' ? 'pic-on' : ''}
            style={placement(e.box, frame)}
            src={imageSrc(e.src, s?.attempt)}
            alt="" draggable="false"
            onLoad={() => onImage(v, 'ok')}
            onError={() => onImage(v, 'error')}
          />
        );
      })}
      {state?.status === 'ok' && showMarks && <Marks view={shown} frame={frame} />}
      {state?.status === 'ok' && showMarks && <OffFrame view={shown} frame={frame} wider={wider} />}
      {state?.status !== 'ok' && (
        <span className="pic-wait" role="status">
          {state?.status === 'error'
            ? 'This picture could not be loaded.'
            : <><span className="pic-ticker" aria-hidden="true" />Loading {current.telescope} picture{current.telescope === 'SPHEREx' && isRemote(current.src) ? ' (about 4 MB from IRSA the first time)' : ''}</>}
        </span>
      )}
      <span className="pic-tag pic-when"><b>{shown.telescope}</b> {shown.when}</span>
      <Compass />
      <span className={`pic-tag pic-scale ${low ? 'pic-scale-up' : ''}`} aria-hidden="true" style={{ '--bar': `${(bar / frame.side) * 100}%` }}>
        <i />{bar >= 60 ? `${bar / 60}′` : `${bar}″`}
      </span>
    </span>
  );
}

const PlayIcon = ({ playing }) => (playing
  ? <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M1.5 0v12M8.5 0v12" stroke="currentColor" strokeWidth="2" /></svg>
  : <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M0 0l10 6-10 6z" fill="currentColor" /></svg>);

/** Play, step and pick: arrow keys step too; every action is also a button. */
function Controls({ views, extras, frames, frame, current, playing, showMarks, images, onPlay, onStep, onShow, onMarks, onZoom }) {
  const onKeyDown = (ev) => {
    if (ev.key === 'ArrowRight') { ev.preventDefault(); onStep(1); }
    if (ev.key === 'ArrowLeft') { ev.preventDefault(); onStep(-1); }
  };
  return (
    <div className="pic-controls" role="group" aria-label="Pictures, oldest first" onKeyDown={onKeyDown}>
      <span className="pic-steps">
        <button type="button" className="pic-iconbtn" disabled={views.length < 2} aria-label={playing ? 'Pause' : 'Play the pictures in date order'} onClick={onPlay}>
          <PlayIcon playing={playing} />
        </button>
        <button type="button" className="pic-iconbtn" disabled={views.length < 2} aria-label="Previous picture" onClick={() => onStep(-1)}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M7 1L3 5l4 4" fill="none" stroke="currentColor" strokeWidth="1.5" /></svg>
        </button>
        <button type="button" className="pic-iconbtn" disabled={views.length < 2} aria-label="Next picture" onClick={() => onStep(1)}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M3 1l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.5" /></svg>
        </button>
      </span>
      <ol className="pic-epochs">
        {views.map((v) => {
          const e = effective(v, images[v.key]);
          return (
            <li key={v.key}>
              <button
                type="button" className={`pic-opt ${v.key === current.key ? 'pic-on' : ''} ${images[v.key]?.status === 'error' ? 'pic-failed' : ''}`}
                aria-pressed={v.key === current.key} aria-label={`${e.telescope}, ${e.when}`} onClick={() => onShow(v.key)}
              >
                <span className="pic-opt-scope">{v.telescope}</span> {v.button.slice(v.telescope.length + 1)}
              </button>
            </li>
          );
        })}
      </ol>
      {frames.length > 1 && (
        <span className="pic-zooms" role="group" aria-label="Zoom">
          {frames.map((f) => (
            <button key={f.key} type="button" className={`pic-opt ${f.key === frame.key ? 'pic-on' : ''}`} aria-pressed={f.key === frame.key} onClick={() => onZoom(f.key)}>
              {f.label}
            </button>
          ))}
        </span>
      )}
      <span className="pic-extra">
        {extras.map((v) => (
          <button key={v.key} type="button" className={`pic-opt ${v.key === current.key ? 'pic-on' : ''}`} aria-pressed={v.key === current.key} onClick={() => onShow(v.key)}>
            {v.button}
          </button>
        ))}
        <button type="button" className={`pic-opt ${showMarks ? 'pic-on' : ''}`} aria-pressed={showMarks} onClick={onMarks}>Marks</button>
      </span>
    </div>
  );
}

/**
 * The telescope pictures of one object, oldest first, with a play control that steps through
 * them, marks where the object is on each date, and a larger view on request.
 *
 * Props: `views` time-ordered pictures in shared coordinates, `frames` the zooms to look at them
 * through (close to wide) and `initialFrame` the key of the one to open on (see knownViews /
 * engineViews in lib/pictures.js), `extras` pictures outside the sequence (the difference
 * image), `notes` [{ key, text }] for what is still loading or missing, `title` for the large view.
 * Remote pictures (SPHEREx cutouts, ~4 MB each from IRSA the first time) load one ahead of the
 * one shown, not all at once.
 */
export default function SkyPictures({ views, frames = [], initialFrame, extras = [], notes = [], title }) {
  const all = useMemo(() => [...views, ...extras], [views, extras]);
  const [key, setKey] = useState(null);
  const [playing, setPlaying] = useState(() => !prefersReducedMotion());
  const [showMarks, setShowMarks] = useState(true);
  const [images, setImages] = useState({}); // key -> { status: 'ok' | 'error' | null, attempt, alt }
  const [seen, setSeen] = useState(() => new Set()); // keys shown so far: their images stay
  const [big, setBig] = useState(false);
  const [zoom, setZoom] = useState(null); // a frame key the viewer picked; until then, initialFrame
  const dialog = useRef(null);
  const frame = frames.find((f) => f.key === zoom) || frames.find((f) => f.key === initialFrame) || frames[0];
  const wider = frame && frames.indexOf(frame) < frames.length - 1;

  const current = all.find((v) => v.key === key) || views[0] || null;
  const newest = views[views.length - 1];
  const currentKey = current?.key ?? null;
  const status = Object.fromEntries(Object.entries(images).map(([k, s]) => [k, s.status]));
  const wanted = wantedKeys(views, currentKey, seen);
  // Playing waits on a picture that is still loading, and skips one that failed.
  const next = stepKey(views, currentKey, 1, status);
  const nextReady = next != null && status[next] === 'ok';

  // Going to a picture keeps it (and so its image) among the ones fetched.
  const go = (k) => {
    setKey(k);
    setSeen((s) => (s.has(k) ? s : new Set(s).add(k)));
  };

  useEffect(() => {
    if (!playing || !nextReady || next === currentKey) return undefined;
    const timer = setTimeout(() => {
      setKey(next);
      setSeen((s) => (s.has(next) ? s : new Set(s).add(next)));
    }, STEP_MS);
    return () => clearTimeout(timer);
  }, [playing, nextReady, next, currentKey]);

  useEffect(() => {
    const d = dialog.current;
    if (big && d && !d.open) d.showModal();
    if (!big && d?.open) d.close();
  }, [big]);

  const show = (k) => { setPlaying(false); go(k); };
  const step = (by) => { const k = stepKey(views, currentKey, by); if (k) show(k); };
  const onImage = (view, result) => setImages((m) => {
    const s = m[view.key] || {};
    // A cutout that cannot be had: show another exposure of the same pass instead.
    if (result === 'error' && (s.alt || 0) < (view.alternates?.length || 0)) return { ...m, [view.key]: { ...s, alt: (s.alt || 0) + 1, status: null } };
    return { ...m, [view.key]: { ...s, status: result } };
  });
  const retry = (k) => setImages((m) => ({ ...m, [k]: { status: null, attempt: (m[k]?.attempt || 0) + 1, alt: 0 } }));

  if (!current || !frame) {
    return (
      <section className="pic" aria-label="Telescope pictures">
        <div className="pic-sky pic-empty">
          <span className="pic-wait" role="status">
            {notes.length ? notes.map((n) => <span key={n.key}>{n.text}</span>) : 'No pictures of this spot.'}
          </span>
        </div>
      </section>
    );
  }

  const shown = effective(current, images[current.key]);
  const controls = (
    <Controls
      views={views} extras={extras} frames={frames} frame={frame} current={current} playing={playing} showMarks={showMarks} images={images}
      onPlay={() => setPlaying(!playing)} onStep={step} onShow={show} onMarks={() => setShowMarks(!showMarks)} onZoom={setZoom}
    />
  );
  const label = `${shown.telescope} picture, ${shown.when}`;
  const failed = images[current.key]?.status === 'error';
  const stage = { all, frame, wider, current, wanted, showMarks, images, onImage };

  return (
    <section className="pic" aria-label="Telescope pictures">
      <button type="button" className="pic-frame" aria-label={`Enlarge: ${label}`} aria-haspopup="dialog" onClick={() => { setPlaying(false); setBig(true); }}>
        <Stage {...stage} />
        <span className="pic-tag pic-zoom" aria-hidden="true">
          <svg width="11" height="11" viewBox="0 0 11 11"><path d="M1 4V1h3M7 1h3v3M10 7v3H7M4 10H1V7" fill="none" stroke="currentColor" strokeWidth="1.25" /></svg>
        </span>
      </button>
      {controls}
      <p className="pic-caption" aria-live={playing ? 'off' : 'polite'}>
        {caption(shown, newest)}
        {failed && isRemote(current.src) && <> <button type="button" className="textbtn" onClick={() => retry(current.key)}>Retry</button></>}
      </p>
      {notes.length > 0 && <ul className="pic-notes">{notes.map((n) => <li key={n.key}>{n.text}</li>)}</ul>}

      {/* The larger view: a modal dialog, so Esc closes it and focus returns to the picture. */}
      <dialog
        ref={dialog} className="pic-dialog" aria-label={`${title ? `${title}: ` : ''}${label}`}
        onClose={() => setBig(false)}
        onKeyDown={(e) => { if (e.key === 'Escape') e.stopPropagation(); }}
        onClick={(e) => { if (e.target === dialog.current) setBig(false); }}
      >
        {big && (
          <div className="pic-dialog-body">
            <div className="pic-dialog-head">
              <p className="kicker">{title}</p>
              <button type="button" className="iconbtn" autoFocus aria-label="Close the large view" onClick={() => setBig(false)}>
                <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M1 1l10 10M11 1L1 11" stroke="currentColor" strokeWidth="1.25" /></svg>
              </button>
            </div>
            <div className="pic-big"><Stage {...stage} /></div>
            {controls}
            <p className="pic-caption">{caption(shown, newest)}</p>
          </div>
        )}
      </dialog>
    </section>
  );
}

/** One request whose answer belongs to `id`; `attempt` re-asks after a failure. */
function usePatchRequest(id, attempt, request) {
  const [state, setState] = useState({ id: null });
  useEffect(() => {
    if (!API_BASE) return undefined;
    const [ra, dec, side] = id.split(',').map(Number);
    const ctrl = new AbortController();
    request({ ra, dec, side }, { signal: ctrl.signal }).then(
      (data) => { if (!ctrl.signal.aborted) setState({ id, attempt, status: 'ok', data }); },
      (err) => { if (!ctrl.signal.aborted) setState({ id, attempt, status: 'error', error: err?.message || String(err) }); },
    );
    return () => ctrl.abort();
  }, [id, attempt, request]);
  // Until this patch's (and this attempt's) answer arrives, it is loading.
  return state.id === id && state.attempt === attempt ? state : { status: 'loading' };
}

/**
 * Pictures of a catalogued star before (or without) any engine run: 2MASS and WISE images of a
 * patch that holds its 1999 and today positions, then one SPHEREx exposure per pass of the same
 * patch, each marked where its catalogued motion puts it on that date.
 */
export function KnownPictures({ row, title }) {
  const nowYear = useMemo(() => epochNow(), []);
  const motion = useMemo(() => knownMotion(row, nowYear), [row, nowYear]);
  const patch = useMemo(() => picturePatch(motion, nowYear), [motion, nowYear]);
  // Requests depend on the numbers only, so a re-rendered row does not ask again.
  const id = `${patch.ra.toFixed(6)},${patch.dec.toFixed(6)},${patch.side}`;
  const [surveysAttempt, setSurveysAttempt] = useState(0);
  const [framesAttempt, setFramesAttempt] = useState(0);
  const s = usePatchRequest(id, surveysAttempt, fetchPatchSurveys);
  const f = usePatchRequest(id, framesAttempt, fetchPatchFrames);
  const pictures = useMemo(() => knownViews({
    motion, patch, surveys: s.status === 'ok' ? s.data : null, frames: f.status === 'ok' ? f.data : null,
    cutoutUrl: (fr) => frameImageUrl(fr),
  }), [motion, patch, s.status, s.data, f.status, f.data]);

  if (!API_BASE) return <p className="line dim">The telescope pictures need the AstroSearchX server, which is not connected.</p>;

  const notes = [];
  if (s.status === 'loading') notes.push({ key: 's', text: 'Loading 2MASS (about 2000) and WISE (about 2010)…' });
  if (s.status === 'error') notes.push({ key: 's', text: `2MASS and WISE are unavailable: ${s.error}.` });
  for (const g of s.status === 'ok' ? s.data.unavailable : []) notes.push({ key: `g:${g.survey}`, text: `${g.label}: ${g.reason}` });
  if (f.status === 'loading') notes.push({ key: 'f', text: 'Listing SPHEREx exposures…' });
  if (f.status === 'error') notes.push({ key: 'f', text: `SPHEREx exposures are unavailable: ${f.error}.` });
  if (f.status === 'ok' && f.data.length === 0) notes.push({ key: 'f', text: 'SPHEREx has not imaged this spot yet.' });
  return (
    <>
      <SkyPictures views={pictures.views} frames={pictures.frames} initialFrame={pictures.initial} notes={notes} title={title} />
      {(s.status === 'error' || f.status === 'error') && (
        <p className="line">
          {s.status === 'error' && <button type="button" className="textbtn" onClick={() => setSurveysAttempt(surveysAttempt + 1)}>Retry 2MASS and WISE</button>}
          {s.status === 'error' && f.status === 'error' && ' '}
          {f.status === 'error' && <button type="button" className="textbtn" onClick={() => setFramesAttempt(framesAttempt + 1)}>Retry SPHEREx</button>}
        </p>
      )}
      {motion.source && <p className="pic-key"><i className="pic-key-ring pic-tone-catalogue" />where the {motion.source} motion puts it on each date</p>}
    </>
  );
}
