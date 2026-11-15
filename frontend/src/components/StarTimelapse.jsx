import { useEffect, useMemo, useRef, useState } from 'react';
import { prefersReducedMotion } from '../lib/camera.js';
import { dayMonthYear, monthYear, scaleBar } from '../lib/evidence.js';
import { compass, positionAngle, speed } from '../lib/plain.js';
import { API_BASE } from '../lib/services.js';
import {
  SPHEREX_DETECTOR, fetchArchive, fetchFrameData, fetchSpherexFrames, marksFor, shiftText, timelapsePatch, yearOf,
} from '../lib/timelapse.js';
import {
  FWHM_ARCSEC, blur, buildTrace, gridFromFloats, gridToSky, greyRgba, loadGrid, matchSigma, overlayRgba, skyToGrid, stretch, timeColor, tone, traceRgba, zmap,
} from '../lib/imaging.js';
import './MotionEvidence.css';
import './StarTimelapse.css';

const STEP_MS = 1100;
/** Side of the common grid every image is put on for matching and overlay, in pixels. */
const GRID = 320;
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
  const [srcs, setSrcs] = useState({}); // SPHEREx frame image_url -> { width, height, pixels } | { error }
  const [view, setView] = useState(0);
  const [playing, setPlaying] = useState(() => !prefersReducedMotion());
  const [marks, setMarks] = useState(true);
  // 'trace': the fixed sky in grey with the star's own light from each image along its path;
  // 'overlay': two dates in one colour picture; 'timelapse': one image after another.
  const [mode, setMode] = useState('trace');
  // Time-lapse images on one look (same sharpness and sky stretch), or as each telescope gave them.
  const [matched, setMatched] = useState(true);
  const [pair, setPair] = useState({ then: null, now: null }); // image keys; null = oldest / newest
  const [grids, setGrids] = useState({}); // image key -> { sharp, soft, z, cover } on the GRID, or { error }
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!API_BASE) return undefined;
    const ctrl = new AbortController();
    const alive = () => !ctrl.signal.aborted;
    fetchArchive(patch, { signal: ctrl.signal }).then(
      (data) => alive() && setArchive({ status: 'ok', data }),
      (err) => alive() && setArchive({ status: 'error', error: errText(err) }),
    );
    fetchSpherexFrames(patch, { signal: ctrl.signal }).then((frames) => {
      if (!alive()) return;
      setSpherex({ status: 'ok', frames });
      for (const f of frames) {
        fetchFrameData(f, { signal: ctrl.signal }).then((got) => {
          if (alive()) setSrcs((s) => ({ ...s, [f.image_url]: got }));
        }, (err) => alive() && setSrcs((s) => ({ ...s, [f.image_url]: { error: errText(err) } })));
      }
    }, (err) => alive() && setSpherex({ status: 'error', error: errText(err) }));
    return () => ctrl.abort();
  }, [patch, attempt]);

  // Every image, oldest first. `src` is a PNG data URL (2MASS, WISE) or float pixels (SPHEREx); an
  // image still loading has none yet and is skipped when playing.
  const images = useMemo(() => {
    const out = [];
    if (archive.status === 'ok') {
      for (const im of archive.data.images) {
        const a = ARCHIVE[im.survey] || { short: im.survey, band: '' };
        out.push({
          key: `archive:${im.survey}`, kind: im.survey === 'wise' ? 'wise' : '2mass', year: yearOf(im), date: im.date, src: `data:image/png;base64,${im.image_png_b64}`,
          button: `${a.short} ${im.date.slice(0, 4)}`, tag: `${a.short} · ${im.date.slice(0, 4)}`,
          what: `${im.label} (${a.band.split(', ')[1] || a.band})${im.survey === 'wise' ? ', mean date of its exposures' : ''}`,
        });
      }
    }
    if (spherex.status === 'ok') {
      for (const f of spherex.frames) {
        const got = srcs[f.image_url];
        out.push({
          key: `spherex:${f.image_url}`, kind: 'spherex', year: yearOf(f), date: f.date, src: got?.pixels ? got : null,
          error: got?.error, button: `SPHEREx ${monthYear(f.date)}`, tag: `SPHEREx · ${monthYear(f.date)}`,
          what: `SPHEREx detector ${f.detector} (${DETECTOR_BAND[f.detector] || ''}), pass ${f.pass}${f.n_pass > 1 ? `, one of ${f.n_pass} exposures` : ''}`,
        });
      }
    }
    return out.sort((a, b) => a.year - b.year);
  }, [archive, spherex, srcs]);

  const ready = images.filter((im) => im.src);
  const current = images[Math.min(view, images.length - 1)];

  // Put every loaded image on the common grid: stretched as it is (sharp) and blurred to SPHEREx's
  // sharpness first (soft), so images from different telescopes look alike.
  useEffect(() => {
    let alive = true;
    const pixel = patch.side / GRID;
    for (const im of images) {
      if (!im.src || grids[im.key]) continue;
      const grid = typeof im.src === 'string'
        ? loadGrid(im.src, GRID, { linear: true })
        : Promise.resolve(gridFromFloats(im.src.pixels, im.src.width, im.src.height, GRID));
      grid.then(({ data, mask }) => {
        const sigma = matchSigma(FWHM_ARCSEC[im.kind], FWHM_ARCSEC.spherex, pixel);
        const sharp = stretch(data, mask);
        const z = zmap(blur(data, mask, GRID, GRID, sigma), mask);
        const soft = tone(z);
        const cover = mask.reduce((n, v) => n + v, 0) / mask.length;
        if (alive) setGrids((g) => (g[im.key] ? g : { ...g, [im.key]: { sharp, soft, z, cover } }));
      }, (err) => alive && setGrids((g) => ({ ...g, [im.key]: { error: errText(err) } })));
    }
    return () => { alive = false; };
  }, [images, grids, patch.side]);

  useEffect(() => {
    if (mode !== 'timelapse' || !playing || ready.length < 2) return undefined;
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
  }, [mode, playing, ready.length, images]);

  const usable = images.filter((im) => grids[im.key]?.soft);
  // By default the oldest and newest images, among those that cover most of the patch.
  const full = usable.filter((im) => grids[im.key].cover >= 0.9);
  const pool = full.length >= 2 ? full : usable;
  const older = usable.find((im) => im.key === pair.then) || pool[0];
  const newer = usable.find((im) => im.key === pair.now) || pool[pool.length - 1];
  const overlayReady = mode === 'overlay' && older && newer && older !== newer;

  // The trace: images lined up on the fixed stars, and the star found in each.
  const pix = patch.side / GRID;
  const fwhmPx = FWHM_ARCSEC.spherex / pix;
  const traceKeys = usable.map((im) => im.key).join('|');
  const trace = useMemo(() => {
    if (mode !== 'trace' || !usable.length) return null;
    const items = usable.map((im) => {
      const at = marksFor(row, patch, im.year, []).at;
      const g = skyToGrid(at.east, at.north, GRID, patch.side);
      return { key: im.key, z: grids[im.key].z, cover: grids[im.key].cover, at: { x: g.col, y: g.row } };
    });
    return buildTrace(items, GRID, fwhmPx, FWHM_ARCSEC.spherex / pix);
    // traceKeys names the images; grids only gains entries, so the keys say when to recompute.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, traceKeys, row, patch, fwhmPx, pix]);
  const story = trace ? traceStory(trace, usable, row, patch) : null;

  // What the canvas shows: the trace, the overlay, or the current image on the matched look.
  const canvas = useRef(null);
  const thenKey = overlayReady ? older.key : null, nowKey = overlayReady ? newer.key : null;
  const viewKey = mode === 'timelapse' && current ? current.key : null;
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    let picture = null;
    if (trace) picture = traceRgba(trace.backdrop, GRID, story?.moved ? traceStamps(trace, usable, fwhmPx) : []);
    else if (thenKey && nowKey) picture = overlayRgba(grids[thenKey].soft, grids[nowKey].soft);
    else if (viewKey && grids[viewKey]?.soft) picture = greyRgba(matched ? grids[viewKey].soft : grids[viewKey].sharp);
    const ctx = c.getContext('2d');
    ctx.clearRect(0, 0, GRID, GRID);
    if (picture) ctx.putImageData(new ImageData(picture, GRID, GRID), 0, 0);
  }, [trace, story?.moved, usable, fwhmPx, thenKey, nowKey, viewKey, matched, grids]);

  if (!API_BASE) return <p className="line dim">The images need the PlanetX server, which is not connected.</p>;

  const loading = archive.status === 'loading' || spherex.status === 'loading'
    || (spherex.status === 'ok' && spherex.frames.some((f) => !srcs[f.image_url]));
  const failed = archive.status === 'error' && spherex.status === 'error';
  const retry = () => {
    setArchive({ status: 'loading' });
    setSpherex({ status: 'loading' });
    setSrcs({});
    setGrids({});
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
  const ring = Math.max(6, patch.side * 0.04);
  const source = row.gaia ? 'Gaia' : 'SIMBAD';
  const moved = first && last && first !== last && row.pm != null ? row.pm * (last.year - first.year) : null;
  const pxSpherex = 6.15;
  const overlay = mode === 'overlay', tracing = mode === 'trace', timelapse = mode === 'timelapse';
  const m = timelapse && current ? marksFor(row, patch, current.year, years) : null;
  const mThen = overlayReady ? marksFor(row, patch, older.year, [older.year, newer.year]) : null;
  const mNow = overlayReady ? marksFor(row, patch, newer.year, years) : null;
  const pairMoved = overlayReady && row.pm != null ? row.pm * (newer.year - older.year) : null;
  const rawShown = timelapse && !matched;
  const waiting = overlay
    ? (!overlayReady && (usable.length < 2 ? (failed ? 'No images could be loaded.' : null) : null))
    : null;
  const short = (im) => `${im.button.split(' ')[0]} ${im.button.includes('SPHEREx') ? monthYear(im.date) : im.date.slice(0, 4)}`;

  let label = 'Images loading';
  if (overlay && overlayReady) label = `Overlay: ${older.what} (${dayMonthYear(older.date)}) in red, ${newer.what} (${dayMonthYear(newer.date)}) in cyan`;
  else if (timelapse && current) label = `${current.what}, ${dayMonthYear(current.date)}`;
  else if (tracing && trace) label = 'The fixed stars in grey, with the star itself in colour in each image where it was found, along its path';

  return (
    <div className="ev tl">
      {moved != null && (
        <p className="ev-line tl-lede">
          {moved >= pxSpherex
            ? <>Between {first.date.slice(0, 4)} and {last.date.slice(0, 4)} it moved <b className="signal">{shiftText(moved)}</b> toward the {compass(row.pa)}{moonShare(moved) ? `, ${moonShare(moved)}` : ''}.</>
            : <>Between {first.date.slice(0, 4)} and {last.date.slice(0, 4)} it moved only {shiftText(moved)}, less than one SPHEREx pixel, so it stays put in these images.</>}
        </p>
      )}

      <div className="ev-view tl-view">
        <div className="tl-modes" role="group" aria-label="How to compare the images">
          <button type="button" className={`ev-opt ${tracing ? 'ev-on' : ''}`} aria-pressed={tracing} onClick={() => setMode('trace')}>Trace</button>
          <button type="button" className={`ev-opt ${overlay ? 'ev-on' : ''}`} aria-pressed={overlay} onClick={() => setMode('overlay')}>Overlay</button>
          <button type="button" className={`ev-opt ${timelapse ? 'ev-on' : ''}`} aria-pressed={timelapse} onClick={() => setMode('timelapse')}>Time-lapse</button>
        </div>

        <div className="ev-sky" role="img" aria-label={label}>
          <canvas ref={canvas} className="tl-canvas ev-on" width={GRID} height={GRID} />

          {overlay && !overlayReady && (
            <p className="tl-wait">
              {waiting || <><span className="ev-ticker" aria-hidden="true" />{images.length ? 'Lining up the images' : 'Finding images of this spot'}</>}
            </p>
          )}
          {tracing && !trace && (
            <p className="tl-wait">
              {failed ? 'No images could be loaded.' : <><span className="ev-ticker" aria-hidden="true" />{images.length ? 'Lining up the images' : 'Finding images of this spot'}</>}
            </p>
          )}
          {tracing && story && marks && <TraceMarks story={story} side={patch.side} />}
          {timelapse && current && !current.src && (
            <p className="tl-wait">
              {current.error
                ? `This image could not be loaded: ${current.error}.`
                : <><span className="ev-ticker" aria-hidden="true" />Fetching from IRSA</>}
            </p>
          )}
          {timelapse && current?.src && !grids[current.key] && (
            <p className="tl-wait"><span className="ev-ticker" aria-hidden="true" />Lining up the image</p>
          )}
          {timelapse && !current && (
            <p className="tl-wait">
              {failed ? 'No images could be loaded.' : <><span className="ev-ticker" aria-hidden="true" />Finding images of this spot</>}
            </p>
          )}

          {m && marks && (
            <svg className="ev-marks" viewBox={`${-h} ${-h} ${patch.side} ${patch.side}`} aria-hidden="true">
              {m.path.length > 1 && <polyline className="tl-path" points={m.path.map((p) => `${-p.east},${-p.north}`).join(' ')} />}
              {m.path.map((p, i) => <circle key={images[i].key} className="tl-dot" r={ring * 0.12} cx={-p.east} cy={-p.north} />)}
              {m.inside && <circle className="tl-ring" r={ring} cx={-m.at.east} cy={-m.at.north} />}
            </svg>
          )}
          {mThen && mNow && marks && (
            <svg className="ev-marks" viewBox={`${-h} ${-h} ${patch.side} ${patch.side}`} aria-hidden="true">
              {pairMoved >= pxSpherex && (
                <>
                  <defs>
                    <marker id="tl-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                      <path d="M0 0L10 5L0 10z" className="tl-head" />
                    </marker>
                  </defs>
                  <line
                    className="tl-arrow" markerEnd="url(#tl-head)"
                    {...arrowEnds(mThen.at, mNow.at, ring * 1.25)}
                  />
                </>
              )}
              {mThen.inside && <circle className="tl-ring tl-then" r={ring} cx={-mThen.at.east} cy={-mThen.at.north} />}
              {mNow.inside && <circle className="tl-ring tl-now" r={ring} cx={-mNow.at.east} cy={-mNow.at.north} />}
            </svg>
          )}
          {overlay && overlayReady && <span className="ev-tag ev-when"><b className="tl-red">{short(older)}</b> → <b className="tl-cyan">{short(newer)}</b></span>}
          {timelapse && current && <span className="ev-tag ev-when">{current.tag}</span>}
          {tracing && story && <span className="ev-tag ev-when">Path {story.span}</span>}
          <Compass />
          <svg className="ev-marks" viewBox={`${-h} ${-h} ${patch.side} ${patch.side}`} aria-hidden="true">
            <line className="ev-bar" x1={-0.92 * h} x2={-0.92 * h + scaleBar(patch.side)} y1={0.91 * h} y2={0.91 * h} />
          </svg>
          <span className="ev-tag ev-scale" aria-hidden="true">{scaleBar(patch.side) >= 60 ? `${scaleBar(patch.side) / 60}′` : `${scaleBar(patch.side)}″`}</span>
        </div>

        {tracing && story && (
          <>
            <ul className="tl-legend" aria-label="Each image">
              {story.points.map((pt) => (
                <li key={pt.key} className={pt.seen ? '' : 'tl-unseen'}>
                  <i className="tl-swatch" style={{ background: pt.css }} />
                  <span className="tl-legend-name">{pt.button}</span>
                  <span className="tl-legend-what">
                    {pt.seen ? `found${pt.ref ? ' · reference' : ''}` : 'not found: too faint here'}
                  </span>
                </li>
              ))}
            </ul>
            <div className="ev-controls">
              <span className="ev-extra">
                <button type="button" className={`ev-opt ${marks ? 'ev-on' : ''}`} aria-pressed={marks} onClick={() => setMarks(!marks)}>Marks</button>
              </span>
            </div>
          </>
        )}
        {tracing ? null : overlay ? (
          <div className="ev-controls tl-pair" role="group" aria-label="Dates to overlay">
            <label className="tl-pick">
              <i className="tl-swatch tl-red-bg" />
              <span className="tl-pick-label">Then</span>
              <select value={older?.key ?? ''} disabled={usable.length < 2} onChange={(e) => setPair((p) => ({ ...p, then: e.target.value }))}>
                {usable.filter((im) => im !== newer).map((im) => <option key={im.key} value={im.key}>{im.button}</option>)}
              </select>
            </label>
            <label className="tl-pick">
              <i className="tl-swatch tl-cyan-bg" />
              <span className="tl-pick-label">Now</span>
              <select value={newer?.key ?? ''} disabled={usable.length < 2} onChange={(e) => setPair((p) => ({ ...p, now: e.target.value }))}>
                {usable.filter((im) => im !== older).map((im) => <option key={im.key} value={im.key}>{im.button}</option>)}
              </select>
            </label>
            <span className="ev-extra">
              <button type="button" className={`ev-opt ${marks ? 'ev-on' : ''}`} aria-pressed={marks} onClick={() => setMarks(!marks)}>Marks</button>
            </span>
          </div>
        ) : (
          <>
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
                <button
                  type="button" className={`ev-opt ${matched ? 'ev-on' : ''}`} aria-pressed={matched}
                  title="Blur every telescope to SPHEREx\u2019s sharpness" onClick={() => setMatched(!matched)}
                >
                  Matched
                </button>
                <button type="button" className={`ev-opt ${marks ? 'ev-on' : ''}`} aria-pressed={marks} onClick={() => setMarks(!marks)}>Marks</button>
              </span>
            </div>
          </>
        )}

        {tracing && story && <p className="ev-caption">{story.caption}</p>}
        {tracing && story && !story.moved && <CloseUps trace={trace} story={story} side={patch.side} />}
        {overlay && overlayReady && (
          <p className="ev-caption">
            <b className="tl-red">Red</b>: {older.what}, {dayMonthYear(older.date)}.{' '}
            <b className="tl-cyan">Cyan</b>: {newer.what}, {dayMonthYear(newer.date)}.{' '}
            Stars that stayed put add up to white.{' '}
            {pairMoved == null || pairMoved < pxSpherex
              ? `This star moved ${pairMoved == null ? 'too little to measure' : `only ${shiftText(pairMoved)}`} between the two, so it should look white too.`
              : `This one moved ${shiftText(pairMoved)}: red where it was, cyan where it is now.`}
            {mThen && !mThen.inside && ` ${source} puts it outside the image on the older date.`}
          </p>
        )}
        {timelapse && current && (
          <p className="ev-caption" aria-live={playing ? 'off' : 'polite'}>
            {current.what} · {dayMonthYear(current.date)}.
            {m && !m.inside && ` ${source} puts the star outside this image on that date.`}
          </p>
        )}
        {loading && images.length > 0 && <p className="ev-state"><span className="ev-ticker" aria-hidden="true" />More images loading. The first time takes up to a minute.</p>}
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
          Real telescope images, north up, east left.{' '}
          {rawShown
            ? 'Shown at each telescope\u2019s own sharpness, on a common grey scale (sky black, stars by their signal over the noise). '
            : 'To compare telescopes fairly, every image is blurred to SPHEREx’s sharpness and stretched from its own sky level, so the sky looks the same in all of them. '}
          {tracing && 'Grey: every image averaged after lining them up on the fixed stars, so only what never moves stays sharp. Colour: the star itself, cut from each image where it was found, oldest red to newest cyan. '}
          {tracing
            ? <><span className="ev-key"><i className="tl-key-path tl-key-solid" />path found in the images</span>{' '}<span className="ev-key"><i className="tl-key-path tl-key-faint" />{source}'s path</span></>
            : overlay
            ? <><span className="ev-key"><i className="tl-key-ring tl-then-key" />where {source} puts it then</span>{' '}<span className="ev-key"><i className="tl-key-ring tl-now-key" />and now</span></>
            : <><span className="ev-key"><i className="tl-key-ring" />where {source} puts it on the date shown</span>{' '}<span className="ev-key"><i className="tl-key-path" />its path</span></>}
          {spherex.status === 'ok' && spherex.frames.length > 0 && spherex.frames[0].detector !== SPHEREX_DETECTOR
            ? ' SPHEREx images are from another detector: detector 3 did not cover this spot.'
            : ' SPHEREx detector 3 sees about the same colour as 2MASS Ks; WISE W1 is redder, so some stars differ in brightness between them.'}
        </p>
      </div>
    </div>
  );
}

