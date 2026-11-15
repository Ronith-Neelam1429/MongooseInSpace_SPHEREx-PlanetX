import { useEffect, useMemo, useState } from 'react';
import { prefersReducedMotion } from '../lib/camera.js';
import { dayMonthYear, monthYear, scaleBar } from '../lib/evidence.js';
import { compass } from '../lib/plain.js';
import { API_BASE } from '../lib/services.js';
import {
  SPHEREX_DETECTOR, fetchArchive, fetchFrameImage, fetchSpherexFrames, marksFor, shiftText, timelapsePatch, yearOf,
} from '../lib/timelapse.js';
import './MotionEvidence.css';
import './StarTimelapse.css';

const STEP_MS = 1100;
const MOON_ARCSEC = 1865;
const ARCHIVE = {
  '2mass': { short: '2MASS', band: 'Ks, 2.2 µm' },
  wise: { short: 'WISE', band: 'W1, 3.4 µm' },
};
const DETECTOR_BAND = { 1: '0.75–1.1 µm', 2: '1.1–1.65 µm', 3: '1.6–2.4 µm', 4: '2.4–3.85 µm', 5: '3.8–4.4 µm', 6: '4.4–5.0 µm' };

const errText = (err) => err?.message || String(err);

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

/** "1/7 of the full Moon's width" for an angle, or null when it is too small to say that way. */
function moonShare(arcsec) {
  if (arcsec < 30) return null;
  const n = Math.round(MOON_ARCSEC / arcsec);
  return n <= 1 ? 'about the width of the full Moon' : `about 1/${n} of the full Moon's width`;
}

/**
 * Real images of one catalogued star's patch of sky, oldest first: 2MASS (~2000), AllWISE
 * (~2010), then one SPHEREx exposure per pass. They play as a time-lapse with the catalogue's
 * position for each date ringed, so a fast star is seen crossing the field.
 */
