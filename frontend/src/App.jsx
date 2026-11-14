import { useCallback, useEffect, useRef, useState } from 'react';
import SkyMap from './components/SkyMap.jsx';
import { SURVEYS } from './lib/surveys.js';
import InfoPanel from './components/InfoPanel.jsx';
import { decimalYear, formatDec, formatRa, parseCoords, propagate } from './lib/coords.js';
import { fetchVisits, queryGaia, querySimbad, resolveName } from './lib/services.js';
import { TARGETS, TARGET_FOV, findTarget } from './lib/targets.js';
import { readViewFromUrl, writeViewToUrl } from './lib/url.js';
import { arrowYears, createOverlays } from './lib/overlays.js';
import './App.css';

const GAIA_LIMIT = 1500;
const DEFAULT_VIEW = { ra: 270, dec: 0, fov: 360 };
const urlView = readViewFromUrl();
const initialView = { ...DEFAULT_VIEW, ...urlView };

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
/** Zoom in progressively on each click until ~0.3°, then just recenter. */
const nextFov = (fov) => (fov > 0.5 ? Math.max(fov / 5, 0.3) : fov);
const radiusFor = (fov) => clamp(fov * 0.3, 0.5 / 60, 0.25);

export default function App() {
  const map = useRef(null); // { aladin, A, overlays }
  const aborts = useRef({});
  const [ready, setReady] = useState(false);
  const [survey, setSurvey] = useState(SURVEYS[0].id);
  const [layers, setLayers] = useState({ simbad: true, gaia: true });
  const [sel, setSel] = useState(null);
  const [simbad, setSimbad] = useState({ status: 'idle' });
  const [gaia, setGaia] = useState({ status: 'idle' });
  const [visits, setVisits] = useState({ status: 'idle' });
  const [focusId, setFocusId] = useState(null);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState({ status: 'idle' });
  const [copied, setCopied] = useState(false);

  const load = useCallback((kind, s) => {
    aborts.current[kind]?.abort();
    const ctrl = new AbortController();
    aborts.current[kind] = ctrl;
    const ov = map.current?.overlays;
    const fail = (set) => (err) => {
      if (ctrl.signal.aborted) return;
      set({ status: 'error', error: err?.message || String(err) });
    };
    if (kind === 'simbad') {
      setSimbad({ status: 'loading' });
      ov?.setSimbad([]);
      querySimbad(s.ra, s.dec, s.radius, { signal: ctrl.signal }).then((objects) => {
        if (ctrl.signal.aborted) return;
        setSimbad({ status: 'ok', objects });
        ov?.setSimbad(objects);
      }, fail(setSimbad));
    } else if (kind === 'gaia') {
      setGaia({ status: 'loading' });
      ov?.setGaia([], null);
      queryGaia(s.ra, s.dec, s.radius, { limit: GAIA_LIMIT, signal: ctrl.signal }).then((stars) => {
        if (ctrl.signal.aborted) return;
        const years = arrowYears(stars, s.radius);
        setGaia({ status: 'ok', stars, years, limit: GAIA_LIMIT });
        ov?.setGaia(stars, years);
      }, fail(setGaia));
    } else if (kind === 'visits') {
      setVisits({ status: 'loading' });
      fetchVisits(s.ra, s.dec, { signal: ctrl.signal }).then((v) => {
        if (ctrl.signal.aborted) return;
        setVisits(v === null ? { status: 'none' } : { status: 'ok', visits: v });
      }, fail(setVisits));
    }
  }, []);

  const select = useCallback((ra, dec, { fov, label } = {}) => {
    const m = map.current;
    if (!m) return;
    const targetFov = fov ?? nextFov(m.aladin.getFov()[0]);
    m.aladin.gotoRaDec(ra, dec);
    m.aladin.setFoV(targetFov);
    const s = { ra, dec, radius: radiusFor(targetFov), label };
    setSel(s);
    setFocusId(null);
    m.overlays.setFocus(null);
    m.overlays.setRegion(ra, dec, s.radius);
    load('simbad', s);
    load('gaia', s);
    load('visits', s);
  }, [load]);

  const closePanel = useCallback(() => {
    Object.values(aborts.current).forEach((c) => c.abort());
    map.current?.overlays.clear();
    setSel(null);
    setFocusId(null);
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
    if (known) {
      goTarget(known);
      return;
    }
    const c = parseCoords(q);
    if (c) {
      setSearch({ status: 'idle' });
      select(c.ra, c.dec, { fov: TARGET_FOV });
      return;
    }
    setSearch({ status: 'loading' });
    try {
      const r = await resolveName(q);
      if (!r) {
        setSearch({ status: 'error', error: `No object named “${q}” was found.` });
        return;
      }
      setSearch({ status: 'idle' });
      select(r.ra, r.dec, { fov: TARGET_FOV, label: r.name });
    } catch (err) {
      setSearch({ status: 'error', error: `Name lookup failed (${err.message}). Try coordinates instead.` });
    }
  }, [goTarget, select]);

  const onReady = useCallback(({ aladin, A }) => {
    map.current = { aladin, A, overlays: createOverlays(A, aladin) };
    setReady(true);
    if (urlView.name) {
      setQuery(urlView.name);
      runSearch(urlView.name);
    } else if (urlView.ra != null && (urlView.fov ?? 360) <= 5) {
      select(urlView.ra, urlView.dec, { fov: urlView.fov });
    }
  }, [runSearch, select]);

  useEffect(() => {
    const ov = map.current?.overlays;
    if (!ov) return;
    ov.showSimbad(layers.simbad);
    ov.showGaia(layers.gaia);
  }, [layers, ready]);

  useEffect(() => {
    if (!copied) return undefined;
    const t = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(t);
  }, [copied]);

  const onFocus = useCallback((id, ra, dec) => {
    const m = map.current;
    if (!m) return;
    setFocusId(id);
    m.aladin.gotoRaDec(ra, dec);
    m.overlays.setFocus(ra, dec);
  }, []);

  const copyLink = useCallback(async () => {
    const url = window.location.href;
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      window.prompt('Copy this link:', url);
    }
    setCopied(true);
  }, []);

  return (
    <div className={`app ${sel ? 'has-panel' : ''}`}>
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden="true">✦</span>
          <div>
            <h1>SPHEREx PlanetX</h1>
            <p className="muted small">Click the sky to explore</p>
          </div>
        </div>

        <form className="search" role="search" onSubmit={(e) => { e.preventDefault(); runSearch(query); }}>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Name or RA Dec, e.g. R Dra or 269.45 +4.69"
            aria-label="Search by object name or coordinates"
            disabled={!ready}
          />
          <button type="submit" className="btn" disabled={!ready || search.status === 'loading'}>
            {search.status === 'loading' ? <span className="spinner" aria-label="Searching" /> : 'Go'}
          </button>
          {search.status === 'error' && <p className="search-error" role="alert">{search.error}</p>}
        </form>

        <div className="controls">
          <label className="select">
            <span className="sr-only">Background survey</span>
            <select value={survey} onChange={(e) => setSurvey(e.target.value)} aria-label="Background survey">
              {SURVEYS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
            </select>
          </label>
          <label className="toggle">
            <input type="checkbox" checked={layers.simbad} onChange={(e) => setLayers({ ...layers, simbad: e.target.checked })} />
            <span><i className="sw simbad" />SIMBAD</span>
          </label>
          <label className="toggle">
            <input type="checkbox" checked={layers.gaia} onChange={(e) => setLayers({ ...layers, gaia: e.target.checked })} />
            <span><i className="sw slow" />Gaia</span>
          </label>
        </div>

        <nav className="targets" aria-label="Test targets">
          {TARGETS.map((t) => (
            <button key={t.id} type="button" className="chip" title={t.note} disabled={!ready} onClick={() => goTarget(t)}>
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="stage">
        <SkyMap
          initial={initialView}
          survey={survey}
          onReady={onReady}
          onViewChange={writeViewToUrl}
          onSkyClick={(ra, dec) => select(ra, dec)}
        />
        {!ready && <div className="map-loading"><span className="spinner" /> Loading sky map…</div>}
        {ready && !sel && (
          <div className="hint">Click anywhere on the sky to zoom in and see what is there.</div>
        )}
        {sel && (
          <InfoPanel
            sel={sel}
            simbad={simbad}
            gaia={gaia}
            visits={visits}
            focusId={focusId}
            onFocus={onFocus}
            onClose={closePanel}
            onRetry={(kind) => load(kind, sel)}
            onCopyLink={copyLink}
            copied={copied}
          />
        )}
      </main>
      <span className="sr-only" aria-live="polite">
        {sel ? `Selected ${formatRa(sel.ra)} ${formatDec(sel.dec)}` : ''}
      </span>
    </div>
  );
}