/** The arrow from the older ring to the newer one, stopping `gap` arcsec short of each centre. */
function arrowEnds(from, to, gap) {
  const x1 = -from.east, y1 = -from.north, x2 = -to.east, y2 = -to.north;
  const d = Math.hypot(x2 - x1, y2 - y1);
  if (d <= 2 * gap) return { x1, y1, x2, y2 };
  const ux = (x2 - x1) / d, uy = (y2 - y1) / d;
  return { x1: x1 + ux * gap, y1: y1 + uy * gap, x2: x2 - ux * gap, y2: y2 - uy * gap };
}

/** Where in [0, 1] an image's date lies between the oldest and newest image. */
const timeOf = (year, images) => {
  const y0 = images[0].year, y1 = images[images.length - 1].year;
  return y1 > y0 ? (year - y0) / (y1 - y0) : 1;
};

/** The star's own light from each image where it was found, coloured by date, for traceRgba. */
function traceStamps(trace, images, fwhmPx) {
  return trace.items.flatMap((it, i) => (it.found?.seen
    ? [{ z: it.z, x: it.found.x, y: it.found.y, radius: 1.4 * fwhmPx, snr: it.found.snr, rgb: timeColor(timeOf(images[i].year, images)) }]
    : []));
}

/**
 * What the trace shows, for the marks and the words: per image its colour, where the star was
 * found (east, north arcsec) or where the catalogue puts it, and a caption.
 */
