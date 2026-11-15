import { useEffect, useMemo, useRef, useState } from 'react';
import { API_BASE } from '../lib/services.js';
import {
  CAUSES, PRECISION, RESULT_NAMES, SPECTRAL_KINDS, causeRows, confusionText, falsePositiveText, fetchResult,
  fetchResultsIndex, fetchSurveyStats, gaiaPoints, generatedText, injectionGrid, injectionText, lightCurve, precisionGain,
  rateText, spectralPoints, spectralText, stdDev, surveySummary,
} from '../lib/results.js';
import {
  BarList, GaiaScatter, Histogram, HourBars, InjectionHeatmap, KindSwatch, LightCurveChart, PrecisionStrip, SpectralScatter,
} from './ResultsCharts.jsx';
import { CloseIcon } from './ObjectCard.jsx';
import './Results.css';

// A count in words; a missing number (a result file without that field) shows as a dash.
const fmt = (x) => (Number.isFinite(x) ? x.toLocaleString('en-US') : '–');

/** The tool that writes each result file (docs/RESULTS.md), shown while it has not been run. */
const MAKES = {
  gaia_validation: 'uv run python tools/gaia_validation.py',
  false_movers: 'uv run python tools/false_mover_audit.py',
  brown_dwarfs: 'uv run python tools/brown_dwarf_search.py',
  light_curve_r_dra: 'uv run python tools/brown_dwarf_search.py --light-curve',
  injection_recovery: 'uv run python tools/injection_recovery.py',
};

/**
 * Every result file and the live survey numbers, fetched once when the page opens. Each entry is
 * { status: 'loading' | 'ok' | 'missing' | 'error' | 'none', doc?, info?, error? }.
 */
function useResults() {
  const [state, setState] = useState(() => ({ index: { status: API_BASE ? 'loading' : 'none' }, survey: { status: API_BASE ? 'loading' : 'none' }, files: {} }));
  useEffect(() => {
    if (!API_BASE) return undefined;
    const ctrl = new AbortController();
    const live = () => !ctrl.signal.aborted;
    const err = (e) => e?.message || String(e);
    const setFile = (name, s) => live() && setState((prev) => ({ ...prev, files: { ...prev.files, [name]: s } }));

    fetchSurveyStats({ signal: ctrl.signal }).then(
      (stats) => live() && setState((prev) => ({ ...prev, survey: stats ? { status: 'ok', stats } : { status: 'none' } })),
      (e) => live() && setState((prev) => ({ ...prev, survey: { status: 'error', error: err(e) } })),
    );
    fetchResultsIndex({ signal: ctrl.signal }).then((index) => {
      if (!live()) return;
      if (!index) { setState((prev) => ({ ...prev, index: { status: 'none' } })); return; }
      setState((prev) => ({
        ...prev,
        index: { status: 'ok', index },
        files: Object.fromEntries(RESULT_NAMES.map((n) => [n, index[n]?.available ? { status: 'loading', info: index[n] }
          : { status: index[n]?.error ? 'error' : 'missing', info: index[n], error: index[n]?.error }])),
      }));
      for (const name of RESULT_NAMES) {
        if (!index[name]?.available) continue;
        fetchResult(name, { signal: ctrl.signal }).then(
          (doc) => setFile(name, doc ? { status: 'ok', doc, info: index[name] } : { status: 'missing', info: index[name] }),
          (e) => setFile(name, { status: 'error', error: err(e), info: index[name] }),
        );
      }
    }, (e) => live() && setState((prev) => ({ ...prev, index: { status: 'error', error: err(e) } })));
    return () => ctrl.abort();
  }, []);
  return state;
}

