import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from 'react';
import SkyMap from './components/SkyMap.jsx';
import InfoPanel from './components/InfoPanel.jsx';
import CandidatesPanel from './components/CandidatesPanel.jsx';
import Search from './components/Search.jsx';
import ClassFilter from './components/ClassFilter.jsx';
import { SHOW_ALL, SHOW_MOVING, countClasses, engineClass, isMovingOnly, knownClass } from './lib/classes.js';
import { decimalYear, formatDec, formatFov, formatRa, parseCoords, propagate, separation } from './lib/coords.js';
import { API_BASE, fetchSurvey, fetchVisits, queryGaia, querySimbad, resolveName, runMotion } from './lib/services.js';
import { StarField, arrowYears } from './lib/starfield.js';
import { Camera } from './lib/camera.js';
import { FAST_ARCSEC_YR, identify, knownObjects, mapTarget, notableKnown } from './lib/names.js';
import { shortClaim, shortName, speed, verdict } from './lib/plain.js';
import { DEFAULT_FRAME, SPHEREX_FRAMES, SURVEYS, surveyById } from './lib/surveys.js';
import { TARGET_FOV, findTarget } from './lib/targets.js';
import { readViewFromUrl, writeViewToUrl } from './lib/url.js';
import { otherObjects } from './lib/evidence.js';
import './App.css';

const GAIA_LIMIT = 3000;
const urlView = readViewFromUrl();
// Opens on a globe centred near the Galactic Centre, so the Milky Way crosses the sphere.
const INITIAL = { ra: 266.4, dec: -18, fov: 180, ...urlView };

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
/** Each click zooms in until ~0.3°, then only recenters. */
const nextFov = (fov) => (fov > 0.5 ? Math.max(fov / 5, 0.3) : fov);
const radiusFor = (fov) => clamp(fov * 0.3, 0.5 / 60, 0.25);
/** The view that frames a patch of that radius (the inverse of radiusFor). */
const fovFor = (radius) => radius / 0.3;
/** Field side for the motion engine: its frames reach 1.5x this, covering the selection circle. */
const motionSize = (radius) => clamp((radius * 4) / 3, 0.03, 0.2);
/**
 * Catalogues are loaded out to the corners of the square the engine analyses, so every engine
 * object can be matched to a name; only what lies inside the circle is drawn and listed.
 */
const catalogRadius = (radius) => Math.max(radius, motionSize(radius) * 0.75 * Math.SQRT2);
/** Engine objects worth showing: everything but plain static stars. */
const notable = (result) => result.objects.filter((o) => o.kind !== 'static' || o.is_variable);

/**
 * The widest view that still shows a selected object well. A measured mover needs a close view
 * for its track (a few arcseconds long) to be visible; anything else is fine at any zoom.
 */
function objectFov(sel) {
  const track = sel.kind === 'engine' && sel.obj.kind !== 'static' ? sel.obj.track : [];
  if (track.length < 2) return Infinity;
  const first = track[0], last = track[track.length - 1];
  return clamp(separation(first.ra, first.dec, last.ra, last.dec) / 0.1, 0.05, 0.1);
}

/** Short name of an engine object for the map. */
const resultLabel = ({ o, who }) => (who.name ? shortName(who.name) : who.checked ? 'Uncatalogued' : verdict(o));

function surveyLine(crawl) {
  if (crawl.status === 'loading') return 'Loading the survey';
  if (crawl.status === 'none') return 'The survey needs the PlanetX server, which is not connected.';
  if (crawl.status === 'error') return `Survey unavailable: ${crawl.error}.`;
  const s = crawl.info.status;
  if (!s.total) return 'Our automatic survey has not started yet.';
  const where = s.current_radius_deg != null ? `, now ${s.current_radius_deg.toFixed(1)}° from the north ecliptic pole` : '';
  return `Our automatic survey has checked ${s.counts.done.toLocaleString('en-US')} of ${s.total.toLocaleString('en-US')} patches${where}.`;
}

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
      <span className="dim">View</span>
      <span><span ref={fov} className="tabular" /> wide</span>
    </div>
  );
});

function Toggle({ on, onClick, title, children }) {
  return <button type="button" className={`opt ${on ? 'on' : ''}`} aria-pressed={on} title={title} onClick={onClick}>{children}</button>;
}