function traceStory(trace, images, row, patch) {
  const pix = patch.side / GRID;
  const fwhm = FWHM_ARCSEC.spherex;
  const source = row.gaia ? 'Gaia' : 'SIMBAD';
  const points = trace.items.map((it, i) => {
    const im = images[i];
    const rgb = timeColor(timeOf(im.year, images));
    const pred = marksFor(row, patch, im.year, []).at;
    const seen = Boolean(it.found?.seen);
    return {
      key: it.key, button: im.button, year: im.year, date: im.date, seen, snr: it.found?.snr ?? 0, ref: it.key === trace.refKey,
      rgb, css: `rgb(${rgb.join(',')})`, pred, pos: seen ? gridToSky(it.found.x, it.found.y, GRID, patch.side) : pred,
    };
  });
  const seen = points.filter((p) => p.seen);
  const a = seen[0], b = seen[seen.length - 1];
  const yearSpan = `${points[0].date.slice(0, 4)}–${points[points.length - 1].date.slice(0, 4)}`;
  const shifts = trace.items.filter((it) => it.key !== trace.refKey);
  const lined = shifts.filter((it) => it.shift.ok);
  const maxShift = Math.max(0, ...lined.map((it) => Math.hypot(it.shift.dx, it.shift.dy) * pix));
  const notLined = shifts.length - lined.length;

  let found;
  let dist = 0;
  if (!seen.length) {
    found = `The star itself is too faint to be found in any of these images; the rings show where ${source} puts it.`;
  } else if (seen.length === 1) {
    found = `The star is found only in ${a.button}; it takes two images to see it move.`;
  } else {
    dist = Math.hypot(b.pos.east - a.pos.east, b.pos.north - a.pos.north);
    const years = b.year - a.year;
    const listed = row.pm != null ? row.pm * years : null;
    if (dist < fwhm / 2) {
      found = `Found in ${seen.length} of ${points.length} images, always on the same spot (within ${shiftText(dist)}): it has not visibly moved between ${a.date.slice(0, 4)} and ${b.date.slice(0, 4)}.`
        + (listed != null ? ` ${source} has it moving ${shiftText(listed)} in that time, too little for these images.` : '');
    } else {
      const pa = positionAngle(b.pos.east - a.pos.east, b.pos.north - a.pos.north);
      found = `Found in ${seen.length} of ${points.length} images. Between ${a.date.slice(0, 4)} and ${b.date.slice(0, 4)} it moved ${shiftText(dist)} toward the ${compass(pa)} in the images: ${(dist / years).toFixed(2)}″ a year`
        + (row.pm != null ? `, where ${source} gives ${speed(row.pm)}″.` : '.');
    }
  }
  const align = !shifts.length ? ''
    : ` The images were lined up on the fixed stars around it${lined.length ? ` (corrections up to ${shiftText(maxShift)})` : ''}`
      + (notLined ? `; ${notLined === shifts.length ? 'none' : notLined} had too few stars in common and ${notLined === 1 ? 'is' : 'are'} shown as given.` : '.');
  const moved = seen.length > 1 && dist > fwhm;
  // Where the close-ups are centred: the mean found position, else the catalogue's latest.
  const centre = seen.length
    ? { east: seen.reduce((t, p) => t + p.pos.east, 0) / seen.length, north: seen.reduce((t, p) => t + p.pos.north, 0) / seen.length }
    : points[points.length - 1].pred;
  return { points, seen, dist, fwhm, moved, centre, span: yearSpan, caption: found + align };
}