/** What a section shows while its file is loading, not run yet, or broken; null once it can draw. */
function FileState({ name, file, index }) {
  if (index.status === 'none') return <p className="line dim">Needs the PlanetX server, which is not connected.</p>;
  if (index.status === 'error') return <p className="state err" role="alert">Could not ask the server for results: {index.error}.</p>;
  if (!file || file.status === 'loading') return <p className="state" role="status"><span className="ticker" aria-hidden="true" />Loading</p>;
  if (file.status === 'missing') {
    return (
      <p className="line dim rp-missing">
        Not run yet. <code>{MAKES[name]}</code> makes it.
      </p>
    );
  }
  if (file.status === 'error') {
    // The index reports a file it cannot parse (a tool is rewriting it, or it is broken); a failed fetch has its own reason.
    return file.info?.error
      ? <p className="state err" role="alert" title={file.info.error}>This result is being rewritten or is broken. Try again in a minute.</p>
      : <p className="state err" role="alert">This result could not be loaded: {file.error}.</p>;
  }
  return null;
}

/** When and how a result was made, and its caveats. */
function Provenance({ doc }) {
  const when = generatedText(doc.generated_at);
  return (
    <footer className="rp-prov">
      {when && <span>Made {when}</span>}
      {doc.command && <code>{doc.command}</code>}
      {(doc.notes || []).map((t) => <span key={t} className="rp-note">{t}</span>)}
    </footer>
  );
}

function Section({ id, kicker, title, lede, children }) {
  return (
    <section className="rp-section" aria-labelledby={id}>
      <header className="rp-section-head">
        <p className="kicker">{kicker}</p>
        <h2 id={id} className="rp-h2">{title}</h2>
        {lede && <p className="rp-lede">{lede}</p>}
      </header>
      {children}
    </section>
  );
}

function Figure({ title, caption, children, wide = false }) {
  return (
    <figure className={`rp-fig ${wide ? 'rp-fig-wide' : ''}`}>
      <h3 className="rp-h3">{title}</h3>
      {children}
      {caption && <figcaption className="rp-cap">{caption}</figcaption>}
    </figure>
  );
}

// ---------- sections ----------

function Precision() {
  const p = PRECISION;
  const gain = precisionGain(p);
  return (
    <section className="rp-hero" aria-labelledby="rp-precision">
      <p className="kicker">Barnard’s Star, the fastest star in our sky</p>
      <h2 id="rp-precision" className="rp-hero-number">
        <span className="measured">{Math.round(gain / 10) * 10}×</span> more precise
      </h2>
      <p className="rp-lede">
        SPHEREx alone, from three visits over one year, measures it moving <b>{p.spherex.pm} ± {p.spherex.err}″ a year</b>.
        Joined with where 2MASS, AllWISE and CatWISE saw it over the past 25 years, the same track gives
        {' '}<b>{p.fused.pm.toFixed(2)} ± {p.fused.err}″ a year</b>: an error {gain.toFixed(0)} times smaller.
        Gaia, the reference, says <b>{p.gaia.pm.toFixed(2)}″</b>.
      </p>
      <PrecisionStrip p={p} gain={gain} />
      <ul className="rp-keys" aria-label="Key">
        <li><i className="rp-key rp-key-wide" aria-hidden="true" />{p.spherex.label} ({p.spherex.detail})</li>
        <li><i className="rp-key rp-key-fused" aria-hidden="true" />{p.fused.label} ({p.fused.detail})</li>
        <li><i className="rp-key rp-key-gaia" aria-hidden="true" />{p.gaia.label} ({p.gaia.detail})</li>
      </ul>
    </section>
  );
}

