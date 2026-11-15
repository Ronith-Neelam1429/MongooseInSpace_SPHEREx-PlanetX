import { Component, Fragment, useMemo } from 'react';
import { decimalYear, formatDec, formatRa } from '../lib/coords.js';
import { FAST_ARCSEC_YR, displayName, identityOf } from '../lib/names.js';
import {
  archiveSentence, claim, compass, plainType, confidence, distanceText, measuredMotion, moonCrossing, motionsAgree, speed, verdict,
} from '../lib/plain.js';
import { knownClass } from '../lib/classes.js';
import { trackedAt } from '../lib/evidence.js';
import { EnginePictures, Exposures, Chart, Proof } from './MotionEvidence.jsx';
import { useChecks, useEvidence, useVerdict } from './useEvidence.js';
import StarTimelapse from './StarTimelapse.jsx';
import Spectrum from './Spectrum.jsx';
import { engineTarget, knownTarget } from '../lib/spectrum.js';

const n = (v, d = 2) => (v == null || !Number.isFinite(v) ? '–' : v.toFixed(d));
const signed = (v, d) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(d)}`;
const CATALOG = { gaia: 'Gaia', simbad: 'SIMBAD', skybot: 'SkyBoT' };
const OBSERVATORY = { '2mass': '2MASS', allwise: 'AllWISE', catwise: 'CatWISE', neowise: 'NEOWISE' };
// What a catalogued object is, in a few words, by its class (lib/classes.js).
const KNOWN_VERDICT = {
  fast: 'A fast-moving star',
  variable: 'Changes brightness',
  galaxy: 'Far away: it does not move',
  star: 'Stays put',
  other: 'Catalogued source',
};

export const CloseIcon = () => (
  <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M1 1l10 10M11 1L1 11" stroke="currentColor" strokeWidth="1.25" /></svg>
);

/** An arrow pointing the way a motion goes on the map (north up, east to the left). */
function Arrow({ pa }) {
  return (
    <svg className="arrow" width="11" height="11" viewBox="0 0 10 10" style={{ transform: `rotate(${-pa}deg)` }} aria-hidden="true">
      <path d="M5 9.5V1M1.5 4.5L5 1l3.5 3.5" fill="none" stroke="currentColor" strokeWidth="1.25" />
    </svg>
  );
}

/** The evidence views are another module's; if one throws, say so instead of taking the page down. */
class EvidenceBoundary extends Component {
  state = { failed: false };

  static getDerivedStateFromError() { return { failed: true }; }

  componentDidCatch(error) { console.error('Evidence view failed', error); }

  render() {
    return this.state.failed ? <p className="line dim">This part of the evidence could not be shown.</p> : this.props.children;
  }
}

/** The card's spectrum section, below the pictures: measured only when asked. */
function SpectrumSection({ target }) {
  return (
    <section className="card-spectrum" aria-labelledby="spectrum-title">
      <h3 id="spectrum-title">Spectrum</h3>
      <EvidenceBoundary><Spectrum target={target} /></EvidenceBoundary>
    </section>
  );
}

function Head({ tone, kicker, title, backLabel, onBack, onClose }) {
  return (
    <header className="panel-head card-head">
      <div className="panel-tools">
        {/* The list this card replaced is gone, so keyboard focus moves to the way back. */}
        <button type="button" className="textbtn back" autoFocus onClick={onBack}>
          <svg width="12" height="10" viewBox="0 0 12 10" aria-hidden="true"><path d="M12 5H1.5M5 1L1 5l4 4" fill="none" stroke="currentColor" strokeWidth="1.25" /></svg>
          {backLabel || 'Back to the patch'}
        </button>
        <span className="tools">
          <button type="button" className="iconbtn" onClick={onClose} aria-label="Close panel"><CloseIcon /></button>
        </span>
      </div>
      <p className="kicker object-kicker"><i className={`dot ${tone}`} />{kicker}</p>
      <h2 className={`title ${title.length > 18 ? 'title-long' : ''}`}>{title}</h2>
    </header>
  );
}

/** What the catalogues say about an engine object, next to what we measured. */
function CatalogueLine({ o, who, m }) {
  const moving = o.kind !== 'static';
  const best = who.matches[0];
  const listed = who.matches.find((x) => x.catalog === 'gaia' && x.pm_ra_cosdec != null && x.pm_dec != null)
    || who.matches.find((x) => x.pm_ra_cosdec != null && x.pm_dec != null);
  if (!best) return who.checked ? 'Not found in the SIMBAD or Gaia catalogues at this position.' : 'No catalogue was available to look it up.';
  if (!moving || !listed) return `Listed in ${CATALOG[best.catalog] || best.catalog} as ${best.name}${best.kind ? ` (${best.kind})` : ''}.`;
  const theirs = { east: listed.pm_ra_cosdec, north: listed.pm_dec };
  return (
    <>
      {CATALOG[listed.catalog] || listed.catalog} lists it moving <b className="signal">{speed(Math.hypot(theirs.east, theirs.north))}″/yr</b>;
      {' '}we measured <b className="measured">{speed(m.pm)}″/yr</b>.
      {' '}{motionsAgree(m, theirs) ? 'The two agree.' : 'The two disagree, so treat this motion with caution.'}
    </>
  );
}

function EngineCard({ o, who, field, backLabel, onBack, onClose }) {
  const m = measuredMotion(o);
  const moving = o.kind !== 'static';
  const ev = useEvidence(o, field);
  const said = useVerdict(o, ev);
  const checks = useChecks(o, ev);
  const name = displayName(who);
  const best = who.matches[0];
  const far = who.matches.find((x) => x.distance_ly != null);
  const about = [
    best?.kind,
    far && distanceText(far.distance_ly),
    best?.magnitude != null && `magnitude ${best.magnitude.toFixed(1)}`,
  ].filter(Boolean).join(' · ');
  const a = o.archive;
  const others = a.observatories.filter((x) => x !== 'spherex').map((x) => OBSERVATORY[x] || x);
  const crossing = moving ? moonCrossing(m.pm) : null;
  const spectrumTarget = useMemo(() => engineTarget(o), [o]);
  return (
    <>
      <Head tone="measured-bg" kicker={`Measured in SPHEREx images · ${verdict(o)}`} title={name} backLabel={backLabel} onBack={onBack} onClose={onClose} />
      <div className="panel-body">
        <section className="card-pictures ev" aria-label="Telescope pictures">
          <EvidenceBoundary><EnginePictures object={o} ev={ev} title={name} /></EvidenceBoundary>
        </section>
        <SpectrumSection target={spectrumTarget} />

        <section className="card-verdict">
          <p className={`verdict-line ${said?.tone === 'moved' ? 'measured' : ''}`}>{said ? said.headline : verdict(o)}</p>
          {moving && (
            <p className="lede">
              Moves <b className="measured">{speed(m.pm)}″</b> per year toward <b>{compass(m.pa)}</b> <Arrow pa={m.pa} />
              {' '}<span className="dim">· {confidence(m.sigma).toLowerCase()} ({m.sigma.toFixed(1)}× its error{m.fused ? ', with older surveys' : ''})</span>
            </p>
          )}
          {o.is_variable && o.variability && (
            <p className="lede">
              {moving ? 'Its brightness also' : 'Its brightness'} changed by a factor of <b className="measured">{o.variability.ratio.toFixed(2)}</b> between SPHEREx passes.
            </p>
          )}
          <p className="line card-cat"><CatalogueLine o={o} who={who} m={m} /></p>
          {o.kind === 'candidate' && <p className="line dim">A candidate, not a discovery: it stays one until other data confirm it.</p>}
        </section>

        {checks && <section className="ev"><EvidenceBoundary><Proof checks={checks} /></EvidenceBoundary></section>}
        {ev.status === 'ok' && (
          <section className="ev" aria-labelledby="chart-title">
            <h3 id="chart-title">Position over time</h3>
            <EvidenceBoundary><Chart object={o} evidence={ev.evidence} /></EvidenceBoundary>
          </section>
        )}
        {ev.status === 'ok' && ev.evidence.passes.some((p) => p.exposures?.length) && (
          <details className="more ev">
            <summary>Single exposures</summary>
            <EvidenceBoundary><Exposures evidence={ev.evidence} /></EvidenceBoundary>
          </details>
        )}

        <details className="more">
          <summary>Details</summary>
          {said && <div className="card-said">{said.lines.map((line) => <p key={line} className="line">{line}</p>)}</div>}
          <dl className="facts">
            {moving && crossing && <><dt>Scale</dt><dd>At that rate it crosses the width of the full Moon in {crossing}.</dd></>}
            {moving && <><dt>Older surveys</dt><dd>{archiveSentence(a)}</dd></>}
            {o.variability?.caution && <><dt>Caution</dt><dd>{o.variability.caution}</dd></>}
            {about && <><dt>About it</dt><dd>{about}</dd></>}
          </dl>
          <dl className="kv">
            {/* A static object's fit is a slope through its positions, which the engine did not accept as motion. */}
            <dt>{moving ? 'Motion' : 'Fitted slope'}</dt>
            <dd>
              {n(o.pm_total)} ± {n(o.pm_err)} ″/yr in SPHEREx alone ({n(o.significance, 1)}σ)
              {!moving && ', not accepted as motion'}
            </dd>
            <dt>East, north</dt><dd>{signed(o.pm_ra_cosdec, 2)}, {signed(o.pm_dec, 2)} ″/yr · {n(o.position_angle_deg, 0)}° east of north</dd>
            {others.length > 0 && a.pm_ra_cosdec != null && a.pm_err != null && (
              <>
                <dt>With older surveys</dt>
                <dd>{signed(a.pm_ra_cosdec, 3)}, {signed(a.pm_dec, 3)} ± {n(a.pm_err, 3)} ″/yr over {n(a.baseline_yr, 1)} yr ({others.join(', ')})</dd>
              </>
            )}
            {o.parallax_arcsec != null && <><dt>Parallax</dt><dd>{n(o.parallax_arcsec)} ± {n(o.parallax_err_arcsec)} ″</dd></>}
            {o.variability && <><dt>Brightness</dt><dd>{o.variability.band} {o.variability.change} · {n(o.variability.significance, 1)}σ</dd></>}
            <dt>Seen in</dt><dd>{o.track.map((t) => t.date).join(' · ')}</dd>
            <dt>Position</dt><dd>{formatRa(o.ra)} {formatDec(o.dec)}</dd>
            {who.matches.map((x) => (
              <Fragment key={`${x.catalog}:${x.name}`}>
                <dt>{CATALOG[x.catalog] || x.catalog}</dt>
                <dd>{x.name} · {n(x.sep_arcsec, 1)}″ away</dd>
              </Fragment>
            ))}
            {o.saturated && <><dt>Note</dt><dd>Too bright for the detector: the position comes from its masked core.</dd></>}
          </dl>
        </details>
      </div>
    </>
  );
}

function KnownCard({ row, measured, tracked, onPick, onBack, onClose }) {
  const { simbad: o, gaia: s } = row;
  const cls = knownClass(row);
  const fast = cls === 'fast';
  const source = [o && 'SIMBAD', s && 'Gaia'].filter(Boolean).join(' and ');
  const about = [
    plainType(row.type),
    row.distanceLy != null && distanceText(row.distanceLy),
    row.mag != null && `magnitude ${row.mag.toFixed(1)}`,
  ].filter(Boolean).join(' · ');
  const result = measured?.o || tracked;
  const spectrumTarget = useMemo(() => knownTarget(row, decimalYear()), [row]);
  return (
    <>
      <Head tone={fast ? 'signal-bg' : 'plain-bg'} kicker={`In the ${source} catalogue${o && s ? 's' : ''}`} title={row.name} onBack={onBack} onClose={onClose} />
      <div className="panel-body">
        <section className="card-pictures" aria-label="Telescope pictures">
          <EvidenceBoundary><StarTimelapse row={row} /></EvidenceBoundary>
        </section>
        <SpectrumSection target={spectrumTarget} />
        <section className="card-verdict">
          <p className={`verdict-line ${fast ? 'signal' : ''}`}>{KNOWN_VERDICT[cls]}</p>
          {row.pm != null && row.pm >= FAST_ARCSEC_YR / 10 ? (
            <p className="lede">
              The catalogue lists it moving <b className={fast ? 'signal' : ''}>{speed(row.pm)}″</b> per year toward <b>{compass(row.pa)}</b> <Arrow pa={row.pa} />.
              {fast && <span className="dim"> It crosses the width of the full Moon in {moonCrossing(row.pm)}.</span>}
            </p>
          ) : <p className="lede">{row.pm != null ? 'The catalogue lists almost no motion for it.' : 'The catalogue lists no motion for it.'}</p>}
          {about && <p className="line dim">{about}</p>}
          {result && (
            <p className="line card-cat">
              Our SPHEREx measurement: {claim(result).toLowerCase()}.{' '}
              <button type="button" className="textbtn" onClick={() => onPick({ kind: 'engine', obj: result })}>See the measurement</button>
            </p>
          )}
        </section>
        <details className="more">
          <summary>Details</summary>
          <dl className="kv">
            {o && <><dt>SIMBAD</dt><dd>{o.name}</dd></>}
            {s && <><dt>Gaia DR3</dt><dd>{s.id}</dd></>}
            <dt>Position today</dt><dd>{formatRa(row.ra)} {formatDec(row.dec)}</dd>
            {s?.pmra != null && s.pmdec != null && <><dt>East, north</dt><dd>{signed(s.pmra, 1)}, {signed(s.pmdec, 1)} mas/yr (Gaia)</dd></>}
            {s?.plx != null && <><dt>Parallax</dt><dd>{n(s.plx)}{s.plxErr != null ? ` ± ${n(s.plxErr)}` : ''} mas</dd></>}
            {row.mag != null && <><dt>Brightness</dt><dd>magnitude {row.mag.toFixed(1)} ({row.band} band; lower is brighter)</dd></>}
            {row.also.length > 0 && <><dt>Also here</dt><dd>{row.also.map((x) => (x.type ? `${x.name} (${x.type})` : x.name)).join(' · ')}</dd></>}
          </dl>
        </details>
      </div>
    </>
  );
}

/** The card for one selected object: an engine result (with its evidence) or a catalogued object. */
export default function ObjectCard({ focus, results, others, objects, field, lists, backLabel, onPick, onBack, onClose }) {
  if (focus.kind === 'engine') {
    const who = identityOf(focus.obj, [...results, ...(others || [])], lists);
    return <EngineCard o={focus.obj} who={who} field={field} backLabel={backLabel} onBack={onBack} onClose={onClose} />;
  }
  const row = focus.obj;
  const names = new Set([row.name, row.gaia && `Gaia DR3 ${row.gaia.id}`]);
  const measured = results.find(({ who }) => who.matches.some((x) => names.has(x.name)));
  // After a run, the tracked object at this star's position, whatever the engine made of it.
  const tracked = objects ? trackedAt(objects, row.ra, row.dec) : null;
  return <KnownCard row={row} measured={measured} tracked={tracked} onPick={onPick} onBack={onBack} onClose={onClose} />;
}