/** The path over the trace picture: the catalogue's (faint) and the one found in the images. */
function TraceMarks({ story, side }) {
  const h = side / 2;
  const { points, seen, fwhm, moved } = story;
  const xy = (p) => `${-p.east},${-p.north}`;
  const ring = Math.max(1.5 * fwhm, side * 0.03);
  const font = side * 0.034;
  const a = seen[0], b = seen[seen.length - 1];
  if (!moved) {
    // Nothing to trace: one ring where it was found (or is expected), and what that means.
    const at = seen.length ? story.centre : points[points.length - 1].pred;
    return (
      <svg className="ev-marks" viewBox={`${-h} ${-h} ${side} ${side}`} aria-hidden="true">
        <circle className={`tl-pt tl-still ${seen.length ? '' : 'tl-pt-unseen'}`} r={ring} cx={-at.east} cy={-at.north} />
        <text className="tl-label" fontSize={font} x={-at.east + ring * 1.25} y={-at.north - ring * 0.5}>
          {seen.length > 1 ? 'stays put' : seen.length ? 'found once' : 'too faint'}
        </text>
      </svg>
    );
  }
  return (
    <svg className="ev-marks" viewBox={`${-h} ${-h} ${side} ${side}`} aria-hidden="true">
      <defs>
        <marker id="tl-trace-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M0 0L10 5L0 10z" className="tl-head" />
        </marker>
      </defs>
      {points.length > 1 && <polyline className="tl-cat" points={points.map((p) => xy(p.pred)).join(' ')} />}
      <line className="tl-found" markerEnd="url(#tl-trace-head)" {...arrowEnds(a.pos, b.pos, ring)} />
      {points.map((p) => (
        <circle
          key={p.key} className={`tl-pt ${p.seen ? '' : 'tl-pt-unseen'}`} r={ring}
          cx={-p.pos.east} cy={-p.pos.north} style={{ stroke: p.css }}
        />
      ))}
      {[a, b].map((p, i) => (
          <text
            key={p.key} className="tl-label" fontSize={font} x={-p.pos.east + ring * 1.2} y={-p.pos.north + (i ? -ring : ring + font)}
            style={{ fill: p.css }}
          >
            {p.date.slice(0, 4)}
          </text>
      ))}
    </svg>
  );
}