function SurveyProgress({ survey }) {
  const s = useMemo(() => (survey.status === 'ok' ? surveySummary(survey.stats) : null), [survey]);
  let body;
  if (survey.status === 'loading') body = <p className="state" role="status"><span className="ticker" aria-hidden="true" />Loading</p>;
  else if (survey.status === 'none') body = <p className="line dim">Needs the PlanetX server, which is not connected.</p>;
  else if (survey.status === 'error') body = <p className="state err" role="alert">Could not read the survey: {survey.error}.</p>;
  else if (!s.available || s.total === 0) body = <p className="line dim">The survey has not started on this server yet.</p>;
  else {
    body = (
      <>
        <dl className="rp-tiles">
          <div>
            <dt>Sky patches searched</dt>
            <dd><span className="rp-big tabular">{fmt(s.done)}</span> <span className="dim">of {fmt(s.total)}</span></dd>
            <dd className="rp-meter" aria-hidden="true"><span style={{ width: `${Math.max(0.4, 100 * s.fraction)}%` }} /></dd>
            {(s.failed > 0 || s.inProgress > 0) && <dd className="rp-sub">{s.inProgress > 0 && `${fmt(s.inProgress)} in progress`}{s.inProgress > 0 && s.failed > 0 && ' · '}{s.failed > 0 && `${fmt(s.failed)} failed`}</dd>}
          </div>
          <div>
            <dt>Objects measured</dt>
            <dd><span className="rp-big tabular">{fmt(s.objects)}</span></dd>
            <dd className="rp-sub">tracked across SPHEREx visits</dd>
          </div>
          <div>
            <dt>Candidates flagged</dt>
            <dd><span className="rp-big tabular">{fmt(s.candidates)}</span></dd>
            <dd className="rp-sub">moving or changing brightness</dd>
          </div>
          <div>
            <dt>Speed of the search</dt>
            <dd><span className="rp-big tabular">{s.rate == null ? '–' : s.rate >= 10 ? Math.round(s.rate) : s.rate.toFixed(1)}</span> <span className="dim">an hour</span></dd>
            <dd className="rp-sub">{s.rate == null ? rateText(null) : `${fmt(s.doneLastDay)} patches in the last day`}</dd>
          </div>
        </dl>
        <div className="rp-grid">
          <Figure title="Patches finished per hour" caption="The last 24 hours, the current hour on the right.">
            <HourBars hours={s.hours} />
          </Figure>
          <Figure title="What the candidates are" caption="Checked against Gaia, SIMBAD and known asteroids. Every one is a candidate until confirmed.">
            {s.classes.length ? <BarList rows={s.classes} label="Candidates by class" /> : <p className="line dim">None yet.</p>}
          </Figure>
        </div>
      </>
    );
  }
  return (
    <Section id="rp-survey" kicker="Live · the automatic survey" title="How far the survey has got"
      lede="A worker searches the sky patch by patch, starting at the north ecliptic pole, where SPHEREx looks most often, and spiralling outward.">
      {body}
    </Section>
  );
}

