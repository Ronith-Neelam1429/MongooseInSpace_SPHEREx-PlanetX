import { useEffect, useMemo, useRef, useState } from 'react';
import { prefersReducedMotion } from '../lib/camera.js';
import { decimalYear } from '../lib/coords.js';
import { fetchPatchSurveys, scaleBar } from '../lib/evidence.js';
import {
  fetchPatchFrames, frameImageUrl, imageSrc, isRemote, knownMotion, knownViews, picturePatch, stepKey, wantedKeys,
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

/** Marks on the shared frame (`side` arcsec), in arcsec from its centre (east is left, north is up). */
function Marks({ view, side }) {
  const { marks } = view;
  const h = side / 2;
  const r = Math.max(RING_MIN_ARCSEC, side * 0.04);
  const [tin, tout] = [r * 1.35, r * 2.2];
  const p = (o) => ({ x: -o.east, y: -o.north });
  const ref = marks.ref && p(marks.ref);
  return (
    <svg className={`pic-marks pic-tone-${marks.tone}`} viewBox={`${-h} ${-h} ${side} ${side}`} aria-hidden="true">
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

/** Where an image's box sits in the shared frame, as CSS percentages. */
function placement(box, side) {
  const pct = (v) => `${(v / side) * 100}%`;
  return {
    left: pct(side / 2 - box.east - box.side / 2), top: pct(side / 2 - box.north - box.side / 2),
    width: pct(box.side), height: pct(box.side),
  };
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
function Stage({ all, side, current, wanted, showMarks, images, onImage }) {
  const bar = scaleBar(side);
  const state = images[current.key];
  const shown = effective(current, state);
  return (
    <span className="pic-sky">
      {all.filter((v) => !isRemote(v.src) || wanted.has(v.key)).map((v) => {
        const s = images[v.key];
        const e = effective(v, s);
        return (
          <img
            key={`${v.key}:${s?.attempt || 0}:${s?.alt || 0}`}
            className={v.key === current.key && s?.status === 'ok' ? 'pic-on' : ''}
            style={placement(e.box, side)}
            src={imageSrc(e.src, s?.attempt)}
            alt="" draggable="false"
            onLoad={() => onImage(v, 'ok')}
            onError={() => onImage(v, 'error')}
          />
        );
      })}
      {state?.status === 'ok' && showMarks && <Marks view={shown} side={side} />}
      {state?.status !== 'ok' && (
        <span className="pic-wait" role="status">
          {state?.status === 'error'
            ? 'This picture could not be loaded.'
            : <><span className="pic-ticker" aria-hidden="true" />Loading {current.telescope} picture{current.telescope === 'SPHEREx' && isRemote(current.src) ? ' (about 4 MB from IRSA the first time)' : ''}</>}
        </span>
      )}
      <span className="pic-tag pic-when"><b>{shown.telescope}</b> {shown.when}</span>
      <Compass />
      <span className="pic-tag pic-scale" aria-hidden="true" style={{ '--bar': `${(bar / side) * 100}%` }}>
        <i />{bar >= 60 ? `${bar / 60}′` : `${bar}″`}
      </span>
    </span>
  );
}

const PlayIcon = ({ playing }) => (playing
  ? <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M1.5 0v12M8.5 0v12" stroke="currentColor" strokeWidth="2" /></svg>
  : <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M0 0l10 6-10 6z" fill="currentColor" /></svg>);

/** Play, step and pick: arrow keys step too; every action is also a button. */
function Controls({ views, extras, current, playing, showMarks, images, onPlay, onStep, onShow, onMarks }) {
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
 * Props: `views` time-ordered pictures and `side` the shared frame they sit on (see knownViews /
 * engineViews in lib/pictures.js), `extras` pictures outside the sequence (the difference
 * image), `notes` [{ key, text }] for what is still loading or missing, `title` for the large view.
 * Remote pictures (SPHEREx cutouts, ~4 MB each from IRSA the first time) load one ahead of the
 * one shown, not all at once.
 */
export default function SkyPictures({ views, side, extras = [], notes = [], title }) {
  const all = useMemo(() => [...views, ...extras], [views, extras]);
  const [key, setKey] = useState(null);
  const [playing, setPlaying] = useState(() => !prefersReducedMotion());
  const [showMarks, setShowMarks] = useState(true);
  const [images, setImages] = useState({}); // key -> { status: 'ok' | 'error' | null, attempt, alt }
  const [seen, setSeen] = useState(() => new Set()); // keys shown so far: their images stay
  const [big, setBig] = useState(false);
  const dialog = useRef(null);

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

  if (!current) {
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
      views={views} extras={extras} current={current} playing={playing} showMarks={showMarks} images={images}
      onPlay={() => setPlaying(!playing)} onStep={step} onShow={show} onMarks={() => setShowMarks(!showMarks)}
    />
  );
  const label = `${shown.telescope} picture, ${shown.when}`;
  const failed = images[current.key]?.status === 'error';
  const stage = { all, side, current, wanted, showMarks, images, onImage };

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
  const nowYear = useMemo(() => decimalYear(), []);
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

  if (!API_BASE) return <p className="line dim">The telescope pictures need the PlanetX server, which is not connected.</p>;

  const notes = [];
  if (s.status === 'loading') notes.push({ key: 's', text: 'Loading 2MASS (about 2000) and WISE (about 2010)…' });
  if (s.status === 'error') notes.push({ key: 's', text: `2MASS and WISE are unavailable: ${s.error}.` });
  for (const g of s.status === 'ok' ? s.data.unavailable : []) notes.push({ key: `g:${g.survey}`, text: `${g.label}: ${g.reason}` });
  if (f.status === 'loading') notes.push({ key: 'f', text: 'Listing SPHEREx exposures…' });
  if (f.status === 'error') notes.push({ key: 'f', text: `SPHEREx exposures are unavailable: ${f.error}.` });
  if (f.status === 'ok' && f.data.length === 0) notes.push({ key: 'f', text: 'SPHEREx has not imaged this spot yet.' });
  return (
    <>
      <SkyPictures views={pictures.views} side={pictures.side} notes={notes} title={title} />
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