/** Side of each close-up, arcsec: about eight SPHEREx pixels, so a one-pixel move would show. */
const CLOSE_ARCSEC = 50;

/** One image's close-up: the lined-up image around `centre`, with the star ringed if found. */
function CloseUp({ item, point, centre, side }) {
  const ref = useRef(null);
  const pix = side / GRID;
  const n = Math.max(8, Math.round(CLOSE_ARCSEC / pix));
  const c = skyToGrid(centre.east, centre.north, GRID, side);
  const x0 = Math.round(c.col - n / 2), y0 = Math.round(c.row - n / 2);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const toned = tone(item.z);
    const crop = new Float32Array(n * n).fill(NaN);
    for (let y = 0; y < n; y++) {
      for (let x = 0; x < n; x++) {
        const gx = x0 + x, gy = y0 + y;
        if (gx >= 0 && gy >= 0 && gx < GRID && gy < GRID) crop[y * n + x] = toned[gy * GRID + gx];
      }
    }
    canvas.getContext('2d').putImageData(new ImageData(greyRgba(crop), n, n), 0, 0);
  }, [item, n, x0, y0]);
  // Marks in arcsec from the close-up's centre (east left, north up).
  const h = (n * pix) / 2;
  // The crop's own centre against `centre` (they differ by rounding to whole pixels).
  const cx = (x0 + (n - 1) / 2 - c.col) * pix;
  const cy = (y0 + (n - 1) / 2 - c.row) * pix;
  const at = point.seen ? point.pos : point.pred;
  const ring = FWHM_ARCSEC.spherex;
  return (
    <li className={point.seen ? '' : 'tl-unseen'}>
      <div className="tl-close-img">
        <canvas ref={ref} width={n} height={n} />
        <svg className="ev-marks" viewBox={`${-h + cx} ${-h + cy} ${2 * h} ${2 * h}`} aria-hidden="true">
          <g className="ev-ref">
            <line x1={-h} y1={0} x2={-ring * 1.3} y2={0} /><line x1={ring * 1.3} y1={0} x2={h} y2={0} />
            <line x1={0} y1={-h} x2={0} y2={-ring * 1.3} /><line x1={0} y1={ring * 1.3} x2={0} y2={h} />
          </g>
          <circle
            className={`tl-pt ${point.seen ? '' : 'tl-pt-unseen'}`} r={ring}
            cx={-(at.east - centre.east)} cy={-(at.north - centre.north)} style={{ stroke: point.css }}
          />
        </svg>
      </div>
      <span className="tl-close-name"><i className="tl-swatch" style={{ background: point.css }} />{point.button}</span>
      <span className="tl-close-what">{point.seen ? 'found' : 'not found'}</span>
    </li>
  );
}