function GaiaAgreement({ file, index }) {
  const doc = file?.status === 'ok' ? file.doc : null;
  const pts = useMemo(() => gaiaPoints(doc), [doc]);
  // Histograms use clean matches only (no blend or chance-match doubt), as summary.pull_std does.
  const resid = useMemo(() => pts.filter((p) => p.clean).flatMap((p) => p.resid), [pts]);
  const pulls = useMemo(() => pts.filter((p) => p.clean).flatMap((p) => p.pull), [pts]);
  const words = confusionText(doc?.summary?.confusion);
  const spread = doc?.summary?.pull_std ?? stdDev(pulls);
  const outside = (vals, lo, hi) => vals.filter((v) => v < lo || v >= hi).length;
  const c = words.counts;
  return (
    <Section id="rp-gaia" kicker="Check 1 · against the best star catalogue" title="Does SPHEREx agree with Gaia?"
      lede="Gaia measures star motions about a thousand times better than SPHEREx can, so for stars both see, Gaia is the answer key.">
      <FileState name="gaia_validation" file={file} index={index} />
      {doc && (
        <>
          <p className="rp-say">
            {words.still} {words.moving}
            {doc.summary && ` ${fmt(doc.summary.n_matched)} of ${fmt(doc.summary.n_objects)} objects in ${fmt(doc.fields?.length || 0)} fields were matched to Gaia.`}
          </p>
          <div className="rp-grid">
            <Figure title="Speed: SPHEREx against Gaia" wide
              caption="Each dot is a star. On the diagonal SPHEREx and Gaia agree. Still stars scatter upward because SPHEREx’s error, not motion, sets their measured speed.">
              <GaiaScatter points={pts} />
              <ul className="rp-keys" aria-label="Key">
                <li><i className="rp-key rp-key-on" aria-hidden="true" />We called it moving</li>
                <li><i className="rp-key rp-key-off" aria-hidden="true" />We called it still</li>
              </ul>
            </Figure>
            <Figure title="How wrong we were, in our own error bars"
              caption={`SPHEREx minus Gaia, divided by the error we quoted. If our errors are honest this matches the curve (spread 1). Ours: ${spread == null ? '–' : spread.toFixed(2)}.${outside(pulls, -5, 5) ? ` ${outside(pulls, -5, 5)} beyond ±5 not drawn.` : ''}`}>
              <Histogram values={pulls} lo={-5} hi={5} bins={20} ticks={[-4, -2, 0, 2, 4]} ideal xLabel="difference ÷ error"
                label={`Histogram of ${pulls.length} differences between SPHEREx and Gaia in units of the quoted error, with the ideal bell curve. Spread ${spread?.toFixed(2)}.`} />
            </Figure>
            <Figure title="How wrong we were, in arcseconds a year"
              caption={`SPHEREx minus Gaia, east and north motion together. Typical size ${doc.summary?.residual_median != null ? `${doc.summary.residual_median.toFixed(2)}″/yr` : '–'}.${outside(resid, -6, 6) ? ` ${outside(resid, -6, 6)} beyond ±6 not drawn.` : ''}`}>
              <Histogram values={resid} lo={-6} hi={6} bins={24} ticks={[-6, -3, 0, 3, 6]} xLabel="SPHEREx − Gaia, ″/yr"
                label={`Histogram of ${resid.length} differences between SPHEREx and Gaia motions in arcseconds per year.`} />
            </Figure>
            <Figure title="Right and wrong calls">
              <table className="rp-confusion">
                <thead><tr><th scope="col"><span className="sr-only">Gaia says</span></th><th scope="col">We said still</th><th scope="col">We said moving</th></tr></thead>
                <tbody>
                  <tr><th scope="row">Gaia: still</th><td className="tabular">{fmt(c.tn)}</td><td className={`tabular ${c.fp ? 'rp-wrong' : ''}`}>{fmt(c.fp)}<span className="rp-cell-note">false alarms</span></td></tr>
                  <tr><th scope="row">Gaia: moving</th><td className={`tabular ${c.fn ? 'rp-wrong' : ''}`}>{fmt(c.fn)}<span className="rp-cell-note">missed</span></td><td className="tabular">{fmt(c.tp)}</td></tr>
                </tbody>
              </table>
              <p className="rp-cap">Stars too slow for SPHEREx to judge either way are left out.</p>
            </Figure>
          </div>
          <Provenance doc={doc} />
        </>
      )}
    </Section>
  );
}

function FakeMotions({ file, index }) {
  const doc = file?.status === 'ok' ? file.doc : null;
  const rows = useMemo(() => causeRows(doc), [doc]);
  const s = doc?.summary;
  // Near-misses: stars Gaia says are still whose apparent motion is significant but under one
  // pixel, so only the engine's 1 px rule keeps them from being called moving.
  const near = doc?.near_summary;
  const nearRows = useMemo(() => (doc?.near_movers
    ? causeRows({ movers: doc.near_movers.filter((m) => m.truth === 'static'), summary: doc.near_summary })
    : []), [doc]);
  const before = doc?.baseline;
  return (
    <Section id="rp-fake" kicker="Check 2 · every false alarm, explained" title="Which motions are fake?"
      lede="Each object our engine called moving that Gaia, or a look at the pixels, shows is not. Knowing why lets the engine learn to throw them out.">
      <FileState name="false_movers" file={file} index={index} />
      {doc && (
        <>
          {s && (
            <p className="rp-say">
              {fmt(s.n_fake)} of {fmt(s.n_movers)} motions the engine flagged are not real{s.n_fake ? '. Here is why.' : '.'}
              {before?.summary && ` Before this round's engine fixes it flagged ${fmt(before.summary.n_movers)}; the ones removed were faint tracks joining unrelated specks.`}
            </p>
          )}
          {rows.length ? <BarList rows={rows} label="Fake motions by cause" /> : !s && <p className="line dim">No fake motions in this run.</p>}
          {near && nearRows.length > 0 && (
            <>
              <p className="rp-say">
                Just under the line: {fmt(near.n_fake)} stars Gaia says are still show a motion at least 5 times their
                error, but smaller than one SPHEREx pixel, so the engine does not call them moving. This is why
                we keep that rule{before?.near_summary ? ` (${fmt(before.near_summary.n_fake)} before this round's fixes)` : ''}.
              </p>
              <BarList rows={nearRows} label="Near-misses by cause" />
            </>
          )}
          {rows.some((r) => r.examples.length) && (
            <details className="more">
              <summary>Examples</summary>
              <dl className="kv rp-examples">
                {rows.filter((r) => r.examples.length).map((r) => (
                  <div key={r.cause} className="rp-example"><dt>{CAUSES[r.cause].label}</dt><dd>{r.examples.join(' ')}</dd></div>
                ))}
              </dl>
            </details>
          )}
          <Provenance doc={doc} />
        </>
      )}
    </Section>
  );
}

