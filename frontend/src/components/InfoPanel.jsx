import { useEffect, useMemo, useRef, useState } from 'react';
import { equatorialToEcliptic, equatorialToGalactic, formatDec, formatFov, formatRa } from '../lib/coords.js';
import { FAST_ARCSEC_YR, displayName, sameObject } from '../lib/names.js';
import { plainType, shortClaim, shortName, speed, verdict } from '../lib/plain.js';
import { CLASS, engineClass, knownClass } from '../lib/classes.js';
import { flaggedKnown, otherKnown, patchVerdict } from '../lib/patch.js';
import ObjectCard, { CloseIcon } from './ObjectCard.jsx';
import { ClassMark } from './ClassFilter.jsx';
import SurveyCandidateNote from './SurveyCandidateNote.jsx';
import Stats from './Stats.jsx';
import Coverage from './Coverage.jsx';
import { baseline } from '../lib/coverage.js';

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
  const type = plainType(row.type);
  return (
    <Row className="known" lit={sameObject(hover, f)} resume={sameObject(resume, f)} onPick={() => onPick(f)} onPoint={(p) => onPoint(p ? f : null)}>
      <span className="row-name" title={`${row.name} · ${CLASS[cls].label}`}><ClassMark cls={CLASS[cls]} size={10} />{shortName(row.name)}</span>
      <span className={`row-pm ${isFast ? 'signal' : 'dim'}`}>{isFast ? `${speed(row.pm)}″/yr` : ''}</span>
      {type && <span className="row-type" title={row.type}>{type}</span>}
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

/** One line for a catalogue or service that failed, with a way to ask again. */
function Failed({ what, error, onRetry }) {
  return (
    <p className="state err" role="alert">
      Could not load {what}{error ? <span className="dim"> ({error})</span> : null}.{' '}
      <button type="button" className="textbtn" onClick={onRetry}>Retry</button>
    </p>
  );
}

/**
 * The top of the panel: whether the patch was measured, one line saying what it holds, the way to
 * measure it (or how that is going), and whatever failed to load.
 */
function Summary({ motion, results, known, simbad, gaia, onRun, onRetry, runShown }) {
  const loading = simbad.status === 'loading' || gaia.status === 'loading';
  const ran = motion.status === 'ok';
  const v = patchVerdict({ ran, results, rows: known.rows, loaded: !loading });
  return (
    <section className="summary" aria-labelledby="patch-verdict">
      <p className="kicker status">
        <i className={`dot ${ran ? 'measured-bg' : 'plain-bg'}`} />
        {ran ? 'Measured in SPHEREx images' : 'From the catalogues · not measured yet'}
      </p>
      <p id="patch-verdict" className={`verdict-line ${v.tone === 'plain' ? '' : v.tone}`} aria-live="polite">{v.text}</p>
      {motion.status === 'none' && <p className="line dim">Measuring motion needs the AstroSearchX server, which is not connected.</p>}
      {motion.status === 'idle' && !runShown && (
        <div className="run">
          <button type="button" className="btn" onClick={onRun}>Find what moves</button>
          <p className="line dim">Compares about 50 SPHEREx images taken months apart. A minute or two the first time.</p>
        </div>
      )}
      {motion.status === 'loading' && <Progress job={motion.job} />}
      {motion.status === 'error' && (
        <p className="state err" role="alert">
          The run failed <span className="dim">({motion.error})</span>.{' '}
          <button type="button" className="textbtn" onClick={onRun}>Try again</button>
        </p>
      )}
      {loading && <p className="state"><span className="ticker" aria-hidden="true" />Looking up {[simbad.status === 'loading' && 'SIMBAD', gaia.status === 'loading' && 'Gaia'].filter(Boolean).join(' and ')}</p>}
      {simbad.status === 'error' && <Failed what="named objects (SIMBAD)" error={simbad.error} onRetry={() => onRetry('simbad')} />}
      {gaia.status === 'error' && <Failed what="stars (Gaia)" error={gaia.error} onRetry={() => onRetry('gaia')} />}
    </section>
  );
}

/**
 * The patch in four figures. Before a run: how often SPHEREx imaged it, over how long, and how many
 * stars and named objects the catalogues hold. After: what the engine used, tracked and flagged.
 */
function PatchStats({ motion, results, simbad, gaia, visits }) {
  const r = motion.status === 'ok' ? motion.result : null;
  // undefined = still loading, null = not available (Stats shows a dash).
  const of = (state, value) => (state.status === 'loading' || state.status === 'idle' ? undefined : state.status === 'ok' ? value() : null);
  const passes = r ? r.passes : visits.status === 'ok' ? visits.passes : null;
  const items = r
    ? [
      { label: 'Images used', value: count(r.n_frames), sub: plural(r.passes.length, 'pass', 'passes'), title: 'SPHEREx images the engine compared' },
      { label: 'Span', value: baseline(r.passes) ?? null, sub: 'baseline', title: 'Time from the first image to the last: longer spans show slower motions' },
      { label: 'Tracked', value: count(r.objects.length), sub: 'objects', title: 'Objects followed from image to image; most are fixed stars' },
      { label: 'Flagged', value: count(results.length), sub: 'notable', tone: results.length ? 'measured' : '', title: 'Movers, variables and candidates' },
    ]
    : [
      {
        label: 'SPHEREx', value: visits.status === 'none' ? null : of(visits, () => count(visits.count)),
        sub: passes ? plural(passes.length, 'pass', 'passes') : 'images', title: 'SPHEREx images of this spot so far',
      },
      { label: 'Span', value: visits.status === 'none' ? null : of(visits, () => baseline(passes) ?? null), sub: 'baseline', title: 'Time from the first image to the last: longer spans show slower motions' },
      { label: 'Gaia', value: of(gaia, () => `${count(gaia.stars.length)}${gaia.capped ? '+' : ''}`), sub: 'stars', title: 'Stars from Gaia DR3 inside the patch (the dots on the map)' },
      { label: 'SIMBAD', value: of(simbad, () => count(simbad.objects.length)), sub: 'named', title: 'Objects with a name in SIMBAD inside the patch' },
    ];
  return <Stats items={items} label="This patch in numbers" />;
}

/** Only what deserves a look: the engine's results, then catalogued fast movers and variables. */
function Flagged({ results: all, known, filter, gaia, hover, resume, onPick, onPoint, player }) {
  const results = all.filter(({ o }) => filter[engineClass(o)]);
  const flagged = flaggedKnown(known.rows, all);
  const rows = flagged.filter((row) => filter[knownClass(row)]);
  const hidden = all.length - results.length + flagged.length - rows.length;
  const fast = known.rows.filter((r) => r.pm >= FAST_ARCSEC_YR).length;
  if (!all.length && !flagged.length && gaia.status !== 'ok') return null;
  const n = results.length + rows.length;
  return (
    <section aria-labelledby="flagged-title">
      <h3 id="flagged-title"><span>Worth a look</span>{n > 0 && <span className="count">{count(n)}</span>}</h3>
      {n > 0 && (
        <ul className="rowlist">
          {results.map(({ o, who }) => <EngineRow key={o.id} o={o} who={who} hover={hover} resume={resume} onPick={onPick} onPoint={onPoint} />)}
          {rows.map((row) => <KnownRow key={row.key} row={row} hover={hover} resume={resume} onPick={onPick} onPoint={onPoint} />)}
        </ul>
      )}
      {!n && !hidden && <p className="line dim">Nothing here is known to move or vary. Pick any star on the map to see its pictures.</p>}
      {hidden > 0 && <p className="line dim">{plural(hidden, 'more', 'more')} hidden by the map filter.</p>}
      {gaia.status === 'ok' && gaia.years && <Player gaia={gaia} fast={fast} {...player} />}
    </section>
  );
}

/** When and in which colours SPHEREx imaged the patch. Left out when there is no server to ask. */
function CoverageSection({ visits, beta, onRetry }) {
  if (visits.status === 'none' || visits.status === 'idle') return null;
  return (
    <section aria-labelledby="coverage-title">
      <h3 id="coverage-title"><span>SPHEREx coverage</span></h3>
      {visits.status === 'loading' && <p className="state"><span className="ticker" aria-hidden="true" />Listing SPHEREx images</p>}
      {visits.status === 'error' && <Failed what="the SPHEREx image list" error={visits.error} onRetry={() => onRetry('visits')} />}
      {visits.status === 'ok' && visits.count === 0 && <p className="line dim">SPHEREx has no images of this spot yet.</p>}
      {visits.status === 'ok' && visits.count > 0 && <Coverage visits={visits} />}
      <p className="line dim cov-note">{cadence(beta)}</p>
    </section>
  );
}

/** Folded sections for what most visitors do not need; each shows only when it has something. */
function More({ sel, motion, known, results, filter, gaia, others, onToggleOthers, hover, resume, onPick, onPoint }) {
  const rest = useMemo(() => otherKnown(known.rows, results).filter((row) => filter[knownClass(row)]), [known, results, filter]);
  const resumeAt = rest.findIndex((row) => sameObject(resume, { kind: 'known', obj: row }));
  const [all, setAll] = useState(() => resumeAt >= KNOWN_SHOWN);
  const few = rest.length <= KNOWN_SHOWN + 2;
  const shown = all || few ? rest : rest.slice(0, KNOWN_SHOWN);
  const { g, e } = useMemo(() => ({
    g: equatorialToGalactic(sel.ra, sel.dec),
    e: equatorialToEcliptic(sel.ra, sel.dec),
  }), [sel.ra, sel.dec]);
  const r = motion.status === 'ok' ? motion.result : null;
  return (
    <div className="folds">
      {rest.length > 0 && (
        <Fold title="Other objects here" count={rest.length} open={resumeAt >= 0}>
          <ul className="rowlist">
            {shown.map((row) => <KnownRow key={row.key} row={row} hover={hover} resume={resume} onPick={onPick} onPoint={onPoint} />)}
          </ul>
          {!few && (
            <p className="line">
              <button type="button" className="textbtn" aria-expanded={all} onClick={() => setAll(!all)}>
                {all ? 'Show fewer' : `Show all ${count(rest.length)}`}
              </button>
            </p>
          )}
          {gaia.status === 'ok' && <p className="line dim">Every dot on the map is a Gaia star: click one to see its pictures.</p>}
        </Fold>
      )}

      {r && (
        <Fold title="Engine details" count={r.objects.length} open={Boolean(others)}>
          <p className="line dim">It followed {plural(r.objects.length, 'object')} from image to image; most are fixed stars.</p>
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
              <h4>Notes</h4>
              {r.notes.map((note) => <p key={note} className="line dim">{note}</p>)}
            </>
          )}
        </Fold>
      )}

      <Fold title="Position">
        <dl className="kv">
          <dt>Equatorial</dt><dd>RA {sel.ra.toFixed(5)}° · Dec {signed(sel.dec, 5)}°</dd>
          <dt>Galactic</dt><dd>l {g.l.toFixed(4)}° · b {signed(g.b, 4)}°</dd>
          <dt>Ecliptic</dt><dd>lon {e.lambda.toFixed(4)}° · lat {signed(e.beta, 4)}°</dd>
          <dt>Radius</dt><dd>{formatFov(sel.radius)}</dd>
        </dl>
      </Fold>
    </div>
  );
}

