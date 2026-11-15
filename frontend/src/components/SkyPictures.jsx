import { useEffect, useMemo, useRef, useState } from 'react';
import { prefersReducedMotion } from '../lib/camera.js';
import { decimalYear } from '../lib/coords.js';
import { scaleBar } from '../lib/evidence.js';
import {
  fetchPatchFrames, fetchPatchSurveys, frameImageUrl, knownMotion, knownViews, picturePatch,
} from '../lib/pictures.js';
import { API_BASE } from '../lib/services.js';
import './SkyPictures.css';

const STEP_MS = 1000;

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

/** Marks on one picture, in arcsec from its centre (east is left, north is up). */
function Marks({ view }) {
  const { side, marks } = view;
  const h = side / 2;
  const r = side * 0.045;
  const [tin, tout] = [side * 0.06, side * 0.1];
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

/** The square with every picture stacked in it; only the current one is visible. */
function Stage({ all, current, showMarks, images, onImage }) {
  const bar = current ? scaleBar(current.side) : null;
  const state = current ? images[current.key] : null;
  return (
    <span className="pic-sky">
      {all.map((v) => (
        <img
          key={`${v.key}:${images[v.key]?.attempt || 0}`}
          className={v === current && images[v.key]?.status === 'ok' ? 'pic-on' : ''}
          src={images[v.key]?.attempt ? `${v.src}${v.src.includes('?') ? '&' : '?'}retry=${images[v.key].attempt}` : v.src}
          alt="" draggable="false"
          onLoad={() => onImage(v.key, 'ok')}
          onError={() => onImage(v.key, 'error')}
        />
      ))}
      {current && state?.status === 'ok' && showMarks && <Marks view={current} />}
      {current && state?.status !== 'ok' && (
        <span className="pic-wait" role="status">
          {state?.status === 'error'
            ? 'This picture could not be loaded.'
            : <><span className="pic-ticker" aria-hidden="true" />Loading {current.telescope} picture{current.telescope === 'SPHEREx' && current.src.startsWith('http') ? ' (about 4 MB from IRSA the first time)' : ''}</>}
        </span>
      )}
      {current && (
        <span className="pic-tag pic-when">
          <b>{current.telescope}</b> {current.when}
        </span>
      )}
      <Compass />
      {current && (
        <span className="pic-tag pic-scale" aria-hidden="true" style={{ '--bar': `${(bar / current.side) * 100}%` }}>
          <i />{bar >= 60 ? `${bar / 60}′` : `${bar}″`}
        </span>
      )}
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
        {views.map((v) => (
          <li key={v.key}>
            <button
              type="button" className={`pic-opt ${v === current ? 'pic-on' : ''} ${images[v.key]?.status === 'error' ? 'pic-failed' : ''}`}
              aria-pressed={v === current} aria-label={`${v.telescope}, ${v.when}`} onClick={() => onShow(v.key)}
            >
              <span className="pic-opt-scope">{v.telescope}</span> {v.button.slice(v.telescope.length + 1)}
            </button>
          </li>
        ))}
      </ol>
      <span className="pic-extra">
        {extras.map((v) => (
          <button key={v.key} type="button" className={`pic-opt ${v === current ? 'pic-on' : ''}`} aria-pressed={v === current} onClick={() => onShow(v.key)}>
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
 * Props: `views` time-ordered pictures (see knownViews / engineViews in lib/pictures.js),
 * `extras` pictures outside the sequence (the difference image), `notes` [{ key, text }] for what
 * is still loading or missing, `title` for the large view.
 */
export default function SkyPictures({ views, extras = [], notes = [], title }) {
  const all = useMemo(() => [...views, ...extras], [views, extras]);
  const [key, setKey] = useState(null);
  const [playing, setPlaying] = useState(() => !prefersReducedMotion());
  const [showMarks, setShowMarks] = useState(true);
  const [images, setImages] = useState({}); // key -> { status: 'ok' | 'error', attempt }
  const [big, setBig] = useState(false);
  const dialog = useRef(null);

  const current = all.find((v) => v.key === key) || views[0] || null;
  const loaded = views.filter((v) => images[v.key]?.status === 'ok');
  const newest = views[views.length - 1];

  // Playing steps through the pictures that have arrived, in date order.
  const loadedKeys = loaded.map((v) => v.key).join('\n');
  const firstKey = views[0]?.key ?? null;
  useEffect(() => {
    const keys = loadedKeys ? loadedKeys.split('\n') : [];
    if (!playing || keys.length < 2) return undefined;
    const timer = setInterval(() => {
      setKey((k) => keys[(keys.indexOf(k ?? firstKey) + 1) % keys.length]);
    }, STEP_MS);
    return () => clearInterval(timer);
  }, [playing, loadedKeys, firstKey]);

  useEffect(() => {
    const d = dialog.current;
    if (big && d && !d.open) d.showModal();
    if (!big && d?.open) d.close();
  }, [big]);

  const show = (k) => { setPlaying(false); setKey(k); };
  const step = (by) => {
    if (!views.length) return;
    const i = views.findIndex((v) => v === current);
    show(views[(Math.max(i, 0) + by + views.length) % views.length].key);
  };
  const onImage = (k, status) => setImages((m) => ({ ...m, [k]: { ...m[k], status } }));
  const retry = (k) => setImages((m) => ({ ...m, [k]: { status: null, attempt: (m[k]?.attempt || 0) + 1 } }));

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

  const controls = (
    <Controls
      views={views} extras={extras} current={current} playing={playing && loaded.length >= 2} showMarks={showMarks} images={images}
      onPlay={() => setPlaying(!playing)} onStep={step} onShow={show} onMarks={() => setShowMarks(!showMarks)}
    />
  );
  const label = `${current.telescope} picture, ${current.when}`;
  const failed = images[current.key]?.status === 'error';

  return (
    <section className="pic" aria-label="Telescope pictures">
      <button type="button" className="pic-frame" aria-label={`Enlarge: ${label}`} aria-haspopup="dialog" onClick={() => { setPlaying(false); setBig(true); }}>
        <Stage all={all} current={current} showMarks={showMarks} images={images} onImage={onImage} />
        <span className="pic-tag pic-zoom" aria-hidden="true">
          <svg width="11" height="11" viewBox="0 0 11 11"><path d="M1 4V1h3M7 1h3v3M10 7v3H7M4 10H1V7" fill="none" stroke="currentColor" strokeWidth="1.25" /></svg>
        </span>
      </button>
      {controls}
      <p className="pic-caption" aria-live={playing ? 'off' : 'polite'}>
        {caption(current, newest)}
        {failed && <> <button type="button" className="textbtn" onClick={() => retry(current.key)}>Retry</button></>}
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
            <div className="pic-big"><Stage all={all} current={current} showMarks={showMarks} images={images} onImage={onImage} /></div>
            {controls}
            <p className="pic-caption">{caption(current, newest)}</p>
          </div>
        )}
      </dialog>
    </section>
  );
}

/**
 * Pictures of a catalogued star before (or without) any engine run: 2MASS and WISE images of a
 * patch that holds its 1999 and today positions, then one SPHEREx exposure per pass of the same
 * patch, each marked where its catalogued motion puts it on that date.
 */
export function KnownPictures({ row, title }) {
  const nowYear = useMemo(() => decimalYear(), []);
  // Depends on the numbers only, so a re-rendered row does not ask again.
  const motion = useMemo(() => knownMotion(row, nowYear), [row, nowYear]);
  const patch = useMemo(() => picturePatch(motion, nowYear), [motion, nowYear]);
  const id = `${patch.ra.toFixed(6)},${patch.dec.toFixed(6)},${patch.side}`;
  const [surveys, setSurveys] = useState({ id: null });
  const [frames, setFrames] = useState({ id: null });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!API_BASE) return undefined;
    const [ra, dec, side] = id.split(',').map(Number);
    const patch = { ra, dec, side };
    const ctrl = new AbortController();
    const fail = (set) => (err) => { if (!ctrl.signal.aborted) set({ id, status: 'error', error: err?.message || String(err) }); };
    fetchPatchSurveys(patch, { signal: ctrl.signal }).then((data) => { if (!ctrl.signal.aborted) setSurveys({ id, status: 'ok', data }); }, fail(setSurveys));
    fetchPatchFrames(patch, { signal: ctrl.signal }).then((data) => { if (!ctrl.signal.aborted) setFrames({ id, status: 'ok', data }); }, fail(setFrames));
    return () => ctrl.abort();
  }, [id, attempt]);

  const s = surveys.id === id ? surveys : { status: 'loading' };
  const f = frames.id === id ? frames : { status: 'loading' };
  const views = useMemo(() => knownViews({
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
  const failed = s.status === 'error' || f.status === 'error';
  return (
    <>
      <SkyPictures views={views} notes={notes} title={title} />
      {failed && <p className="line"><button type="button" className="textbtn" onClick={() => setAttempt(attempt + 1)}>Retry</button></p>}
      {motion.source && <p className="pic-key"><i className="pic-key-ring pic-tone-catalogue" />where the {motion.source} motion puts it on each date</p>}
    </>
  );
}