function Reach({ file, index }) {
  const doc = file?.status === 'ok' ? file.doc : null;
  return (
    <Section id="rp-reach" kicker="Check 3 · fake stars, planted on purpose" title="What can we find?"
      lede="We planted moving stars of known speed and brightness into real SPHEREx images and counted how many the engine found.">
      <FileState name="injection_recovery" file={file} index={index} />
      {doc && (
        <>
          {injectionText(doc) && <p className="rp-say">{injectionText(doc)} {falsePositiveText(doc.false_positives)}</p>}
          <div className="rp-grid">
            <Figure title="Share of planted movers found"
              caption="Brighter cells: more found. Bright stars at the top. Left of the solid line fewer than half are found; right of the dashed line nine in ten are.">
              <InjectionHeatmap doc={doc} />
              <ul className="rp-keys" aria-label="Key">
                <li><i className="rp-key rp-key-ramp" aria-hidden="true" />0% to 100% found</li>
                <li><i className="rp-key rp-key-l50" aria-hidden="true" />half found</li>
                <li><i className="rp-key rp-key-l90" aria-hidden="true" />nine in ten found</li>
              </ul>
            </Figure>
            <Figure title="The same, in numbers" caption="Share flagged as moving. Rows: brightness as a magnitude (bigger numbers are fainter stars). Columns: speed in arcseconds a year. The 0″ column is planted still stars: anything above 0% there is a false alarm.">
              <InjectionTable doc={doc} />
            </Figure>
          </div>
          <Provenance doc={doc} />
        </>
      )}
    </Section>
  );
}

