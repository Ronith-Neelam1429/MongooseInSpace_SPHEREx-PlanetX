import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import SkyMap from './components/SkyMap.jsx';
import InfoPanel from './components/InfoPanel.jsx';
import Search from './components/Search.jsx';
import { decimalYear, formatDec, formatFov, formatRa, parseCoords, propagate } from './lib/coords.js';
import { API_BASE, fetchVisits, queryGaia, querySimbad, resolveName, runMotion } from './lib/services.js';
import { StarField, arrowYears } from './lib/starfield.js';
import { Camera, prefersReducedMotion } from './lib/camera.js';
import { DEFAULT_FRAME, SPHEREX_FRAMES, SURVEYS, surveyById } from './lib/surveys.js';
import { TARGET_FOV, findTarget } from './lib/targets.js';
import { readViewFromUrl, writeViewToUrl } from './lib/url.js';
import './App.css';

const GAIA_LIMIT = 1500;
const urlView = readViewFromUrl();
// Opens on a globe centred near the Galactic Centre, so the Milky Way crosses the sphere.
const INITIAL = { ra: 266.4, dec: -18, fov: 180, ...urlView };

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
/** Each click zooms in until ~0.3°, then only recenters. */
const nextFov = (fov) => (fov > 0.5 ? Math.max(fov / 5, 0.3) : fov);
const radiusFor = (fov) => clamp(fov * 0.3, 0.5 / 60, 0.25);
/** Field side for the motion engine: its frames reach 1.5x this, covering the selection circle. */
const motionSize = (radius) => clamp((radius * 4) / 3, 0.03, 0.2);
/** Engine objects worth marking on the sky: everything but plain static stars. */
const notable = (result) => result.objects.filter((o) => o.kind !== 'static' || o.is_variable);

/** Cursor / centre readout, updated imperatively so mouse moves don't re-render the app. */
const Readout = forwardRef(function Readout(_, ref) {
  const pos = useRef(null);
  const fov = useRef(null);
  const label = useRef(null);
  useImperativeHandle(ref, () => ({
    update({ ra, dec, fov: f, cursor }) {
      if (pos.current && Number.isFinite(ra)) pos.current.textContent = `${formatRa(ra)}  ${formatDec(dec)}`;
      if (label.current) label.current.textContent = cursor ? 'Cursor' : 'Centre';
      if (fov.current && f != null) fov.current.textContent = formatFov(f);
    },
  }), []);
  return (
    <div className="readout" aria-hidden="true">
      <span ref={label} className="dim">Centre</span>
      <span ref={pos} className="tabular" />
      <span className="dim">FoV</span>
      <span ref={fov} className="tabular" />
    </div>
  );
});

function Toggle({ on, onClick, children }) {
  return <button type="button" className={`opt ${on ? 'on' : ''}`} aria-pressed={on} onClick={onClick}>{children}</button>;
}

