import { useEffect, useMemo, useRef, useState } from 'react';
import { equatorialToEcliptic, equatorialToGalactic, formatDec, formatFov, formatRa } from '../lib/coords.js';
import { FAST_ARCSEC_YR, displayName, sameObject } from '../lib/names.js';
import { dateSpan, monthYear, plainType, shortClaim, shortName, speed, verdict } from '../lib/plain.js';
import { CLASS, engineClass, knownClass } from '../lib/classes.js';
import { flaggedKnown, otherKnown, patchVerdict } from '../lib/patch.js';
import ObjectCard, { CloseIcon } from './ObjectCard.jsx';
import { ClassMark } from './ClassFilter.jsx';
import SurveyCandidateNote from './SurveyCandidateNote.jsx';

const signed = (v, d) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(d)}`;
const count = (n) => n.toLocaleString('en-US');
const plural = (n, one, many = `${one}s`) => `${count(n)} ${n === 1 ? one : many}`;
const KNOWN_SHOWN = 8;

function cadence(beta) {
  const b = Math.abs(beta);
  if (b >= 80) return 'Near an ecliptic pole, where SPHEREx passes on most orbits.';
  if (b >= 60) return 'High ecliptic latitude: SPHEREx comes by more often than average.';
  return 'SPHEREx comes by about every six months.';
}

function State({ state, what, onRetry }) {
  if (state.status === 'loading') return <p className="state"><span className="ticker" aria-hidden="true" />Loading {what}</p>;
  if (state.status === 'error') {
    return (
      <p className="state err" role="alert">
        Could not load {what}: {state.error}.{' '}
        <button type="button" className="textbtn" onClick={onRetry}>Retry</button>
      </p>
    );
  }
  return null;
}

/**
 * One pickable object in a list. Pointing at it (mouse or keyboard focus) marks it on the map;
 * `lit` is the reverse: the map's pointer is on this object, so the row lights up and scrolls into
 * view. `resume` puts keyboard focus on the row when the list appears (the object just left).
 */
function Row({ lit, resume, onPick, onPoint, className = '', children }) {
  const ref = useRef(null);
  useEffect(() => { if (lit) ref.current?.scrollIntoView({ block: 'nearest' }); }, [lit]);
  return (
    <li>
      <button
        ref={ref}
        type="button"
        className={`row ${className} ${lit ? 'lit' : ''}`}
        autoFocus={resume}
        onClick={onPick}
        onMouseEnter={() => onPoint(true)}
        onMouseLeave={() => onPoint(false)}
        onFocus={() => onPoint(true)}
        onBlur={() => onPoint(false)}
      >
        {children}
      </button>
    </li>
  );
}

const STAGES = { queued: 'Waiting for a free slot', listing: 'Listing SPHEREx images', analysing: 'Measuring what moved', checking: 'Checking older sky surveys' };

function Progress({ job }) {
  const fetching = job?.stage === 'downloading' && job.frames_total > 0;
  const text = fetching ? `Fetching images: ${job.frames_done} of ${job.frames_total}` : (job && STAGES[job.stage]) || 'Starting';
  return (
    <div className="progress" role="status">
      <p className="state"><span className="ticker" aria-hidden="true" />{text}</p>
      {fetching && <progress max={job.frames_total} value={job.frames_done} aria-label="Images fetched" />}
    </div>
  );
}

function EngineRow({ o, who, hover, resume, onPick, onPoint, mark = true }) {
  const f = { kind: 'engine', obj: o };
  const name = displayName(who);
  const what = shortClaim(o);
  return (
    <Row className="result" lit={sameObject(hover, f)} resume={sameObject(resume, f)} onPick={() => onPick(f)} onPoint={(p) => onPoint(p ? f : null)}>
      <span className="row-name" title={name}>{mark && <ClassMark cls={CLASS[engineClass(o)]} size={10} />}{shortName(name)}</span>
      <span className="badge">{verdict(o)}</span>
      {what && <span className="row-sub">{what}</span>}
    </Row>
  );
}

function KnownRow({ row, hover, resume, onPick, onPoint }) {
  const f = { kind: 'known', obj: row };
  const cls = knownClass(row);
  const isFast = cls === 'fast';
  return (
    <Row className="known" lit={sameObject(hover, f)} resume={sameObject(resume, f)} onPick={() => onPick(f)} onPoint={(p) => onPoint(p ? f : null)}>
      <span className="row-name" title={`${row.name} · ${CLASS[cls].label}`}><ClassMark cls={CLASS[cls]} size={10} />{shortName(row.name)}</span>
      <span className="row-type" title={row.type}>{plainType(row.type)}</span>
      <span className={`row-pm ${isFast ? 'signal' : 'dim'}`}>{isFast ? `${speed(row.pm)}″/yr` : ''}</span>
    </Row>
  );
}

function Clock({ getYear }) {
  const ref = useRef(null);
  useEffect(() => {
    let raf;
    const tick = () => {
      if (ref.current) ref.current.textContent = `Year ${count(Math.round(getYear()))}`;
      raf = requestAnimationFrame(tick);
    };
    tick();
    return () => cancelAnimationFrame(raf);
  }, [getYear]);
  return <span ref={ref} className="tabular" />;
}

/** The catalogued motion player: runs every star's Gaia motion forward on the map. */
function Player({ gaia, fast, playing, onTogglePlay, getYear, moversOnly, onToggleMovers }) {
  return (
    <div className="player">
      <button type="button" className="iconbtn" onClick={onTogglePlay} aria-label={playing ? 'Stop the star motion' : 'Play the star motion'}>
        {playing
          ? <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M0 0h10v10H0z" fill="currentColor" /></svg>
          : <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M0 0l10 6-10 6z" fill="currentColor" /></svg>}
      </button>
      <span className="player-text">
        {playing ? <Clock getYear={getYear} /> : `Play ${count(gaia.years)} years of star motion`}
      </span>
      {fast > 0 && (
        <button
          type="button"
          className={`opt legend ${moversOnly ? 'on' : ''}`}
          aria-pressed={moversOnly}
          title={`Show only what moves: fade every star slower than ${FAST_ARCSEC_YR}″ per year`}
          onClick={onToggleMovers}
        >
          <i className="dot signal-bg" />Moving only
        </button>
      )}
    </div>
  );
}

/**
 * The top of the panel: one line saying what this patch holds, then the way to measure it (or
 * how the measurement is going), and whatever failed to load.
 */
function Summary({ motion, results, known, simbad, gaia, visits, onRun, onRetry, runShown }) {
  const loaded = simbad.status !== 'loading' && gaia.status !== 'loading';
  const ran = motion.status === 'ok';
  const v = patchVerdict({ ran, results, rows: known.rows, loaded });
  const r = motion.result;
  return (
    <section className="summary" aria-labelledby="patch-verdict">
      <p id="patch-verdict" className={`verdict-line ${v.tone === 'plain' ? '' : v.tone}`} aria-live="polite">{v.text}</p>
      {ran && (
        <p className="line dim">
          SPHEREx: compared {plural(r.n_frames, 'image')} from {plural(r.passes.length, 'pass', 'passes')}
          {r.passes.length > 0 && ` (${dateSpan(r.passes[0].start, r.passes[r.passes.length - 1].end)})`}.
        </p>
      )}
      {!ran && visits.status === 'ok' && visits.count > 0 && (
        <p className="line dim">
          SPHEREx has imaged this spot {plural(visits.count, 'time')} in {plural(visits.passes.length, 'pass', 'passes')} since {monthYear(visits.passes[0].start)}.
        </p>
      )}
      {!ran && visits.status === 'ok' && visits.count === 0 && <p className="line dim">SPHEREx has no images of this spot yet.</p>}
      {!ran && visits.status === 'error' && (
        <p className="line dim">
          Could not check how often SPHEREx imaged this spot.{' '}
          <button type="button" className="textbtn" onClick={() => onRetry('visits')}>Retry</button>
        </p>
      )}
      {motion.status === 'none' && <p className="line dim">Measuring motion needs the PlanetX server, which is not connected.</p>}
      {motion.status === 'idle' && !runShown && (
        <div className="run">
          <button type="button" className="btn" onClick={onRun}>Find what moves</button>
          <p className="line dim">Compares about 50 SPHEREx images taken months apart. A minute or two the first time.</p>
        </div>
      )}
      {motion.status === 'loading' && <Progress job={motion.job} />}
      {motion.status === 'error' && (
        <p className="state err" role="alert">
          The run failed: {motion.error}.{' '}
          <button type="button" className="textbtn" onClick={onRun}>Try again</button>
        </p>
      )}
      <State state={simbad} what="named objects (SIMBAD)" onRetry={() => onRetry('simbad')} />
      <State state={gaia} what="stars (Gaia)" onRetry={() => onRetry('gaia')} />
    </section>
  );
}

/** Only what deserves a look: the engine's results, then catalogued fast movers and variables. */
function Flagged({ results: all, known, filter, gaia, hover, resume, onPick, onPoint, player }) {
  const results = all.filter(({ o }) => filter[engineClass(o)]);
  const flagged = flaggedKnown(known.rows, all);
  const rows = flagged.filter((row) => filter[knownClass(row)]);
  const hidden = all.length - results.length + flagged.length - rows.length;
  const fast = known.rows.filter((r) => r.pm >= FAST_ARCSEC_YR).length;
  if (!all.length && !flagged.length && gaia.status !== 'ok') return null;
  return (
    <section aria-labelledby="flagged-title">
      <h3 id="flagged-title"><span>Worth a look</span></h3>
      {(results.length > 0 || rows.length > 0) && (
        <ul className="rowlist">
          {results.map(({ o, who }) => <EngineRow key={o.id} o={o} who={who} hover={hover} resume={resume} onPick={onPick} onPoint={onPoint} />)}
          {rows.map((row) => <KnownRow key={row.key} row={row} hover={hover} resume={resume} onPick={onPick} onPoint={onPoint} />)}
        </ul>
      )}
      {!results.length && !rows.length && !hidden && <p className="line dim">Nothing here is known to move or vary. Pick any star on the map to see its pictures.</p>}
      {hidden > 0 && <p className="line dim">{plural(hidden, 'more', 'more')} hidden by the map filter.</p>}
      {gaia.status === 'ok' && gaia.years && <Player gaia={gaia} fast={fast} {...player} />}
    </section>
  );
}

/** What most visitors do not need, behind one control: fixed stars, the engine's notes, coordinates, passes, credits. */
function More({ sel, visits, motion, known, results, filter, gaia, others, onToggleOthers, hover, resume, onPick, onPoint }) {
  const rest = useMemo(() => otherKnown(known.rows, results).filter((row) => filter[knownClass(row)]), [known, results, filter]);
  const inside = Boolean(others) || rest.some((row) => sameObject(resume, { kind: 'known', obj: row }));
  const [open, setOpen] = useState(inside);
  const [all, setAll] = useState(() => rest.findIndex((row) => sameObject(resume, { kind: 'known', obj: row })) >= KNOWN_SHOWN);
  const few = rest.length <= KNOWN_SHOWN + 2;
  const shown = all || few ? rest : rest.slice(0, KNOWN_SHOWN);
  const { g, e } = useMemo(() => ({
    g: equatorialToGalactic(sel.ra, sel.dec),
    e: equatorialToEcliptic(sel.ra, sel.dec),
  }), [sel.ra, sel.dec]);
  const r = motion.status === 'ok' ? motion.result : null;
  return (
    <details className="more" open={open} onToggle={(ev) => setOpen(ev.currentTarget.open)}>
      <summary>More about this patch</summary>
      {open && (
        <>
          <h4>Other known objects{rest.length ? ` · ${count(rest.length)}` : ''}</h4>
          {shown.length > 0 && (
            <ul className="rowlist">
              {shown.map((row) => <KnownRow key={row.key} row={row} hover={hover} resume={resume} onPick={onPick} onPoint={onPoint} />)}
            </ul>
          )}
          {!few && (
            <p className="line">
              <button type="button" className="textbtn" aria-expanded={all} onClick={() => setAll(!all)}>
                {all ? 'Show fewer' : `Show all ${count(rest.length)}`}
              </button>
            </p>
          )}
          {gaia.status === 'ok' && (
            <p className="line dim">
              The dots on the map are {count(gaia.stars.length)}{gaia.capped ? '+' : ''} stars from the Gaia catalogue. Click any of them to see its pictures.
            </p>
          )}

          {r && (
            <>
              <h4>What the engine tracked</h4>
              <p className="line dim">It followed {plural(r.objects.length, 'object')}; most are fixed stars.</p>
              {r.objects.length > 0 && (
                <p className="line">
                  <button type="button" className="textbtn" aria-expanded={Boolean(others)} onClick={onToggleOthers}>
                    {others ? 'Hide the tracked objects' : 'List every tracked object'}
                  </button>
                </p>
              )}
              {others && (
                <ul className="rowlist">
                  {others.map(({ o, who }) => <EngineRow key={o.id} o={o} who={who} hover={hover} resume={resume} onPick={onPick} onPoint={onPoint} mark={false} />)}
                </ul>
              )}
              {r.notes.length > 0 && (
                <>
                  <h4>Engine notes</h4>
                  {r.notes.map((note) => <p key={note} className="line dim">{note}</p>)}
                </>
              )}
            </>
          )}

          <h4>Coordinates of the centre</h4>
          <dl className="kv">
            <dt>Equatorial</dt><dd>RA {sel.ra.toFixed(5)}° · Dec {signed(sel.dec, 5)}°</dd>
            <dt>Galactic</dt><dd>l {g.l.toFixed(4)}° · b {signed(g.b, 4)}°</dd>
            <dt>Ecliptic</dt><dd>lon {e.lambda.toFixed(4)}° · lat {signed(e.beta, 4)}°</dd>
            <dt>Radius</dt><dd>{formatFov(sel.radius)}</dd>
          </dl>
          <h4>SPHEREx coverage</h4>
          <p className="line">{cadence(e.beta)}</p>
          {visits.status === 'none' && <p className="line dim">Pass dates need the PlanetX server, which is not connected.</p>}
          {visits.status === 'loading' && <p className="state"><span className="ticker" aria-hidden="true" />Listing SPHEREx passes</p>}
          {visits.status === 'ok' && visits.passes.length > 0 && (
            <ol className="passes">
              {visits.passes.map((p) => (
                <li key={p.start}><span>{dateSpan(p.start, p.end)}</span><span className="dim">{plural(p.n_visits, 'image')}</span></li>
              ))}
            </ol>
          )}
          <p className="credits">Data: NASA/SPHEREx DR1 + QR3 (IRSA) · ESA Gaia DR3 (VizieR) · SIMBAD (CDS) · 2MASS, AllWISE, CatWISE (IRSA)</p>
        </>
      )}
    </details>
  );
}

/**
 * The side panel for the selected patch of sky: a one-line verdict, the way to measure it, the
 * few objects worth a look, and everything else behind "More". Picking any object swaps the
 * panel for that object's card, with a way back.
 */
export default function InfoPanel({
  sel, simbad, gaia, visits, motion, known, results, others, onToggleOthers, focus, hover, resume, onPick, onOpen, onPoint, onBack, onClose, onRetry,
  lists, backLabel, candidate,
  onCopyLink, copied, playing, onTogglePlay, getYear, filter, moversOnly, onToggleMovers,
}) {
  if (focus) {
    return (
      <aside className="panel" aria-label="Selected object">
        <ObjectCard
          key={focus.kind === 'engine' ? `engine:${focus.obj.id}` : focus.obj.key}
          focus={focus}
          results={results}
          others={others}
          objects={motion.status === 'ok' ? motion.result.objects : null}
          field={motion.result?.field}
          lists={lists}
          backLabel={backLabel}
          onPick={onOpen}
          onBack={onBack}
          onClose={onClose}
        />
      </aside>
    );
  }
  const width = sel.radius * 2;
  const pointing = { hover, resume, onPick, onPoint };
  return (
    <aside className="panel" aria-label="Selected patch of sky">
      <header className="panel-head">
        <div className="panel-tools">
          <span className="kicker">Patch of sky · {width >= 1 ? width.toFixed(1) : width.toFixed(2)}° wide</span>
          <span className="tools">
            <button type="button" className="textbtn" onClick={onCopyLink}>{copied ? 'Copied' : 'Copy link'}</button>
            <button type="button" className="iconbtn" onClick={onClose} aria-label="Close panel"><CloseIcon /></button>
          </span>
        </div>
        {sel.label
          ? <h2 className="title">{sel.label}</h2>
          : <h2 className="title title-coords">{formatRa(sel.ra)}<br />{formatDec(sel.dec)}</h2>}
        {sel.label && <p className="coords">{formatRa(sel.ra)}&ensp;{formatDec(sel.dec)}</p>}
      </header>

      <div className="panel-body">
        {candidate && <SurveyCandidateNote candidate={candidate} motion={motion} onRun={() => onRetry('motion')} />}
        <Summary
          motion={motion} results={results} known={known} simbad={simbad} gaia={gaia} visits={visits}
          onRun={() => onRetry('motion')} onRetry={onRetry} runShown={Boolean(candidate)}
        />
        <Flagged
          results={results} known={known} filter={filter} gaia={gaia} {...pointing}
          player={{ playing, onTogglePlay, getYear, moversOnly, onToggleMovers }}
        />
        <More
          sel={sel} visits={visits} motion={motion} known={known} results={results} filter={filter} gaia={gaia}
          others={others} onToggleOthers={onToggleOthers} {...pointing}
        />
      </div>
    </aside>
  );
}