function InjectionTable({ doc }) {
  const { speeds, mags, cells } = injectionGrid(doc);
  const at = new Map(cells.map((c) => [`${c.pm}|${c.mag}`, c]));
  return (
    // Focusable so a keyboard can scroll it sideways on a phone.
    <div className="rp-scroll" tabIndex={0} role="region" aria-label="Recovery table">
      <table className="rp-table">
        <caption className="sr-only">Percent of planted movers found, by brightness (rows, magnitude) and speed (columns, arcseconds a year)</caption>
        <thead>
          <tr><th scope="col">mag</th>{speeds.map((v) => <th key={v} scope="col">{v}″</th>)}</tr>
        </thead>
        <tbody>
          {mags.map((m) => (
            <tr key={m}>
              <th scope="row">{cells.find((c) => c.mag === m)?.label ?? m}</th>
              {speeds.map((v) => {
                const f = at.get(`${v}|${m}`)?.fraction ?? null;
                return <td key={v} className="tabular">{f == null || !Number.isFinite(f) ? '–' : `${Math.round(100 * f)}%`}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OnlySpherex({ bd, lc, index }) {
  const bdDoc = bd?.status === 'ok' ? bd.doc : null;
  const lcDoc = lc?.status === 'ok' ? lc.doc : null;
  const pts = useMemo(() => spectralPoints(bdDoc), [bdDoc]);
  const curve = useMemo(() => (lcDoc ? lightCurve(lcDoc) : null), [lcDoc]);
  const kinds = Object.keys(SPECTRAL_KINDS).filter((k) => pts.some((p) => p.kind === k));
  return (
    <Section id="rp-spherex" kicker="What other telescopes cannot do" title="Only SPHEREx can do this"
      lede="SPHEREx photographs every spot of sky in 102 colours of infrared light. That gives every star a spectrum, and a brightness in each of its visits.">
      <div className="rp-grid">
        <Figure title="Methane and water in every spectrum" wide
          caption={`The coolest brown dwarfs have methane and water in their air, which take bites out of their infrared light. Ordinary stars show neither.${spectralText(pts) ? ` ${spectralText(pts)}` : ''}`}>
          <FileState name="brown_dwarfs" file={bd} index={index} />
          {bdDoc && pts.length > 0 && (
            <>
              <SpectralScatter points={pts} />
              <ul className="rp-keys" aria-label="Key">
                {kinds.map((k) => <li key={k}><KindSwatch kind={k} />{SPECTRAL_KINDS[k].label}</li>)}
              </ul>
            </>
          )}
          {bdDoc && pts.length === 0 && <p className="line dim">No object had both dips measured.</p>}
        </Figure>
        <Figure title="R Draconis breathing in and out"
          caption={curve ? `A Mira variable: a giant star that swells and shrinks. SPHEREx saw it change ${curve.ratio ? `${curve.ratio.toFixed(1)}×` : ''} in brightness${curve.period ? `, repeating every ${Math.round(curve.period)} days${curve.literature ? ` (books say ${Math.round(curve.literature)})` : ''}` : ''}.` : 'A Mira variable: a giant star that swells and shrinks over about 245 days.'}>
          <FileState name="light_curve_r_dra" file={lc} index={index} />
          {curve && (
            <>
              <LightCurveChart curve={curve} />
              {curve.folded && <LightCurveChart curve={curve} folded />}
            </>
          )}
          {lcDoc && !curve && <p className="line dim">No brightness points in this run.</p>}
        </Figure>
      </div>
      {bdDoc && <Provenance doc={bdDoc} />}
      {lcDoc && <Provenance doc={lcDoc} />}
    </Section>
  );
}

/** The Results page: what the survey and the analyses found, over the whole app. */
export default function Results({ onClose }) {
  const { index, survey, files } = useResults();
  const closeRef = useRef(null);
  // Focus moves into the page, and back to whatever opened it when it closes. The opener is
  // read while rendering, before focus moves (an effect could run twice and read the close button).
  const [opener] = useState(() => document.activeElement);
  useEffect(() => {
    closeRef.current?.focus();
    return () => { if (opener instanceof HTMLElement && opener.isConnected) opener.focus(); };
  }, [opener]);
  return (
    <div className="rp" role="dialog" aria-modal="true" aria-labelledby="rp-title">
      <div className="rp-inner">
        <header className="rp-head">
          <div className="rp-head-row">
            <p className="kicker">PlanetX · SPHEREx</p>
            <button ref={closeRef} type="button" className="iconbtn" onClick={onClose} aria-label="Close results, back to the sky"><CloseIcon /></button>
          </div>
          <h1 id="rp-title" className="rp-title">What we found</h1>
          <p className="rp-lede">
            What the automatic survey has measured so far, and three checks of how far to trust it. Anything new it flags
            is a candidate, not a discovery, until other telescopes confirm it.
          </p>
        </header>
        <Precision />
        <SurveyProgress survey={survey} />
        <GaiaAgreement file={files.gaia_validation} index={index} />
        <FakeMotions file={files.false_movers} index={index} />
        <Reach file={files.injection_recovery} index={index} />
        <OnlySpherex bd={files.brown_dwarfs} lc={files.light_curve_r_dra} index={index} />
        <p className="rp-credits">
          Data: SPHEREx (NASA/IPAC IRSA), Gaia DR3 (ESA), 2MASS, AllWISE and CatWISE2020 (IRSA), SIMBAD (CDS).
        </p>
      </div>
    </div>
  );
}