export default function App() {
  const map = useRef(null); // { aladin, el }
  const field = useRef(null);
  const camera = useRef(null);
  const canvasRef = useRef(null);
  const searchRef = useRef(null);
  const readout = useRef(null);
  const view = useRef(INITIAL);
  const aborts = useRef({}); // in-flight requests of the open patch, by kind
  const crawlAbort = useRef(null);
  const latest = useRef({}); // current state for the map's long-lived callbacks

  const [ready, setReady] = useState(false);
  const [tilesLoading, setTilesLoading] = useState(false);
  const [survey, setSurvey] = useState('spherex');
  const [frame, setFrame] = useState(DEFAULT_FRAME);
  const [skyFrame, setSkyFrame] = useState(DEFAULT_FRAME); // debounced: dragging shouldn't fetch every frame
  const [surveyError, setSurveyError] = useState(null);
  const [show, setShow] = useState({ gaia: true, simbad: true, engine: true, grid: false, crawl: false });
  const [layersOpen, setLayersOpen] = useState(false);
  const [sel, setSel] = useState(null); // the open patch of sky
  const [simbad, setSimbad] = useState({ status: 'idle' });
  const [gaia, setGaia] = useState({ status: 'idle' });
  const [visits, setVisits] = useState({ status: 'idle' });
  const [motion, setMotion] = useState({ status: 'idle' });
  const [crawl, setCrawl] = useState({ status: 'idle' }); // the offline survey's progress and coverage
  const [focus, setFocus] = useState(null); // the selected object: { kind: 'engine' | 'known', obj }
  const [hover, setHover] = useState(null); // the map marker under the pointer: { kind, obj }
  const [pointed, setPointed] = useState(null); // the list row under the pointer, as a selection
  const [resume, setResume] = useState(null); // the object just left: its list row takes the keyboard focus back
  const [listOpen, setListOpen] = useState(false); // the survey's candidates list
  const [playing, setPlaying] = useState(false);
  const [filter, setFilter] = useState(SHOW_ALL); // which kinds of object are shown (lib/classes.js)
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState({ status: 'idle' });
  const [copied, setCopied] = useState(false);

  // SIMBAD and Gaia merged into one list of known objects, and each engine result with a name.
  const known = useMemo(
    () => knownObjects(simbad.status === 'ok' ? simbad.objects : [], gaia.status === 'ok' ? gaia.stars : []),
    [simbad, gaia],
  );
  const results = useMemo(() => {
    if (motion.status !== 'ok') return [];
    const lists = { simbad: simbad.status === 'ok' ? simbad.all : null, gaia: gaia.status === 'ok' ? gaia.all : null };
    return notable(motion.result).map((o) => ({ o, who: identify(o, lists) }));
  }, [motion, simbad, gaia]);
  // The run whose other (static) objects are listed and pickable on the map; a new run closes the list.
  const [othersFor, setOthersFor] = useState(null);
  const othersOpen = motion.status === 'ok' && othersFor === motion.result;
  const others = useMemo(() => {
    if (!othersOpen) return null;
    const lists = { simbad: simbad.status === 'ok' ? simbad.all : null, gaia: gaia.status === 'ok' ? gaia.all : null };
    return otherObjects(motion.result.objects, results.map((r) => r.o)).map((o) => ({ o, who: identify(o, lists) }));
  }, [othersOpen, motion, results, simbad, gaia]);
  // Objects that keep a name on the map: the engine's results, then the notable known ones.
  const labels = useMemo(() => {
    // Only results the filter shows carry a name; a hidden one leaves the label to its known row.
    const named = new Set(results.filter((r) => filter[engineClass(r.o)]).flatMap(({ who }) => who.matches.map((m) => m.name)));
    const out = results.map((r) => ({ ...mapTarget({ kind: 'engine', obj: r.o }), text: resultLabel(r), tone: 'measured' }));
    for (const row of notableKnown(known.rows)) {
      // An engine result already carries this object's name.
      if (named.has(row.name) || (row.gaia && named.has(`Gaia DR3 ${row.gaia.id}`))) continue;
      out.push({ ...mapTarget({ kind: 'known', obj: row }), text: shortName(row.name), tone: 'known' });
    }
    return out;
  }, [results, known, filter]);
  // How many of each kind the open field holds, for the filter.
  const counts = useMemo(() => {
    if (gaia.status !== 'ok' && simbad.status !== 'ok' && motion.status !== 'ok') return null;
    return countClasses(known.rows, gaia.status === 'ok' ? gaia.stars : [], results.map((r) => r.o));
  }, [known, gaia, simbad, motion, results]);
  const hovered = useMemo(() => {
    if (!hover) return null;
    return hover.kind === 'engine' ? hover : { kind: 'known', obj: known.of(hover.kind, hover.obj) };
  }, [hover, known]);

  useLayoutEffect(() => {
    latest.current = {
      sel,
      known,
      describe(kind, obj) {
        if (kind === 'engine') {
          const r = results.find((x) => x.o === obj) || others?.find((x) => x.o === obj);
          const what = shortClaim(obj) || verdict(obj);
          return r ? `${resultLabel(r)} · ${what}` : what;
        }
        const row = known.of(kind, obj);
        const pm = row.pm >= FAST_ARCSEC_YR ? ` · ${speed(row.pm)}″/yr` : '';
        return `${shortName(row.name)}${row.type ? ` · ${row.type}` : ''}${pm}`;
      },
    };
  });

  const load = useCallback((kind, s) => {
    aborts.current[kind]?.abort();
    const ctrl = new AbortController();
    aborts.current[kind] = ctrl;
    const fail = (set) => (err) => {
      if (!ctrl.signal.aborted) set({ status: 'error', error: err?.message || String(err) });
    };
    if (kind === 'simbad') {
      setSimbad({ status: 'loading' });
      querySimbad(s.ra, s.dec, catalogRadius(s.radius), { signal: ctrl.signal }).then((all) => {
        if (!ctrl.signal.aborted) setSimbad({ status: 'ok', all, objects: all.filter((o) => o.dist <= s.radius) });
      }, fail(setSimbad));
    } else if (kind === 'gaia') {
      setGaia({ status: 'loading' });
      queryGaia(s.ra, s.dec, catalogRadius(s.radius), { limit: GAIA_LIMIT, signal: ctrl.signal }).then((all) => {
        if (ctrl.signal.aborted) return;
        const stars = all.filter((g) => separation(s.ra, s.dec, g.ra, g.dec) <= s.radius);
        setGaia({ status: 'ok', all, stars, years: arrowYears(stars, s.radius), capped: all.length >= GAIA_LIMIT });
      }, fail(setGaia));
    } else if (kind === 'visits') {
      setVisits({ status: 'loading' });
      fetchVisits(s.ra, s.dec, { signal: ctrl.signal }).then((v) => {
        if (!ctrl.signal.aborted) setVisits(v === null ? { status: 'none' } : { status: 'ok', ...v });
      }, fail(setVisits));
    } else if (kind === 'motion') {
      setMotion({ status: 'loading' });
      runMotion(s.ra, s.dec, motionSize(s.radius), {
        signal: ctrl.signal,
        onProgress: (job) => { if (!ctrl.signal.aborted) setMotion({ status: 'loading', job }); },
      }).then((result) => {
        if (!ctrl.signal.aborted) setMotion({ status: 'ok', result });
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
    setPointed(null);
    setResume(null);
    setListOpen(false);
    // The class filter carries over: someone hunting movers keeps "Moving" from patch to patch.
    // The engine is costly (it downloads tens of frames), so it runs only on request.
    aborts.current.motion?.abort();
    setMotion({ status: API_BASE ? 'idle' : 'none' });
    load('simbad', s);
    load('gaia', s);
    load('visits', s);
  }, [load]);

  const closePanel = useCallback(() => {
    Object.values(aborts.current).forEach((c) => c.abort());
    setSel(null);
    setFocus(null);
    setPointed(null);
    setSimbad({ status: 'idle' });
    setGaia({ status: 'idle' });
    setMotion({ status: 'idle' });
    writeViewToUrl(view.current);
  }, []);

  /** Select one object: its card opens and the camera goes to it. */
  const pick = useCallback((s) => {
    setFocus(s);
    setPointed(null);
    const c = camera.current;
    const t = mapTarget(s);
    if (c) c.flyTo({ ra: t.ra, dec: t.dec, fov: Math.min(c.view().fov, objectFov(s)) });
  }, []);

  /** Leave the selected object and frame the whole patch again. */
  const backToPatch = useCallback(() => {
    setResume(focus);
    setFocus(null);
    const s = latest.current.sel;
    if (s) camera.current?.flyTo({ ra: s.ra, dec: s.dec, fov: fovFor(s.radius) });
  }, [focus]);

  const goTarget = useCallback((t) => {
    const p = propagate(t.ra, t.dec, t.pmra, t.pmdec, decimalYear() - 2000);
    setQuery(t.label);
    setSearch({ status: 'idle' });
    select(p.ra, p.dec, { fov: TARGET_FOV, label: t.label });
  }, [select]);

  const runSearch = useCallback(async (text) => {
    const q = text.trim();
    if (!q) return;
    const target = findTarget(q);
    if (target) { goTarget(target); return; }
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
      onHover: (hit) => { el.classList.toggle('pointing', !!hit); setHover(hit); },
      describe: (kind, obj) => latest.current.describe(kind, obj),
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

  // The canvas layer draws what the state says.
  useEffect(() => () => { field.current?.destroy(); camera.current?.stop(); }, []);
  useEffect(() => { field.current?.setRegion(sel); }, [sel, ready]);
  useEffect(() => { field.current?.setSimbad(simbad.status === 'ok' ? simbad.objects : []); }, [simbad, ready]);
  useEffect(() => {
    if (gaia.status === 'ok') field.current?.setGaia(gaia.stars, gaia.years);
    else field.current?.setGaia([], null);
  }, [gaia, ready]);
  // The ~hundreds of static objects are drawn (and pickable) only while their list is open.
  useEffect(() => { field.current?.setMotion([...results, ...(others || [])].map((r) => r.o)); }, [results, others, ready]);
  useEffect(() => { field.current?.setNotable(labels); }, [labels, ready]);
  useEffect(() => {
    const name = focus && (focus.kind === 'engine' ? labels.find((l) => l.obj === focus.obj)?.text : shortName(focus.obj.name));
    field.current?.setFocus(focus ? { ...mapTarget(focus), text: name } : null);
  }, [focus, labels, ready]);
  useEffect(() => {
    const t = pointed && mapTarget(pointed);
    field.current?.setHighlight(t ? { ...t, text: latest.current.describe(t.kind, t.obj) } : null);
  }, [pointed, ready]);
  // A catalogue marker's class comes from its known-object row, so a Gaia dot joined to a SIMBAD
  // galaxy or variable is drawn as one.
  useEffect(() => { field.current?.setClassifier((kind, obj) => knownClass(known.of(kind, obj))); }, [known, ready]);
  useEffect(() => { field.current?.setFilter(filter); }, [filter, ready]);
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

  // Esc steps back one level: the candidates list, then the selected object, then the patch.
  useEffect(() => {
    const onKey = (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
      if (e.key === '/' && !typing) { e.preventDefault(); searchRef.current?.focus(); }
      else if (e.key === 'Escape' && !typing) {
        if (listOpen) setListOpen(false);
        else if (focus) backToPatch();
        else closePanel();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [listOpen, focus, backToPatch, closePanel]);

  const onViewChange = useCallback((v) => {
    view.current = v;
    // With a patch open the link is the patch, so it reopens the same one however the view has moved since.
    const s = latest.current.sel;
    writeViewToUrl(s ? { ra: s.ra, dec: s.dec, fov: fovFor(s.radius) } : v);
    readout.current?.update(v);
  }, []);

  const onPointer = useCallback((p) => {
    field.current?.setPointer(p ? { x: p.x, y: p.y } : null);
    readout.current?.update(p ? { ra: ((p.ra % 360) + 360) % 360, dec: p.dec, cursor: true } : view.current);
  }, []);

  const onSkyClick = useCallback(({ ra, dec, x, y }) => {
    const { sel: s, known: k } = latest.current;
    const hit = s ? field.current?.hitTest(x, y) : null;
    if (hit) {
      pick(hit.kind === 'engine' ? hit : { kind: 'known', obj: k.of(hit.kind, hit.obj) });
      return;
    }
    // A click that misses inside the open patch does nothing: it must not reload the patch and drop its results.
    if (s && separation(ra, dec, s.ra, s.dec) <= s.radius) return;
    select(ra, dec);
  }, [pick, select]);

  const getYear = useCallback(() => field.current?.currentYear() ?? 2016, []);

  const copyLink = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
    } catch {
      window.prompt('Copy this link', window.location.href);
    }
  }, []);

  /** Show or hide the offline survey's finished patches; its progress is fetched each time it is shown. */
  const toggleCrawl = () => {
    crawlAbort.current?.abort();
    const on = !show.crawl;
    setShow({ ...show, crawl: on });
    if (!on) return;
    const ctrl = new AbortController();
    crawlAbort.current = ctrl;
    setCrawl({ status: 'loading' });
    fetchSurvey({ signal: ctrl.signal }).then((info) => {
      if (!ctrl.signal.aborted) setCrawl(info ? { status: 'ok', info } : { status: 'none' });
    }, (err) => {
      if (!ctrl.signal.aborted) setCrawl({ status: 'error', error: err?.message || String(err) });
    });
  };

  const goCandidate = useCallback((c) => {
    select(c.ra, c.dec, { fov: TARGET_FOV, label: c.name || `Candidate ${c.id}` });
  }, [select]);

  const pickSurvey = (id) => { setSurveyError(null); setSurvey(id); };
  const frameIdx = Math.max(0, SPHEREX_FRAMES.findIndex((x) => x.frame === frame));
  const fr = SPHEREX_FRAMES[frameIdx];
  const isCube = surveyById(survey).cube;
  const panel = listOpen ? 'list' : sel ? 'patch' : null;
  // Every object's card opens with its telescope pictures (they need the backend), so it gets half the window.
  const wide = panel === 'patch' && Boolean(focus) && Boolean(API_BASE);

  return (
    <div className={`app ${panel ? 'with-panel' : ''} ${wide ? 'wide' : ''}`}>
      <main className={`stage ${sel && show.gaia ? 'dimmed' : ''}`}>
        <SkyMap
          initial={INITIAL}
          survey={survey}
          frame={skyFrame}
          grid={show.grid}
          coverage={show.crawl && crawl.status === 'ok' ? crawl.info.coverage : null}
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
          <button type="button" className={`opt ${listOpen ? 'on' : ''}`} aria-pressed={listOpen} disabled={!ready} onClick={() => setListOpen(!listOpen)}>
            Candidates
          </button>
        </header>

        <nav className={`layers ${layersOpen ? 'open' : ''}`} aria-label="Layers">
          <button type="button" className="opt layers-toggle" aria-expanded={layersOpen} onClick={() => setLayersOpen(!layersOpen)}>Layers</button>
          <div className="layer-groups">
            <div className="group" role="group" aria-label="Sky image">
              {SURVEYS.map((s) => <Toggle key={s.id} on={survey === s.id} onClick={() => pickSurvey(s.id)}>{s.label}</Toggle>)}
            </div>
            <div className="group" role="group" aria-label="Shown on the map">
              <Toggle on={show.gaia} title="Stars from the Gaia catalogue" onClick={() => setShow({ ...show, gaia: !show.gaia })}>Stars</Toggle>
              <Toggle on={show.simbad} title="Named objects from the SIMBAD catalogue" onClick={() => setShow({ ...show, simbad: !show.simbad })}>Named</Toggle>
              <Toggle on={show.engine} title="What PlanetX measured in SPHEREx images" onClick={() => setShow({ ...show, engine: !show.engine })}>Results</Toggle>
              <Toggle on={show.crawl} title="Patches our automatic survey has already checked" onClick={toggleCrawl}>Survey</Toggle>
              <Toggle on={show.grid} title="Coordinate grid" onClick={() => setShow({ ...show, grid: !show.grid })}>Grid</Toggle>
            </div>
          </div>
        </nav>

        {sel && <ClassFilter filter={filter} counts={counts} onChange={setFilter} />}

        <footer className="hud">
          <Readout ref={readout} />
          {isCube && (
            <label className="lambda">
              <span className="dim">Wavelength</span>
              <input
                type="range"
                min="0"
                max={SPHEREX_FRAMES.length - 1}
                value={frameIdx}
                onChange={(e) => setFrame(SPHEREX_FRAMES[+e.target.value].frame)}
                aria-label="SPHEREx wavelength"
                aria-valuetext={`${fr.um.toFixed(2)} micrometres`}
              />
              <span className="tabular">{fr.um.toFixed(2)} µm</span>
            </label>
          )}
          {surveyError && <span className="hud-note" role="alert">{surveyError}</span>}
          {show.crawl && <span className="hud-note" role="status">{surveyLine(crawl)}</span>}
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

      {panel === 'list' && <CandidatesPanel onGo={goCandidate} onClose={() => setListOpen(false)} />}
      {panel === 'patch' && (
        <InfoPanel
          sel={sel}
          simbad={simbad}
          gaia={gaia}
          visits={visits}
          motion={motion}
          known={known}
          results={results}
          others={others}
          onToggleOthers={() => setOthersFor(othersOpen ? null : motion.result)}
          focus={focus}
          hover={hovered}
          resume={resume}
          onPick={pick}
          onPoint={setPointed}
          onBack={backToPatch}
          onClose={closePanel}
          onRetry={(kind) => load(kind, sel)}
          onCopyLink={copyLink}
          copied={copied}
          playing={playing}
          onTogglePlay={() => setPlaying(!playing)}
          filter={filter}
          moversOnly={isMovingOnly(filter)}
          onToggleMovers={() => setFilter(isMovingOnly(filter) ? SHOW_ALL : SHOW_MOVING)}
          getYear={getYear}
        />
      )}
    </div>
  );
}