/**
 * For a star that does not visibly move: the same small square of sky from every image, lined
 * up, with a crosshair on one fixed point. A star staying on the crosshair in every image is
 * what "not moving" looks like; one that drifted off it would show.
 */
function CloseUps({ trace, story, side }) {
  const { points, seen } = story;
  let line;
  if (seen.length > 1) {
    line = `The crosshair marks one fixed point on the sky, the same in every image. The star sits on it in all ${seen.length} images where it is found, so it has not moved: a shift of one SPHEREx pixel (6″) would put it a ring's width off.`;
  } else if (seen.length === 1) {
    line = 'The crosshair marks one fixed point on the sky. The star shows up in only one image, so these images cannot tell whether it moves.';
  } else {
    line = 'The crosshair marks where the catalogue puts the star. There is only sky noise there: the star is too faint for these telescopes, so they cannot show whether it moves.';
  }
  return (
    <section className="tl-close" aria-label="Close-up of the star in each image">
      <h4 className="tl-close-title">Close-up: the same {CLOSE_ARCSEC}″ of sky in every image</h4>
      <p className="tl-close-line">{line}</p>
      <ul className="tl-close-list">
        {trace.items.map((it, i) => <CloseUp key={it.key} item={it} point={points[i]} centre={story.centre} side={side} />)}
      </ul>
    </section>
  );
}
