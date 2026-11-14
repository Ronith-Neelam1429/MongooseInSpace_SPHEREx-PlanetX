import { useEffect, useMemo, useRef } from 'react';
import {
  equatorialToEcliptic, equatorialToGalactic, formatDec, formatFov, formatRa,
} from '../lib/coords.js';
import { FAST_PM, MEASURED, measuredMotion } from '../lib/starfield.js';

const n = (v, d = 2) => (v == null || !Number.isFinite(v) ? '–' : v.toFixed(d));
const signed = (v, d) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(d)}`;
const pmText = (pm) => (pm >= 1000 ? `${(pm / 1000).toFixed(2)}″` : `${pm.toFixed(1)}`);

function cadence(beta) {
  const b = Math.abs(beta);
  if (b >= 80) return 'Ecliptic pole. SPHEREx passes here on most orbits.';
  if (b >= 60) return 'High ecliptic latitude. Above-average SPHEREx coverage.';
  return 'Covered about every six months.';
}

function State({ state, what, onRetry }) {
  if (state.status === 'loading') return <p className="state"><span className="ticker" aria-hidden="true" />Querying {what}</p>;
  if (state.status === 'error') {
    return (
      <p className="state err" role="alert">
        {what} unavailable: {state.error}.{' '}
        <button type="button" className="textbtn" onClick={onRetry}>Retry</button>
      </p>
    );
  }
  return null;
}

const STAGES = { queued: 'Waiting for the engine', listing: 'Listing exposures', analysing: 'Measuring motion', checking: 'Checking archival catalogues' };

function motionProgress(job) {
  if (!job) return 'Starting the engine';
  if (job.stage === 'downloading') return `Fetching frames ${job.frames_done}/${job.frames_total}`;
  return STAGES[job.stage] || 'Working';
}

const KIND = { mover: 'Mover', candidate: 'Candidate', static: 'Variable' };
const VERDICT = {
  confirmed: 'Motion confirmed by archival positions',
  stationary: 'Stationary in archival positions',
  refuted: 'Refuted: separate archival sources at each position',
  unconfirmed: 'Not enough archival data to check',
  spherex_only: 'SPHEREx only',
};
const OBSERVATORY = { spherex: 'SPHEREx', twomass: '2MASS', allwise: 'AllWISE', catwise: 'CatWISE', neowise: 'NEOWISE' };

/** The motion engine's section: a run button, progress, then the movers, candidates and variables. */
function Motion({ motion, focus, onFocus, onRun }) {
  const r = motion.result;
  const rows = useMemo(() => (r ? r.objects.filter((o) => o.kind !== 'static' || o.is_variable) : []), [r]);
  return (
    <section>
      <h3>
        Motion engine
        {motion.status === 'ok' && <span className="count">{r.objects.length} tracked</span>}
      </h3>
      {motion.status === 'none' && <p className="line dim">The engine needs the PlanetX API, which is not connected.</p>}
      {motion.status === 'idle' && (
        <>
          <p className="line">Measure what moves or changes brightness between SPHEREx passes of this field.</p>
          <p className="line dim">Fetches about 50 frames from IRSA: a minute or two the first time.</p>
          <p className="line"><button type="button" className="textbtn" onClick={onRun}>Run engine</button></p>
        </>
      )}
      {motion.status === 'loading' && <p className="state"><span className="ticker" aria-hidden="true" />{motionProgress(motion.job)}</p>}
      {motion.status === 'error' && (
        <p className="state err" role="alert">
          Engine run failed: {motion.error}.{' '}
          <button type="button" className="textbtn" onClick={onRun}>Retry</button>
        </p>
      )}
      {motion.status === 'ok' && (
        <>
          <p className="line">
            {r.n_frames} frames in {r.passes.length} {r.passes.length === 1 ? 'pass' : 'passes'}
            {r.passes.length > 0 && <span className="dim"> · {r.passes[0].start} to {r.passes[r.passes.length - 1].end}</span>}
          </p>
          {rows.length === 0
            ? <p className="line dim">Nothing moving or varying above the threshold.</p>
            : (
              <table className="rows measured">
                <thead><tr><th><i className="dot" style={{ background: `rgb(${MEASURED})` }} /> Measured</th><th>µ ″/yr</th><th>σ</th></tr></thead>
                <tbody>
                  {rows.map((o) => (
                    <tr
                      key={o.id}
                      className={focus?.obj === o ? 'on' : ''}
                      onClick={() => onFocus('engine', o)}
                      onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onFocus('engine', o); } }}
                      tabIndex={0}
                    >
                      <td className="name">{KIND[o.kind]}{o.kind !== 'static' && o.is_variable ? ' · variable' : ''}</td>
                      <td>{o.kind === 'static' ? `×${n(o.variability?.ratio)}` : n(measuredMotion(o).pm)}</td>
                      <td>{n(o.kind === 'static' ? o.variability?.significance : measuredMotion(o).sigma, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          {r.notes.map((note) => <p key={note} className="line dim">{note}</p>)}
        </>
      )}
    </section>
  );
}

function EngineFocus({ o, onClear }) {
  const a = o.archive;
  const others = a.observatories.filter((x) => x !== 'spherex').map((x) => OBSERVATORY[x] || x);
  const title = o.kind === 'static' ? 'Variable source' : o.kind === 'candidate' ? 'Candidate mover' : 'Moving source';
  return (
    <section className="focus">
      <header><span className="kicker">SPHEREx engine</span><button type="button" className="textbtn" onClick={onClear}>Clear</button></header>
      <p className="focus-name">{title}</p>
      <dl className="kv">
        <dt>µ SPHEREx</dt><dd>{n(o.pm_total)} ± {n(o.pm_err)} ″/yr · {n(o.significance, 1)}σ</dd>
        <dt>µα*, µδ</dt><dd>{signed(o.pm_ra_cosdec, 2)}, {signed(o.pm_dec, 2)} ″/yr · PA {n(o.position_angle_deg, 0)}°</dd>
        {o.parallax_arcsec != null && <><dt>ϖ</dt><dd>{n(o.parallax_arcsec)} ± {n(o.parallax_err_arcsec)} ″</dd></>}
        <dt>Passes</dt><dd>{o.track.map((t) => t.date).join(' · ')}</dd>
        {o.variability && <><dt>Flux</dt><dd>{o.variability.band} {o.variability.change} · {n(o.variability.significance, 1)}σ</dd></>}
        {o.variability?.caution && <><dt /><dd className="dim">{o.variability.caution}</dd></>}
        <dt>Archive</dt><dd>{VERDICT[a.verdict] || a.verdict}{others.length > 0 && <span className="dim"> ({others.join(', ')})</span>}</dd>
        {others.length > 0 && a.pm_ra_cosdec != null && a.pm_err != null && (
          <><dt>µ fused</dt><dd>{signed(a.pm_ra_cosdec, 3)}, {signed(a.pm_dec, 3)} ± {n(a.pm_err, 3)} ″/yr · {n(a.baseline_yr, 1)} yr</dd></>
        )}
        <dt>Position</dt><dd>{formatRa(o.ra)} {formatDec(o.dec)}</dd>
        {o.saturated && <><dt /><dd className="dim">Saturated star: position from its masked core.</dd></>}
      </dl>
    </section>
  );
}

function Epoch({ getYears, total }) {
  const ref = useRef(null);
  useEffect(() => {
    let raf;
    const tick = () => {
      if (ref.current) ref.current.textContent = `+${Math.round(getYears()).toLocaleString('en-US')}`;
      raf = requestAnimationFrame(tick);
    };
    tick();
    return () => cancelAnimationFrame(raf);
  }, [getYears]);
  return <span className="epoch"><span>2016.0</span><span ref={ref} className="tabular">+{total}</span><span className="dim">yr</span></span>;
}

function Focused({ focus, onClear }) {
  const o = focus.obj;
  if (focus.kind === 'engine') return <EngineFocus o={o} onClear={onClear} />;
  if (focus.kind === 'simbad') {
    return (
      <section className="focus">
        <header><span className="kicker">SIMBAD</span><button type="button" className="textbtn" onClick={onClear}>Clear</button></header>
        <p className="focus-name">{o.name}</p>
        <dl className="kv">
          <dt>Type</dt><dd>{o.type || '–'}</dd>
          <dt>Mag</dt><dd>{o.mag != null ? `${o.band} ${n(o.mag)}` : '–'}</dd>
          {o.pmra != null && <><dt>µ</dt><dd>{pmText(Math.hypot(o.pmra, o.pmdec))} mas/yr</dd></>}
          <dt>Position</dt><dd>{formatRa(o.ra)} {formatDec(o.dec)}</dd>
        </dl>
      </section>
    );
  }
  const pm = o.pmra != null ? Math.hypot(o.pmra, o.pmdec) : null;
  return (
    <section className="focus">
      <header><span className="kicker">Gaia DR3</span><button type="button" className="textbtn" onClick={onClear}>Clear</button></header>
      <p className="focus-name mono">{o.id}</p>
      <dl className="kv">
        <dt>G</dt><dd>{n(o.gmag)}</dd>
        <dt>µα*, µδ</dt><dd>{o.pmra != null ? `${signed(o.pmra, 1)}, ${signed(o.pmdec, 1)} mas/yr` : '–'}</dd>
        <dt>µ</dt><dd className={pm >= FAST_PM ? 'signal' : ''}>{pm != null ? `${pmText(pm)} mas/yr` : '–'}</dd>
        <dt>ϖ</dt><dd>{o.plx != null ? `${n(o.plx)} mas${o.plx > 0 ? ` · ${(1000 / o.plx).toFixed(o.plx > 100 ? 2 : 0)} pc` : ''}` : '–'}</dd>
      </dl>
    </section>
  );
}

export default function InfoPanel({
  sel, simbad, gaia, visits, motion, focus, onFocus, onClearFocus, onClose, onRetry, onCopyLink, copied,
  playing, onTogglePlay, getYears, moversOnly, onToggleMovers,
}) {
  const { g, e } = useMemo(() => ({
    g: equatorialToGalactic(sel.ra, sel.dec),
    e: equatorialToEcliptic(sel.ra, sel.dec),
  }), [sel.ra, sel.dec]);

  const movers = useMemo(() => (gaia.status !== 'ok' ? [] : gaia.stars
    .filter((s) => s.pmra != null && s.pmdec != null)
    .map((s) => ({ s, pm: Math.hypot(s.pmra, s.pmdec) }))
    .sort((a, b) => b.pm - a.pm)
    .slice(0, 8)), [gaia]);

  const fastCount = useMemo(() => movers.filter((m) => m.pm >= FAST_PM).length, [movers]);

  return (
    <aside className="panel" aria-label="Field details">
      <header className="panel-head">
        <div className="panel-tools">
          <span className="kicker">{sel.label ? 'Target' : 'Field'} · r {formatFov(sel.radius)}</span>
          <span className="tools">
            <button type="button" className="textbtn" onClick={onCopyLink}>{copied ? 'Copied' : 'Copy link'}</button>
            <button type="button" className="iconbtn" onClick={onClose} aria-label="Close panel">
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M1 1l10 10M11 1L1 11" stroke="currentColor" strokeWidth="1.25" /></svg>
            </button>
          </span>
        </div>
        {sel.label
          ? <h2 className="title">{sel.label}</h2>
          : <h2 className="title title-coords">{formatRa(sel.ra)}<br />{formatDec(sel.dec)}</h2>}
        {sel.label && <p className="coords">{formatRa(sel.ra)}&ensp;{formatDec(sel.dec)}</p>}
      </header>

      <div className="panel-body">
        <section>
          <h3>Position</h3>
          <dl className="grid4">
            <dt>α</dt><dd>{sel.ra.toFixed(5)}°</dd><dt>δ</dt><dd>{signed(sel.dec, 5)}°</dd>
            <dt>l</dt><dd>{g.l.toFixed(4)}°</dd><dt>b</dt><dd>{signed(g.b, 4)}°</dd>
            <dt>λ</dt><dd>{e.lambda.toFixed(4)}°</dd><dt>β</dt><dd>{signed(e.beta, 4)}°</dd>
          </dl>
        </section>

        <section>
          <h3>SPHEREx</h3>
          <p className="line">{cadence(e.beta)}</p>
          {visits.status === 'none' && <p className="line dim">Visit dates need the PlanetX API, which is not connected.</p>}
          <State state={visits} what="Visits" onRetry={() => onRetry('visits')} />
          {visits.status === 'ok' && (visits.visits.length === 0
            ? <p className="line dim">No visits yet.</p>
            : (
              <ol className="visits">
                {visits.visits.map((v, i) => (
                  // obs_id is shared by an exposure's 6 detectors; access_url is unique per visit.
                  <li key={v.access_url || i}><span>{String(v.date || '').slice(0, 10) || '–'}</span><span className="dim">{v.band || (v.detector ? `D${v.detector}` : '')}</span></li>
                ))}
              </ol>
            ))}
        </section>

        <Motion motion={motion} focus={focus} onFocus={onFocus} onRun={() => onRetry('motion')} />

        <section>
          <h3>
            Proper motion
            {gaia.status === 'ok' && <span className="count">{gaia.stars.length.toLocaleString('en-US')}{gaia.stars.length >= gaia.limit ? '+' : ''}</span>}
          </h3>
          <State state={gaia} what="Gaia DR3" onRetry={() => onRetry('gaia')} />
          {gaia.status === 'ok' && (gaia.years ? (
            <>
              <div className="player">
                <button type="button" className="iconbtn" onClick={onTogglePlay} aria-label={playing ? 'Pause motion' : 'Play motion'}>
                  {playing
                    ? <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M1.5 0v12M8.5 0v12" stroke="currentColor" strokeWidth="2" /></svg>
                    : <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M0 0l10 6-10 6z" fill="currentColor" /></svg>}
                </button>
                <Epoch getYears={getYears} total={gaia.years.toLocaleString('en-US')} />
                <button
                  type="button"
                  className={`opt legend ${moversOnly ? 'on' : ''}`}
                  aria-pressed={moversOnly}
                  title={`Show only stars moving faster than ${FAST_PM} mas/yr`}
                  onClick={onToggleMovers}
                >
                  <i className="dot signal-bg" />Movers ≥ {FAST_PM}
                </button>
              </div>
              {movers.length > 0 && (
                <table className="rows">
                  <thead><tr><th>Gaia DR3</th><th>G</th><th>µ mas/yr</th></tr></thead>
                  <tbody>
                    {movers.map(({ s, pm }) => (
                      <tr
                        key={s.id}
                        className={focus?.obj === s ? 'on' : ''}
                        onClick={() => onFocus('gaia', s)}
                        onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onFocus('gaia', s); } }}
                        tabIndex={0}
                      >
                        <td className="id">{s.id}</td>
                        <td>{n(s.gmag, 1)}</td>
                        <td className={pm >= FAST_PM ? 'signal' : ''}>{pmText(pm)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {fastCount === 0 && <p className="line dim">No star here moves faster than {FAST_PM} mas/yr.</p>}
            </>
          ) : <p className="line dim">No proper motions measured in this field.</p>)}
        </section>

        <section>
          <h3>
            Catalogued
            {simbad.status === 'ok' && <span className="count">{simbad.objects.length}{simbad.objects.length >= 200 ? '+' : ''}</span>}
          </h3>
          <State state={simbad} what="SIMBAD" onRetry={() => onRetry('simbad')} />
          {simbad.status === 'ok' && (simbad.objects.length === 0
            ? <p className="line dim">Nothing catalogued in this field.</p>
            : (
              <table className="rows">
                <thead><tr><th>Name</th><th>Type</th><th>Mag</th></tr></thead>
                <tbody>
                  {simbad.objects.map((o, i) => (
                    <tr
                      key={`${o.name}-${i}`}
                      className={focus?.obj === o ? 'on' : ''}
                      onClick={() => onFocus('simbad', o)}
                      onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onFocus('simbad', o); } }}
                      tabIndex={0}
                    >
                      <td className="name" title={o.name}>{o.name}</td>
                      <td className="type" title={o.type}>{o.type}</td>
                      <td>{o.mag != null ? n(o.mag, 1) : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ))}
        </section>

        <footer className="credits">Data: NASA/SPHEREx DR1 + QR3 (IRSA) · ESA Gaia DR3 (VizieR) · SIMBAD (CDS) · 2MASS, AllWISE, CatWISE (IRSA)</footer>
      </div>
      {focus && <Focused focus={focus} onClear={onClearFocus} />}
    </aside>
  );
}
