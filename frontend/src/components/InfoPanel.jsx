import { useEffect, useMemo, useRef, useState } from 'react';
import { equatorialToEcliptic, equatorialToGalactic, formatDec, formatFov, formatRa } from '../lib/coords.js';
import { FAST_ARCSEC_YR, displayName, sameObject } from '../lib/names.js';
import { dateSpan, monthYear, shortClaim, shortName, speed, verdict } from '../lib/plain.js';
import { CLASS, engineClass, knownClass } from '../lib/classes.js';
import ObjectCard, { CloseIcon } from './ObjectCard.jsx';
import { ClassMark } from './ClassFilter.jsx';

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

/** The motion engine: a run button, progress, then what moves or changes brightness, by name. */
function Results({ motion, results: all, filter, others, visits, hover, resume, onPick, onPoint, onRun, onRetryVisits, onToggleOthers }) {
  const r = motion.result;
  const results = all.filter(({ o }) => filter[engineClass(o)]);
  const hidden = all.length - results.length;
  return (
    <section className="results">
      <h3>
        <span><i className="dot measured-bg" />What moves here</span>
        {motion.status === 'ok' && <span className="count">{all.length} found</span>}
      </h3>
      {motion.status === 'none' && <p className="line dim">Measuring motion needs the PlanetX server, which is not connected.</p>}
      {motion.status === 'idle' && (
        <>
          <p className="line">Compare SPHEREx images taken months apart and list what moved or changed brightness.</p>
          {visits.status === 'ok' && visits.count > 0 && (
            <p className="line dim">
              SPHEREx has imaged this spot {plural(visits.count, 'time')} in {plural(visits.passes.length, 'pass', 'passes')} since {monthYear(visits.passes[0].start)}.
            </p>
          )}
          {visits.status === 'ok' && visits.count === 0 && <p className="line dim">SPHEREx has no images of this spot yet.</p>}
          {visits.status === 'error' && (
            <p className="line dim">
              Could not check how often SPHEREx imaged this spot.{' '}
              <button type="button" className="textbtn" onClick={onRetryVisits}>Retry</button>
            </p>
          )}
          <button type="button" className="btn" onClick={onRun}>Find what moves</button>
          <p className="line dim">Uses about 50 images. A minute or two the first time, seconds after that.</p>
        </>
      )}
      {motion.status === 'loading' && <Progress job={motion.job} />}
      {motion.status === 'error' && (
        <p className="state err" role="alert">
          The run failed: {motion.error}.{' '}
          <button type="button" className="textbtn" onClick={onRun}>Try again</button>
        </p>
      )}
      {motion.status === 'ok' && (
        <>
          <p className="line">
            Compared {plural(r.n_frames, 'image')} from {plural(r.passes.length, 'pass', 'passes')}
            {r.passes.length > 0 && ` (${dateSpan(r.passes[0].start, r.passes[r.passes.length - 1].end)})`}
            {' '}and followed {plural(r.objects.length, 'object')}.
          </p>
          {all.length === 0 && <p className="line dim">Nothing here moved or changed brightness by more than the measurement error.</p>}
          {results.length === 0
            ? null
            : (
              <ul className="rowlist">
                {results.map(({ o, who }) => {
                  const f = { kind: 'engine', obj: o };
                  const name = displayName(who);
                  return (
                    <Row key={o.id} className="result" lit={sameObject(hover, f)} resume={sameObject(resume, f)} onPick={() => onPick(f)} onPoint={(p) => onPoint(p ? f : null)}>
                      <span className="row-name" title={name}><ClassMark cls={CLASS[engineClass(o)]} size={10} />{shortName(name)}</span>
                      <span className="badge">{verdict(o)}</span>
                      <span className="row-sub">{shortClaim(o)}</span>
                    </Row>
                  );
                })}
              </ul>
            )}
          {hidden > 0 && <p className="line dim">{plural(hidden, 'more result')} hidden by the map filter.</p>}
          {r.objects.length > results.length && (
            <p className="line">
              <button type="button" className="textbtn" aria-expanded={Boolean(others)} onClick={onToggleOthers}>
                {others ? 'Hide the other tracked objects' : `Show all ${count(r.objects.length)} tracked objects`}
              </button>
            </p>
          )}
          {others && (
            <>
              <p className="line dim">Not moving or changing beyond the error. Pick one to see its images in every pass.</p>
              <ul className="rowlist">
                {others.map(({ o, who }) => {
                  const f = { kind: 'engine', obj: o };
                  const name = displayName(who);
                  return (
                    <Row key={o.id} className="result" lit={sameObject(hover, f)} resume={sameObject(resume, f)} onPick={() => onPick(f)} onPoint={(p) => onPoint(p ? f : null)}>
                      <span className="row-name" title={name}>{shortName(name)}</span>
                      <span className="badge">{verdict(o)}</span>
                      <span className="row-sub">Seen in {plural(o.track.length, 'pass', 'passes')}</span>
                    </Row>
                  );
                })}
              </ul>
            </>
          )}
          {r.notes.map((note) => <p key={note} className="line dim">{note}</p>)}
        </>
      )}
    </section>
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

/** SIMBAD and Gaia as one list of already-known objects, with the control that plays catalogued motion. */
function Known({ known, filter, simbad, gaia, hover, resume, onPick, onPoint, onRetry, playing, onTogglePlay, getYear, moversOnly, onToggleMovers }) {
  const [all, setAll] = useState(false);
  const shown = useMemo(
    () => known.rows.map((row) => ({ row, cls: knownClass(row) })).filter((x) => filter[x.cls]),
    [known, filter],
  );
  const hidden = known.rows.length - shown.length;
  const few = shown.length <= KNOWN_SHOWN + 2; // not worth folding
  const rows = all || few ? shown : shown.slice(0, KNOWN_SHOWN);
  const fast = known.rows.filter((r) => r.pm >= FAST_ARCSEC_YR).length;
  const loaded = simbad.status === 'ok' && gaia.status === 'ok';
  return (
    <section>
      <h3>
        <span>Already known here</span>
        {known.rows.length > 0 && <span className="count">{count(known.rows.length)}</span>}
      </h3>
      <State state={simbad} what="named objects (SIMBAD)" onRetry={() => onRetry('simbad')} />
      <State state={gaia} what="stars (Gaia)" onRetry={() => onRetry('gaia')} />
      {gaia.status === 'ok' && gaia.years && (
        <div className="player">
          <button type="button" className="iconbtn" onClick={onTogglePlay} aria-label={playing ? 'Stop the star motion' : 'Play the star motion'}>
            {playing
              ? <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M0 0h10v10H0z" fill="currentColor" /></svg>
              : <svg width="10" height="12" viewBox="0 0 10 12" aria-hidden="true"><path d="M0 0l10 6-10 6z" fill="currentColor" /></svg>}
          </button>
          <span className="player-text">
            {playing ? <Clock getYear={getYear} /> : `Play ${count(gaia.years)} years of motion`}
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
      )}
      {rows.length > 0 && (
        <ul className="rowlist">
          {rows.map(({ row, cls }) => {
            const f = { kind: 'known', obj: row };
            const isFast = cls === 'fast';
            return (
              <Row key={row.key} className="known" lit={sameObject(hover, f)} resume={sameObject(resume, f)} onPick={() => onPick(f)} onPoint={(p) => onPoint(p ? f : null)}>
                <span className="row-name" title={`${row.name} · ${CLASS[cls].label}`}><ClassMark cls={CLASS[cls]} size={10} />{shortName(row.name)}</span>
                <span className="row-type" title={row.type}>{row.type}</span>
                <span className={`row-pm ${isFast ? 'signal' : 'dim'}`}>{isFast ? `${speed(row.pm)}″/yr` : ''}</span>
              </Row>
            );
          })}
        </ul>
      )}
      {!few && (
        <p className="line">
          <button type="button" className="textbtn" aria-expanded={all} onClick={() => setAll(!all)}>
            {all ? 'Show fewer' : `Show all ${count(shown.length)}`}
          </button>
        </p>
      )}
      {hidden > 0 && <p className="line dim">{plural(hidden, 'more object')} hidden by the map filter.</p>}
      {loaded && known.rows.length === 0 && <p className="line dim">Nothing here has a catalogue name.</p>}
      {loaded && fast === 0 && known.rows.length > 0 && (
        <p className="line dim">No catalogued star here moves faster than {FAST_ARCSEC_YR}″ per year.</p>
      )}
      {gaia.status === 'ok' && (
        <p className="line dim">
          The dots on the map are {count(gaia.stars.length)}{gaia.capped ? '+' : ''} stars from the Gaia catalogue. Click any of them.
        </p>
      )}
    </section>
  );
}

/** What most visitors do not need: coordinates in three systems, SPHEREx pass dates, data credits. */
function FieldDetails({ sel, visits }) {
  const { g, e } = useMemo(() => ({
    g: equatorialToGalactic(sel.ra, sel.dec),
    e: equatorialToEcliptic(sel.ra, sel.dec),
  }), [sel.ra, sel.dec]);
  return (
    <details className="more">
      <summary>Details</summary>
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
    </details>
  );
}

/**
 * The side panel for the selected patch of sky. It lists the engine's results first, then the
 * known objects; picking any object swaps the list for that object's card, with a way back.
 */
export default function InfoPanel({
  sel, simbad, gaia, visits, motion, known, results, others, onToggleOthers, focus, hover, resume, onPick, onPoint, onBack, onClose, onRetry,
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
          onPick={onPick}
          onBack={onBack}
          onClose={onClose}
        />
      </aside>
    );
  }
  const width = sel.radius * 2;
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
        <Results
          motion={motion}
          results={results}
          filter={filter}
          others={others}
          onToggleOthers={onToggleOthers}
          visits={visits}
          hover={hover}
          resume={resume}
          onPick={onPick}
          onPoint={onPoint}
          onRun={() => onRetry('motion')}
          onRetryVisits={() => onRetry('visits')}
        />
        <Known
          known={known}
          filter={filter}
          simbad={simbad}
          gaia={gaia}
          hover={hover}
          resume={resume}
          onPick={onPick}
          onPoint={onPoint}
          onRetry={onRetry}
          playing={playing}
          onTogglePlay={onTogglePlay}
          getYear={getYear}
          moversOnly={moversOnly}
          onToggleMovers={onToggleMovers}
        />
        <FieldDetails sel={sel} visits={visits} />
      </div>
    </aside>
  );
}