export default function StarTimelapse({ row }) {
  const patch = useMemo(() => timelapsePatch(row), [row]);
  const [archive, setArchive] = useState({ status: 'loading' });
  const [spherex, setSpherex] = useState({ status: 'loading' });
  const [srcs, setSrcs] = useState({}); // SPHEREx frame image_url -> object URL | { error }
  const [view, setView] = useState(0);
  const [playing, setPlaying] = useState(() => !prefersReducedMotion());
  const [marks, setMarks] = useState(true);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!API_BASE) return undefined;
    const ctrl = new AbortController();
    const urls = [];
    const alive = () => !ctrl.signal.aborted;
    fetchArchive(patch, { signal: ctrl.signal }).then(
      (data) => alive() && setArchive({ status: 'ok', data }),
      (err) => alive() && setArchive({ status: 'error', error: errText(err) }),
    );
    fetchSpherexFrames(patch, { signal: ctrl.signal }).then((frames) => {
      if (!alive()) return;
      setSpherex({ status: 'ok', frames });
      for (const f of frames) {
        fetchFrameImage(f, { signal: ctrl.signal }).then((url) => {
          urls.push(url);
          if (alive()) setSrcs((s) => ({ ...s, [f.image_url]: url }));
          else URL.revokeObjectURL(url);
        }, (err) => alive() && setSrcs((s) => ({ ...s, [f.image_url]: { error: errText(err) } })));
      }
    }, (err) => alive() && setSpherex({ status: 'error', error: errText(err) }));
    return () => {
      ctrl.abort();
      urls.forEach((u) => URL.revokeObjectURL(u));
    };
  }, [patch, attempt]);

  // Every image, oldest first. An image still loading has no `src` yet and is skipped when playing.
  const images = useMemo(() => {
    const out = [];
    if (archive.status === 'ok') {
      for (const im of archive.data.images) {
        const a = ARCHIVE[im.survey] || { short: im.survey, band: '' };
        out.push({
          key: `archive:${im.survey}`, year: yearOf(im), date: im.date, src: `data:image/png;base64,${im.image_png_b64}`,
          button: `${a.short} ${im.date.slice(0, 4)}`, tag: `${a.short} · ${im.date.slice(0, 4)}`,
          what: `${im.label} (${a.band.split(', ')[1] || a.band})${im.survey === 'wise' ? ', mean date of its exposures' : ''}`,
        });
      }
    }
    if (spherex.status === 'ok') {
      for (const f of spherex.frames) {
        const got = srcs[f.image_url];
        out.push({
          key: `spherex:${f.image_url}`, year: yearOf(f), date: f.date, src: typeof got === 'string' ? got : null,
          error: got?.error, button: `SPHEREx ${monthYear(f.date)}`, tag: `SPHEREx · ${monthYear(f.date)}`,
          what: `SPHEREx detector ${f.detector} (${DETECTOR_BAND[f.detector] || ''}), pass ${f.pass}${f.n_pass > 1 ? `, one of ${f.n_pass} exposures` : ''}`,
        });
      }
    }
    return out.sort((a, b) => a.year - b.year);
  }, [archive, spherex, srcs]);

  const ready = images.filter((im) => im.src);
  const current = images[Math.min(view, images.length - 1)];

  useEffect(() => {
    if (!playing || ready.length < 2) return undefined;
    const timer = setInterval(() => {
      setView((v) => {
        // The next image that has loaded.
        for (let k = 1; k <= images.length; k++) {
          const i = (v + k) % images.length;
          if (images[i].src) return i;
        }
        return v;
      });
    }, STEP_MS);
    return () => clearInterval(timer);
  }, [playing, ready.length, images]);

  if (!API_BASE) return <p className="line dim">The images need the PlanetX server, which is not connected.</p>;

  const loading = archive.status === 'loading' || spherex.status === 'loading'
    || (spherex.status === 'ok' && spherex.frames.some((f) => !srcs[f.image_url]));
  const failed = archive.status === 'error' && spherex.status === 'error';
  const retry = () => {
    setArchive({ status: 'loading' });
    setSpherex({ status: 'loading' });
    setSrcs({});
    setAttempt((n) => n + 1);
  };
  const show = (i) => { setPlaying(false); setView(i); };
  const step = (by) => images.length && show((view + by + images.length) % images.length);
  const onKeyDown = (ev) => {
    if (ev.key === 'ArrowRight') { ev.preventDefault(); step(1); }
    if (ev.key === 'ArrowLeft') { ev.preventDefault(); step(-1); }
  };

  const first = images[0], last = images[images.length - 1];
  const years = images.map((im) => im.year);
  const h = patch.side / 2;
  const m = current ? marksFor(row, patch, current.year, years) : null;
  const ring = Math.max(6, patch.side * 0.04);
  const source = row.gaia ? 'Gaia' : 'SIMBAD';
  const moved = first && last && first !== last && row.pm != null ? row.pm * (last.year - first.year) : null;
  const pxSpherex = 6.15;

  return (
    <div className="ev tl">
      {moved != null && (
        <p className="ev-line tl-lede">
          {moved >= pxSpherex
            ? <>Between {first.date.slice(0, 4)} and {last.date.slice(0, 4)} it moved <b className="signal">{shiftText(moved)}</b> toward the {compass(row.pa)}{moonShare(moved) ? `, ${moonShare(moved)}` : ''}. Watch it cross the field.</>
            : <>Between {first.date.slice(0, 4)} and {last.date.slice(0, 4)} it moved only {shiftText(moved)}, less than one SPHEREx pixel, so it stays put in these images.</>}
        </p>
      )}

      <div className="ev-view tl-view">
        <div className="ev-sky" role="img" aria-label={current ? `${current.what}, ${dayMonthYear(current.date)}` : 'Images loading'}>
          {images.map((im, i) => im.src && (
            <img key={im.key} className={i === view ? 'ev-on' : ''} src={im.src} alt="" draggable="false" />
          ))}
          {current && !current.src && (
            <p className="tl-wait">
              {current.error
                ? `This image could not be loaded: ${current.error}.`
                : <><span className="ev-ticker" aria-hidden="true" />Fetching from IRSA</>}
            </p>
          )}
          {!current && (
            <p className="tl-wait">
              {failed ? 'No images could be loaded.' : <><span className="ev-ticker" aria-hidden="true" />Finding images of this spot</>}
            </p>
          )}
          {m && marks && (
            <svg className="ev-marks" viewBox={`${-h} ${-h} ${patch.side} ${patch.side}`} aria-hidden="true">
              {m.path.length > 1 && (
                <polyline className="tl-path" points={m.path.map((p) => `${-p.east},${-p.north}`).join(' ')} />
              )}
              {m.path.map((p, i) => <circle key={images[i].key} className="tl-dot" r={ring * 0.12} cx={-p.east} cy={-p.north} />)}
              {m.inside && <circle className="tl-ring" r={ring} cx={-m.at.east} cy={-m.at.north} />}
            </svg>
          )}
          {current && <span className="ev-tag ev-when">{current.tag}</span>}
          <Compass />
          <svg className="ev-marks" viewBox={`${-h} ${-h} ${patch.side} ${patch.side}`} aria-hidden="true">
            <line className="ev-bar" x1={-0.92 * h} x2={-0.92 * h + scaleBar(patch.side)} y1={0.91 * h} y2={0.91 * h} />
          </svg>
          <span className="ev-tag ev-scale" aria-hidden="true">{scaleBar(patch.side) >= 60 ? `${scaleBar(patch.side) / 60}′` : `${scaleBar(patch.side)}″`}</span>
        </div>

        {images.length > 0 && (
          <div className="tl-track" aria-hidden="true">
            <i style={{ width: `${(100 * (view + 1)) / images.length}%` }} />
          </div>
        )}

        <div className="ev-controls" role="group" aria-label="Images by date" onKeyDown={onKeyDown}>
          <button
            type="button" className="ev-iconbtn" disabled={ready.length < 2}
            aria-label={playing ? 'Pause the time-lapse' : 'Play the time-lapse'} onClick={() => setPlaying(!playing)}
          >
            {playing
              ? <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M1.5 0v12M8.5 0v12" stroke="currentColor" strokeWidth="2" /></svg>
              : <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M0 0l10 6-10 6z" fill="currentColor" /></svg>}
          </button>
          {images.map((im, i) => (
            <button
              key={im.key} type="button" className={`ev-opt ${i === view ? 'ev-on' : ''}`} disabled={!im.src}
              aria-pressed={i === view} aria-label={`${im.what}, ${dayMonthYear(im.date)}`} onClick={() => show(i)}
            >
              {im.button}
            </button>
          ))}
          <span className="ev-extra">
            <button type="button" className={`ev-opt ${marks ? 'ev-on' : ''}`} aria-pressed={marks} onClick={() => setMarks(!marks)}>Marks</button>
          </span>
        </div>

        {current && (
          <p className="ev-caption" aria-live={playing ? 'off' : 'polite'}>
            {current.what} · {dayMonthYear(current.date)}.
            {m && !m.inside && ` ${source} puts the star outside this image on that date.`}
          </p>
        )}
        {loading && current && <p className="ev-state"><span className="ev-ticker" aria-hidden="true" />More images loading. The first time takes up to a minute.</p>}
        {(archive.status === 'error' || spherex.status === 'error') && (
          <p className="ev-state ev-err" role="alert">
            {archive.status === 'error' && <>2MASS and WISE could not be loaded ({archive.error}). </>}
            {spherex.status === 'error' && <>SPHEREx could not be loaded ({spherex.error}). </>}
            <button type="button" className="ev-textbtn" onClick={retry}>Try again</button>
          </p>
        )}
        {spherex.status === 'ok' && spherex.frames.length === 0 && <p className="ev-state">SPHEREx has no images of this spot yet.</p>}
        {archive.status === 'ok' && archive.data.unavailable.map((g) => (
          <p key={g.survey} className="ev-state">{ARCHIVE[g.survey]?.short || g.label}: {g.reason}</p>
        ))}

        <p className="ev-note">
          Real telescope images, north up, east left, each on its own grey scale.{' '}
          <span className="ev-key"><i className="tl-key-ring" />where {source} puts it on the date shown</span>{' '}
          <span className="ev-key"><i className="tl-key-path" />its path</span>
          {spherex.status === 'ok' && spherex.frames.length > 0 && spherex.frames[0].detector !== SPHEREX_DETECTOR
            ? ' SPHEREx images are from another detector: detector 3 did not cover this spot.'
            : ' SPHEREx detector 3 sees about the same colour as 2MASS Ks.'}
          {' '}SPHEREx pixels are 6″, 2MASS and WISE about 1″, so SPHEREx looks blockier.
        </p>
      </div>
    </div>
  );
}