export default function App() {
  const map = useRef(null); // { aladin, el }
  const field = useRef(null);
  const camera = useRef(null);
  const canvasRef = useRef(null);
  const searchRef = useRef(null);
  const readout = useRef(null);
  const view = useRef(INITIAL);
  const aborts = useRef({});

  const [ready, setReady] = useState(false);
  const [tilesLoading, setTilesLoading] = useState(false);
  const [survey, setSurvey] = useState('spherex');
  const [frame, setFrame] = useState(DEFAULT_FRAME);
  const [skyFrame, setSkyFrame] = useState(DEFAULT_FRAME); // debounced: dragging shouldn't fetch every frame
  const [surveyError, setSurveyError] = useState(null);
  const [show, setShow] = useState({ gaia: true, simbad: true, engine: true, grid: false });
  const [layersOpen, setLayersOpen] = useState(false);
  const [sel, setSel] = useState(null);
  const [simbad, setSimbad] = useState({ status: 'idle' });
  const [gaia, setGaia] = useState({ status: 'idle' });
  const [visits, setVisits] = useState({ status: 'idle' });
  const [motion, setMotion] = useState({ status: 'idle' });
  const [focus, setFocus] = useState(null);
  const [playing, setPlaying] = useState(() => !prefersReducedMotion());
  const [moversOnly, setMoversOnly] = useState(false);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState({ status: 'idle' });
  const [copied, setCopied] = useState(false);

  const load = useCallback((kind, s) => {
    aborts.current[kind]?.abort();
    const ctrl = new AbortController();
    aborts.current[kind] = ctrl;
    const f = field.current;
    const fail = (set) => (err) => {
      if (!ctrl.signal.aborted) set({ status: 'error', error: err?.message || String(err) });
    };
    if (kind === 'simbad') {
      setSimbad({ status: 'loading' });
      f?.setSimbad([]);
      querySimbad(s.ra, s.dec, s.radius, { signal: ctrl.signal }).then((objects) => {
        if (ctrl.signal.aborted) return;
        setSimbad({ status: 'ok', objects });
        f?.setSimbad(objects);
      }, fail(setSimbad));
    } else if (kind === 'gaia') {
      setGaia({ status: 'loading' });
      f?.setGaia([], null);
      queryGaia(s.ra, s.dec, s.radius, { limit: GAIA_LIMIT, signal: ctrl.signal }).then((stars) => {
        if (ctrl.signal.aborted) return;
        const years = arrowYears(stars, s.radius);
        setGaia({ status: 'ok', stars, years, limit: GAIA_LIMIT });
        f?.setGaia(stars, years);
      }, fail(setGaia));
    } else if (kind === 'visits') {
      setVisits({ status: 'loading' });
      fetchVisits(s.ra, s.dec, { signal: ctrl.signal }).then((v) => {
        if (!ctrl.signal.aborted) setVisits(v === null ? { status: 'none' } : { status: 'ok', visits: v });
      }, fail(setVisits));
    } else if (kind === 'motion') {
      setMotion({ status: 'loading' });
      f?.setMotion([]);
      runMotion(s.ra, s.dec, motionSize(s.radius), {
        signal: ctrl.signal,
        onProgress: (job) => { if (!ctrl.signal.aborted) setMotion({ status: 'loading', job }); },
      }).then((result) => {
        if (ctrl.signal.aborted) return;
        setMotion({ status: 'ok', result });
        f?.setMotion(notable(result));
      }, fail(setMotion));
    }
  }, []);

  const select = useCallback((ra, dec, { fov, label } = {}) => {
    const m = map.current;
    if (!m) return;
    const targetFov = fov ?? nextFov(m.aladin.getFov()[0]);
    camera.current.flyTo({ ra, dec, fov: targetFov });
    const s = { ra, dec, radius: radiusFor(targetFov), label };
    setSel(s);
    setFocus(null);
    field.current?.setFocus(null);
    field.current?.setRegion(s);
    // The engine is costly (it downloads tens of frames), so it runs only on request.
    aborts.current.motion?.abort();
    field.current?.setMotion([]);
    setMotion({ status: API_BASE ? 'idle' : 'none' });
    load('simbad', s);
    load('gaia', s);
    load('visits', s);
  }, [load]);

  const closePanel = useCallback(() => {
    Object.values(aborts.current).forEach((c) => c.abort());
    const f = field.current;
    if (f) { f.setRegion(null); f.setGaia([], null); f.setSimbad([]); f.setMotion([]); f.setFocus(null); }
    setSel(null);
    setFocus(null);
  }, []);

  const goTarget = useCallback((t) => {
    const p = propagate(t.ra, t.dec, t.pmra, t.pmdec, decimalYear() - 2000);
    setQuery(t.label);
    setSearch({ status: 'idle' });
    select(p.ra, p.dec, { fov: TARGET_FOV, label: t.label });
  }, [select]);

  const runSearch = useCallback(async (text) => {
    const q = text.trim();
    if (!q) return;
    const known = findTarget(q);
    if (known) { goTarget(known); return; }
    const c = parseCoords(q);
    if (c) {
      setSearch({ status: 'idle' });
      select(c.ra, c.dec, { fov: TARGET_FOV });
      return;
    }
    setSearch({ status: 'loading' });
    try {
      const r = await resolveName(q);
      if (!r) { setSearch({ status: 'error', error: `“${q}” not found` }); return; }
      setSearch({ status: 'idle' });
      select(r.ra, r.dec, { fov: TARGET_FOV, label: r.name });
    } catch (err) {
      setSearch({ status: 'error', error: `Name lookup failed (${err.message})` });
    }
  }, [goTarget, select]);

  const onReady = useCallback(({ aladin, el }) => {
    map.current = { aladin, el };
    camera.current = new Camera(aladin, el);
    field.current = new StarField(canvasRef.current, aladin, {
      onHover: (hit) => el.classList.toggle('pointing', !!hit),
    });
    setReady(true);
    if (urlView.name) {
      setQuery(urlView.name);
      runSearch(urlView.name);
    } else if (urlView.ra != null && (urlView.fov ?? 360) <= 5) {
      select(urlView.ra, urlView.dec, { fov: urlView.fov });
    } else if (INITIAL.fov >= 60) {
      camera.current.spin();
    }
  }, [runSearch, select]);

  useEffect(() => () => { field.current?.destroy(); camera.current?.stop(); }, []);
  useEffect(() => { field.current?.setMoversOnly(moversOnly); }, [moversOnly, ready]);
  useEffect(() => { field.current?.setVisible({ gaia: show.gaia, simbad: show.simbad, engine: show.engine }); }, [show, ready]);
  useEffect(() => { field.current?.setPlaying(playing); }, [playing, ready]);

  useEffect(() => {
    const t = setTimeout(() => setSkyFrame(frame), 250);
    return () => clearTimeout(t);
  }, [frame]);

  useEffect(() => {
    if (!copied) return undefined;
    const t = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(t);
  }, [copied]);

  useEffect(() => {
    const onKey = (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
      if (e.key === '/' && !typing) { e.preventDefault(); searchRef.current?.focus(); }
      else if (e.key === 'Escape' && !typing) closePanel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [closePanel]);

  const onViewChange = useCallback((v) => {
    view.current = v;
    writeViewToUrl(v);
    readout.current?.update(v);
  }, []);

  const onPointer = useCallback((p) => {
    field.current?.setPointer(p ? { x: p.x, y: p.y } : null);
    readout.current?.update(p ? { ra: ((p.ra % 360) + 360) % 360, dec: p.dec, cursor: true } : view.current);
  }, []);

  const onSkyClick = useCallback(({ ra, dec, x, y }) => {
    const hit = sel ? field.current?.hitTest(x, y) : null;
    if (hit) {
      setFocus(hit);
      field.current.setFocus(hit.obj);
      return;
    }
    select(ra, dec);
  }, [sel, select]);

  const onFocus = useCallback((kind, obj) => {
    setFocus({ kind, obj });
    field.current?.setFocus(obj);
    const c = camera.current;
    if (c) c.flyTo({ ra: obj.ra, dec: obj.dec, fov: c.view().fov });
  }, []);

  const clearFocus = useCallback(() => { setFocus(null); field.current?.setFocus(null); }, []);
  const getYears = useCallback(() => field.current?.currentYears() ?? 0, []);

  const copyLink = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
    } catch {
      window.prompt('Copy this link', window.location.href);
    }
  }, []);

  const pickSurvey = (id) => { setSurveyError(null); setSurvey(id); };
  const frameIdx = Math.max(0, SPHEREX_FRAMES.findIndex((x) => x.frame === frame));
  const fr = SPHEREX_FRAMES[frameIdx];
  const isCube = surveyById(survey).cube;

  return (
    <div className={`app ${sel ? 'with-panel' : ''}`}>
      <main className={`stage ${sel && show.gaia ? 'dimmed' : ''}`}>
        <SkyMap
          initial={INITIAL}
          survey={survey}
          frame={skyFrame}
          grid={show.grid}
          onReady={onReady}
          onViewChange={onViewChange}
          onPointer={onPointer}
          onSkyClick={onSkyClick}
          onSurveyError={setSurveyError}
          onLoading={setTilesLoading}
        />
        {tilesLoading && <div className="loadbar" aria-hidden="true" />}
        <canvas ref={canvasRef} className="field" aria-hidden="true" />

        <header className="bar">
          <h1 className="mark">PlanetX</h1>
          <Search
            ref={searchRef}
            value={query}
            onChange={(v) => { setQuery(v); if (search.status === 'error') setSearch({ status: 'idle' }); }}
            onSubmit={runSearch}
            onPick={goTarget}
            busy={search.status === 'loading'}
            error={search.status === 'error' ? search.error : null}
            disabled={!ready}
          />
        </header>

        <nav className={`layers ${layersOpen ? 'open' : ''}`} aria-label="Layers">
          <button type="button" className="opt layers-toggle" aria-expanded={layersOpen} onClick={() => setLayersOpen(!layersOpen)}>Layers</button>
          <div className="layer-groups">
            <div className="group" role="group" aria-label="Sky survey">
              {SURVEYS.map((s) => <Toggle key={s.id} on={survey === s.id} onClick={() => pickSurvey(s.id)}>{s.label}</Toggle>)}
            </div>
            <div className="group" role="group" aria-label="Overlays">
              <Toggle on={show.gaia} onClick={() => setShow({ ...show, gaia: !show.gaia })}>Gaia</Toggle>
              <Toggle on={show.simbad} onClick={() => setShow({ ...show, simbad: !show.simbad })}>SIMBAD</Toggle>
              <Toggle on={show.engine} onClick={() => setShow({ ...show, engine: !show.engine })}>Engine</Toggle>
              <Toggle on={show.grid} onClick={() => setShow({ ...show, grid: !show.grid })}>Grid</Toggle>
            </div>
          </div>
        </nav>

        <footer className="hud">
          <Readout ref={readout} />
          {isCube && (
            <label className="lambda">
              <span className="dim">λ</span>
              <input
                type="range"
                min="0"
                max={SPHEREX_FRAMES.length - 1}
                value={frameIdx}
                onChange={(e) => setFrame(SPHEREX_FRAMES[+e.target.value].frame)}
                aria-label="SPHEREx wavelength"
                aria-valuetext={`${fr.um.toFixed(2)} micrometres, band ${fr.band}`}
              />
              <span className="tabular">{fr.um.toFixed(2)} µm</span>
              <span className="dim">B{fr.band}</span>
            </label>
          )}
          {surveyError && <span className="hud-error" role="alert">{surveyError}</span>}
          <div className="zoom">
            <button type="button" className="iconbtn" aria-label="Zoom in" disabled={!ready} onClick={() => map.current?.aladin.increaseZoom()}>
              <svg width="11" height="11" viewBox="0 0 11 11" aria-hidden="true"><path d="M5.5 0v11M0 5.5h11" stroke="currentColor" strokeWidth="1.25" /></svg>
            </button>
            <button type="button" className="iconbtn" aria-label="Zoom out" disabled={!ready} onClick={() => map.current?.aladin.decreaseZoom()}>
              <svg width="11" height="11" viewBox="0 0 11 11" aria-hidden="true"><path d="M0 5.5h11" stroke="currentColor" strokeWidth="1.25" /></svg>
            </button>
          </div>
        </footer>

        {!ready && <div className="boot" aria-live="polite">Loading sky</div>}
      </main>

      {sel && (
        <InfoPanel
          sel={sel}
          simbad={simbad}
          gaia={gaia}
          visits={visits}
          motion={motion}
          focus={focus}
          onFocus={onFocus}
          onClearFocus={clearFocus}
          onClose={closePanel}
          onRetry={(kind) => load(kind, sel)}
          onCopyLink={copyLink}
          copied={copied}
          playing={playing}
          onTogglePlay={() => setPlaying(!playing)}
          moversOnly={moversOnly}
          onToggleMovers={() => setMoversOnly(!moversOnly)}
          getYears={getYears}
        />
      )}
    </div>
  );
}