/** A folded section; mounts its content only while open. */
function Fold({ title, count: n, open: initial = false, children }) {
  const [open, setOpen] = useState(initial);
  return (
    <details className="more" open={open} onToggle={(ev) => setOpen(ev.currentTarget.open)}>
      <summary>{title}{n != null && <span className="fold-count">{count(n)}</span>}</summary>
      {open && <div className="fold-body">{children}</div>}
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
  const beta = equatorialToEcliptic(sel.ra, sel.dec).beta;
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
        <h2 className={`title ${sel.label ? (sel.label.length > 18 ? 'title-long' : '') : 'title-coords'}`}>{sel.label || <>{formatRa(sel.ra)}<br />{formatDec(sel.dec)}</>}</h2>
        {sel.label && <p className="coords">{formatRa(sel.ra)}&ensp;{formatDec(sel.dec)}</p>}
      </header>

      <div className="panel-body">
        {candidate && <SurveyCandidateNote candidate={candidate} motion={motion} onRun={() => onRetry('motion')} />}
        <Summary
          motion={motion} results={results} known={known} simbad={simbad} gaia={gaia}
          onRun={() => onRetry('motion')} onRetry={onRetry} runShown={Boolean(candidate)}
        />
        <PatchStats motion={motion} results={results} simbad={simbad} gaia={gaia} visits={visits} />
        <Flagged
          results={results} known={known} filter={filter} gaia={gaia} {...pointing}
          player={{ playing, onTogglePlay, getYear, moversOnly, onToggleMovers }}
        />
        <CoverageSection visits={visits} beta={beta} onRetry={onRetry} />
        <More
          sel={sel} motion={motion} known={known} results={results} filter={filter} gaia={gaia}
          others={others} onToggleOthers={onToggleOthers} {...pointing}
        />
        <p className="credits">Data: NASA/SPHEREx DR1 + QR3 (IRSA) · ESA Gaia DR3 (VizieR) · SIMBAD (CDS) · 2MASS, AllWISE, CatWISE (IRSA)</p>
      </div>
    </aside>
  );
}
