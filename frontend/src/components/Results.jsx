import { useEffect, useMemo, useRef, useState } from 'react';
import { API_BASE } from '../lib/services.js';
import {
  CAUSES, PRECISION, RESULT_NAMES, SPECTRAL_KINDS, causeRows, fetchResult, fetchResultsIndex, fetchSurveyStats,
  gaiaPoints, generatedText, injectionGrid, lightCurve, precisionGain, spectralPoints, stdDev, surveySummary,
} from '../lib/results.js';
import {
  BarList, GaiaScatter, Histogram, HourBars, InjectionHeatmap, KindSwatch, LightCurveChart, PrecisionStrip, SpectralScatter,
} from './ResultsCharts.jsx';
import { CloseIcon } from './ObjectCard.jsx';
import './Results.css';

// A count in words; a missing number (a result file without that field) shows as a dash.
const fmt = (x) => (Number.isFinite(x) ? x.toLocaleString('en-US') : '–');
const pctOf = (k, n) => (n > 0 ? `${(100 * k / n) < 1 && k > 0 ? (100 * k / n).toFixed(1) : Math.round(100 * k / n)}%` : '–');
const r1 = (v) => (Number.isFinite(v) ? String(+v.toFixed(1)) : '–');

/** "5 min ago", "3 h ago", "2 days ago" for the survey's last finished patch. */
function ago(date, now = Date.now()) {
  if (!date) return null;
  const min = Math.max(0, Math.round((now - date.getTime()) / 6e4));
  if (min < 1) return 'just now';
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}

/** The tool that writes each result file (docs/RESULTS.md), shown while it has not been run. */
const MAKES = {
  gaia_validation: 'uv run python tools/gaia_validation.py',
  false_movers: 'uv run python tools/false_mover_audit.py',
  brown_dwarfs: 'uv run python tools/brown_dwarf_search.py',
  light_curve_r_dra: 'uv run python tools/brown_dwarf_search.py --light-curve',
  injection_recovery: 'uv run python tools/injection_recovery.py',
};

/** The dashboard's sections, in order, for the header's jump links. */
const SECTIONS = [
  ['rp-survey', 'Crawler'],
  ['rp-gaia', 'Accuracy'],
  ['rp-fake', 'False alarms'],
  ['rp-reach', 'Limits'],
  ['rp-spherex', 'Spectra'],
];

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
  if (index.status === 'none') return <p className="rp-empty">Needs the AstroSearchX server, which is not connected.</p>;
  if (index.status === 'error') return <p className="rp-empty err" role="alert">Could not ask the server for results: {index.error}.</p>;
  if (!file || file.status === 'loading') return <p className="rp-empty" role="status"><span className="ticker" aria-hidden="true" />Loading</p>;
  if (file.status === 'missing') return <p className="rp-empty">Not run yet. <code>{MAKES[name]}</code> makes it.</p>;
  if (file.status === 'error') {
    // The index reports a file it cannot parse (a tool is rewriting it, or it is broken); a failed fetch has its own reason.
    return file.info?.error
      ? <p className="rp-empty err" role="alert" title={file.info.error}>This result is being rewritten or is broken. Try again in a minute.</p>
      : <p className="rp-empty err" role="alert">This result could not be loaded: {file.error}.</p>;
  }
  return null;
}

/** When and how a result was made, and its caveats, folded away: the dashboard shows the numbers. */
function Provenance({ docs }) {
  const shown = docs.filter(Boolean);
  if (!shown.length) return null;
  return (
    <details className="rp-prov">
      <summary>How this was made</summary>
      {shown.map((doc) => (
        <div key={doc.command || doc.generated_at} className="rp-prov-body">
          {generatedText(doc.generated_at) && <span>Made {generatedText(doc.generated_at)}</span>}
          {doc.command && <code>{doc.command}</code>}
          {(doc.notes || []).map((t) => <p key={t}>{t}</p>)}
        </div>
      ))}
    </details>
  );
}

// ---------- building blocks ----------

/** One number with a plain label. tone: 'good' (green tick), 'warn', or none. */
function Kpi({ label, value, unit, help, tone, meter }) {
  return (
    <div className={`rp-kpi ${tone ? `rp-kpi-${tone}` : ''}`}>
      <dt>{label}</dt>
      <dd className="rp-kpi-value"><span className="tabular">{value}</span>{unit && <span className="rp-kpi-unit"> {unit}</span>}</dd>
      {meter != null && <dd className="rp-meter" aria-hidden="true"><span style={{ width: `${Math.min(100, Math.max(0.6, 100 * meter))}%` }} /></dd>}
      {help && <dd className="rp-kpi-help">{help}</dd>}
    </div>
  );
}

function KpiRow({ children, label }) {
  return <dl className="rp-kpis" aria-label={label}>{children}</dl>;
}

/** A dashboard card: a title, one plain sentence, then the chart. span is out of 6 columns. */
function Panel({ title, desc, span = 3, children }) {
  return (
    <figure className={`rp-panel rp-span-${span}`}>
      <figcaption className="rp-panel-head">
        <h3 className="rp-h3">{title}</h3>
        {desc && <p className="rp-desc">{desc}</p>}
      </figcaption>
      <div className="rp-panel-body">{children}</div>
    </figure>
  );
}

function Section({ id, title, desc, badge, children }) {
  return (
    <section id={id} className="rp-section" aria-labelledby={`${id}-h`}>
      <header className="rp-section-head">
        <h2 id={`${id}-h`} className="rp-h2">{title}</h2>
        {badge}
        {desc && <p className="rp-section-desc">{desc}</p>}
      </header>
      {children}
    </section>
  );
}

function Badge({ tone = 'neutral', children }) {
  return <span className={`rp-badge rp-badge-${tone}`}>{children}</span>;
}

// ---------- sections ----------

function Crawler({ survey }) {
  const s = useMemo(() => (survey.status === 'ok' ? surveySummary(survey.stats) : null), [survey]);
  let body;
  if (survey.status === 'loading') body = <p className="rp-empty" role="status"><span className="ticker" aria-hidden="true" />Loading</p>;
  else if (survey.status === 'none') body = <p className="rp-empty">Needs the AstroSearchX server, which is not connected.</p>;
  else if (survey.status === 'error') body = <p className="rp-empty err" role="alert">Could not read the crawler: {survey.error}.</p>;
  else if (!s.available || s.total === 0) body = <p className="rp-empty">The crawler has not started on this server yet.</p>;
  else {
    const extra = [s.inProgress > 0 && `${fmt(s.inProgress)} running now`, s.failed > 0 && `${fmt(s.failed)} failed`].filter(Boolean).join(' · ');
    body = (
      <>
        <KpiRow label="Crawler numbers">
          <Kpi label="Sky squares done" value={fmt(s.done)} unit={`/ ${fmt(s.total)}`} meter={s.fraction}
            help={extra || `${pctOf(s.done, s.total)} of the sky`} />
          <Kpi label="Objects seen" value={fmt(s.objects)} help="Dots found in the pictures" />
          <Kpi label="Worth a look" value={fmt(s.candidates)} help="Might be moving or flickering" tone={s.candidates ? 'accent' : undefined} />
          <Kpi label="Speed" value={s.rate == null ? '–' : s.rate >= 10 ? Math.round(s.rate) : s.rate.toFixed(1)} unit="squares / hour"
            help={`${fmt(s.doneLastDay)} in the last day`} />
        </KpiRow>
        <div className="rp-grid">
          <Panel title="Squares done per hour" desc="Last 24 hours. Right edge is now." span={4}>
            <HourBars hours={s.hours} />
          </Panel>
          <Panel title="What it flagged" desc="Everything is a candidate until checked." span={2}>
            {s.classes.length ? <BarList rows={s.classes} label="Candidates by type" /> : <p className="rp-empty">Nothing yet.</p>}
          </Panel>
        </div>
      </>
    );
  }
  const last = s?.lastFinished ? ago(s.lastFinished) : null;
  return (
    <Section id="rp-survey" title="Survey crawler"
      desc="A robot checks the sky one small square at a time, starting where SPHEREx takes the most pictures."
      badge={s?.available && <Badge tone="live"><i className="rp-pulse" aria-hidden="true" />Live{last && ` · last square ${last}`}</Badge>}>
      {body}
    </Section>
  );
}

function Headline() {
  const p = PRECISION;
  const gain = precisionGain(p);
  return (
    <div className="rp-grid rp-headline">
      <Panel title="Barnard’s Star speed" span={6}
        desc="SPHEREx alone is close. Add 25 years of older telescope pictures and it lands right on Gaia’s answer.">
        <div className="rp-headline-row">
          <KpiRow label="Barnard's Star">
            <Kpi label="SPHEREx alone" value={`${p.spherex.pm} ± ${p.spherex.err}`} unit="″/yr" help={p.spherex.detail} />
            <Kpi label="With older surveys" value={`${p.fused.pm.toFixed(2)} ± ${p.fused.err}`} unit="″/yr" help="2MASS, AllWISE, CatWISE" />
            <Kpi label="Gaia (the answer)" value={p.gaia.pm.toFixed(2)} unit="″/yr" help="Best star catalogue" />
            <Kpi label="Sharper" value={`${Math.round(gain / 10) * 10}×`} help="Smaller error" tone="good" />
          </KpiRow>
          <div className="rp-headline-chart">
            <PrecisionStrip p={p} gain={gain} />
            <ul className="rp-keys" aria-label="Key">
              <li><i className="rp-key rp-key-wide" aria-hidden="true" />{p.spherex.label}</li>
              <li><i className="rp-key rp-key-fused" aria-hidden="true" />{p.fused.label}</li>
              <li><i className="rp-key rp-key-gaia" aria-hidden="true" />{p.gaia.label}</li>
            </ul>
          </div>
        </div>
      </Panel>
    </div>
  );
}

function GaiaAgreement({ file, index }) {
  const doc = file?.status === 'ok' ? file.doc : null;
  const pts = useMemo(() => gaiaPoints(doc), [doc]);
  // Histograms use clean matches only (no blend or chance-match doubt), as summary.pull_std does.
  const resid = useMemo(() => pts.filter((p) => p.clean).flatMap((p) => p.resid), [pts]);
  const pulls = useMemo(() => pts.filter((p) => p.clean).flatMap((p) => p.pull), [pts]);
  const spread = doc?.summary?.pull_std ?? stdDev(pulls);
  const outside = (vals, lo, hi) => vals.filter((v) => v < lo || v >= hi).length;
  const { tp = 0, fp = 0, fn = 0, tn = 0 } = doc?.summary?.confusion || {};
  return (
    <Section id="rp-gaia" title="Accuracy: SPHEREx vs Gaia"
      desc="Gaia is the best star catalogue there is. We use it as the answer key."
      badge={doc && <Badge tone={fp ? 'warn' : 'good'}>{fp ? `${fmt(fp)} false alarms` : 'No false alarms'}</Badge>}>
      <FileState name="gaia_validation" file={file} index={index} />
      {doc && (
        <>
          <KpiRow label="Accuracy numbers">
            <Kpi label="Still stars called moving" value={fmt(fp)} unit={`/ ${fmt(fp + tn)}`} help="Lower is better" tone={fp ? 'warn' : 'good'} />
            <Kpi label="Moving stars caught" value={fmt(tp)} unit={`/ ${fmt(tp + fn)}`} help="Fast enough for SPHEREx to see" />
            <Kpi label="Typical error" value={doc.summary?.residual_median != null ? doc.summary.residual_median.toFixed(2) : '–'} unit="″/yr"
              help="How far off our speeds are" />
            <Kpi label="Error bars honest?" value={spread == null ? '–' : spread.toFixed(2)} help="1.00 is perfect"
              tone={spread != null && Math.abs(spread - 1) < 0.2 ? 'good' : undefined} />
          </KpiRow>
          <div className="rp-grid">
            <Panel title="Our speed vs Gaia’s" desc="Each dot is a star. On the line means we agree." span={4}>
              <GaiaScatter points={pts} />
              <ul className="rp-keys" aria-label="Key">
                <li><i className="rp-key rp-key-on" aria-hidden="true" />We said moving</li>
                <li><i className="rp-key rp-key-off" aria-hidden="true" />We said still</li>
              </ul>
            </Panel>
            <Panel title="Right and wrong calls" desc="Rows are Gaia’s answer, columns are ours." span={2}>
              <table className="rp-confusion">
                <thead><tr><th scope="col"><span className="sr-only">Gaia says</span></th><th scope="col">We: still</th><th scope="col">We: moving</th></tr></thead>
                <tbody>
                  <tr><th scope="row">Gaia: still</th><td className="tabular">{fmt(tn)}</td><td className={`tabular ${fp ? 'rp-wrong' : ''}`}>{fmt(fp)}<span className="rp-cell-note">false alarms</span></td></tr>
                  <tr><th scope="row">Gaia: moving</th><td className={`tabular ${fn ? 'rp-wrong' : ''}`}>{fmt(fn)}<span className="rp-cell-note">missed</span></td><td className="tabular">{fmt(tp)}</td></tr>
                </tbody>
              </table>
              <p className="rp-foot">{fmt(doc.summary?.n_matched)} of {fmt(doc.summary?.n_objects)} objects matched in {fmt(doc.fields?.length || 0)} fields.</p>
            </Panel>
            <Panel title="Miss ÷ error bar" span={3}
              desc={`Should match the curve if our error bars are honest.${outside(pulls, -5, 5) ? ` ${outside(pulls, -5, 5)} off the chart.` : ''}`}>
              <Histogram values={pulls} lo={-5} hi={5} bins={20} ticks={[-4, -2, 0, 2, 4]} ideal xLabel="miss ÷ error"
                label={`Histogram of ${pulls.length} differences between SPHEREx and Gaia in units of the quoted error, with the ideal bell curve. Spread ${spread?.toFixed(2)}.`} />
            </Panel>
            <Panel title="Miss in ″/yr" span={3}
              desc={`How far our speeds are from Gaia’s. Tall and narrow is good.${outside(resid, -6, 6) ? ` ${outside(resid, -6, 6)} off the chart.` : ''}`}>
              <Histogram values={resid} lo={-6} hi={6} bins={24} ticks={[-6, -3, 0, 3, 6]} xLabel="SPHEREx − Gaia, ″/yr"
                label={`Histogram of ${resid.length} differences between SPHEREx and Gaia motions in arcseconds per year.`} />
            </Panel>
          </div>
          <Provenance docs={[doc]} />
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
    <Section id="rp-fake" title="False alarms"
      desc="Things that looked like they moved but didn’t, and why. Knowing why helps us filter them out."
      badge={s && <Badge tone={s.n_fake ? 'warn' : 'good'}>{s.n_fake ? `${fmt(s.n_fake)} fake` : 'None fake'}</Badge>}>
      <FileState name="false_movers" file={file} index={index} />
      {doc && (
        <>
          <KpiRow label="False alarm numbers">
            <Kpi label="Flagged as moving" value={fmt(s?.n_movers)} help="By the engine" />
            <Kpi label="Turned out fake" value={fmt(s?.n_fake)} help="Not really moving" tone={s?.n_fake ? 'warn' : 'good'} />
            {near && <Kpi label="Close calls" value={fmt(near.n_fake)} help="Stopped by the 1-pixel rule" />}
            {before?.summary && <Kpi label="Flagged before fixes" value={fmt(before.summary.n_movers)} help="Earlier engine version" />}
          </KpiRow>
          <div className="rp-grid">
            <Panel title="Why fake motions happen" desc="Each fake mover, grouped by cause." span={!nearRows.length ? 6 : rows.length ? 3 : 2}>
              {rows.length ? <BarList rows={rows} label="Fake motions by cause" /> : <p className="rp-allclear">None this run</p>}
            </Panel>
            {nearRows.length > 0 && (
              <Panel title="Close calls" desc="Still stars that seemed to move less than one pixel. The 1-pixel rule keeps them out." span={rows.length ? 3 : 4}>
                <BarList rows={nearRows.map((r) => ({ ...r, sentence: null }))} label="Close calls by cause" />
              </Panel>
            )}
          </div>
          {rows.some((r) => r.examples.length) && (
            <details className="rp-prov">
              <summary>Examples</summary>
              <dl className="rp-prov-body rp-examples">
                {rows.filter((r) => r.examples.length).map((r) => (
                  <div key={r.cause}><dt>{CAUSES[r.cause].label}</dt><dd>{r.examples.join(' ')}</dd></div>
                ))}
              </dl>
            </details>
          )}
          <Provenance docs={[doc]} />
        </>
      )}
    </Section>
  );
}

function Reach({ file, index }) {
  const doc = file?.status === 'ok' ? file.doc : null;
  const s = doc?.summary || {};
  const fpi = doc?.false_positives;
  return (
    <Section id="rp-reach" title="Limits: what can it find?"
      desc="We hid fake moving stars in real pictures and counted how many the engine found.">
      <FileState name="injection_recovery" file={file} index={index} />
      {doc && (
        <>
          <KpiRow label="Detection limits">
            <Kpi label="Half found at" value={r1(s.pm_50)} unit="″/yr" help="Slower ones are mostly missed" />
            <Kpi label="9 in 10 found at" value={r1(s.pm_90)} unit="″/yr" help="Fast movers are easy" />
            <Kpi label="Faintest" value={r1(s.mag_50)} unit="mag" help="Fainter stars are mostly lost" />
            {fpi?.n_static_injected > 0 && (
              <Kpi label="Hidden still stars called moving" value={fmt(fpi.n_flagged_mover || 0)} unit={`/ ${fmt(fpi.n_static_injected)}`}
                help="Lower is better" tone={fpi.n_flagged_mover ? 'warn' : 'good'} />
            )}
          </KpiRow>
          <div className="rp-grid">
            <Panel title="Share found" desc="Brighter cell = more found. Solid line: half found. Dashed: 9 in 10." span={3}>
              <InjectionHeatmap doc={doc} />
              <ul className="rp-keys" aria-label="Key">
                <li><i className="rp-key rp-key-ramp" aria-hidden="true" />0% to 100% found</li>
                <li><i className="rp-key rp-key-l50" aria-hidden="true" />half</li>
                <li><i className="rp-key rp-key-l90" aria-hidden="true" />9 in 10</li>
              </ul>
            </Panel>
            <Panel title="Same, in numbers" desc="Rows: brightness (bigger = fainter). Columns: speed. The 0″ column should be 0%." span={3}>
              <InjectionTable doc={doc} />
            </Panel>
          </div>
          <Provenance docs={[doc]} />
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
                const ok = f != null && Number.isFinite(f);
                return (
                  <td key={v} className="tabular" style={ok ? { '--f': f } : undefined}>
                    {ok ? `${Math.round(100 * f)}%` : '–'}
                  </td>
                );
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
  const known = pts.filter((p) => p.kind === 'known_brown_dwarf');
  const knownCool = known.filter((p) => p.flag === 'suggests_brown_dwarf').length;
  const movers = pts.filter((p) => p.kind === 'mover');
  const moversCool = movers.filter((p) => p.flag === 'suggests_brown_dwarf').length;
  const period = curve?.period && curve.constrained
    ? (curve.periodRange && curve.periodRange[1] - curve.periodRange[0] > 0.1 * curve.period
      ? `${Math.round(curve.periodRange[0])}–${Math.round(curve.periodRange[1])}`
      : String(Math.round(curve.period)))
    : null;
  return (
    <Section id="rp-spherex" title="Spectra and brightness"
      desc="SPHEREx sees 102 colours of infrared light, so every star gets a spectrum and a brightness history.">
      {(bdDoc || curve) && (
        <KpiRow label="Spectra numbers">
          {bdDoc && <Kpi label="Known brown dwarfs with both dips" value={fmt(knownCool)} unit={`/ ${fmt(known.length)}`} help="Methane and water found" tone={knownCool ? 'good' : undefined} />}
          {bdDoc && <Kpi label="Our movers with both dips" value={fmt(moversCool)} unit={`/ ${fmt(movers.length || bdDoc.summary?.n_movers || 0)}`}
            help={movers.length ? 'Would suggest a brown dwarf' : 'Not checkable yet'} />}
          {curve?.ratio && <Kpi label="R Draconis brightness swing" value={`${curve.ratio.toFixed(1)}×`} help="Brightest vs faintest" />}
          {period && <Kpi label="R Draconis cycle" value={period} unit="days" help={curve.literature ? `Books say ${Math.round(curve.literature)}` : 'Fitted'} />}
        </KpiRow>
      )}
      <div className="rp-grid">
        <Panel title="Methane and water dips" desc="Cold brown dwarfs have both. Normal stars sit near zero." span={3}>
          <FileState name="brown_dwarfs" file={bd} index={index} />
          {bdDoc && pts.length > 0 && (
            <>
              <SpectralScatter points={pts} />
              <ul className="rp-keys" aria-label="Key">
                {kinds.map((k) => <li key={k}><KindSwatch kind={k} />{SPECTRAL_KINDS[k].label}</li>)}
              </ul>
            </>
          )}
          {bdDoc && pts.length === 0 && <p className="rp-empty">No object had both dips measured.</p>}
        </Panel>
        <Panel title="R Draconis brightness" desc="A giant star that swells and shrinks over months." span={3}>
          <FileState name="light_curve_r_dra" file={lc} index={index} />
          {curve && (
            <div className="rp-stack">
              <LightCurveChart curve={curve} />
              {curve.folded && <LightCurveChart curve={curve} folded />}
            </div>
          )}
          {lcDoc && !curve && <p className="rp-empty">No brightness points in this run.</p>}
        </Panel>
      </div>
      <Provenance docs={[bdDoc, lcDoc]} />
    </Section>
  );
}

/** The Results page: a dashboard of what the survey crawler and the checks found, over the whole app. */
export default function Results({ onClose }) {
  const { index, survey, files } = useResults();
  const closeRef = useRef(null);
  const scrollRef = useRef(null);
  // Focus moves into the page, and back to whatever opened it when it closes. The opener is
  // read while rendering, before focus moves (an effect could run twice and read the close button).
  const [opener] = useState(() => document.activeElement);
  useEffect(() => {
    closeRef.current?.focus();
    return () => { if (opener instanceof HTMLElement && opener.isConnected) opener.focus(); };
  }, [opener]);
  // Jump links scroll inside the page; a hash link would replace #results, which keeps the page open.
  const jump = (id) => scrollRef.current?.querySelector(`#${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  return (
    <div className="rp" role="dialog" aria-modal="true" aria-labelledby="rp-title" ref={scrollRef}>
      <header className="rp-bar">
        <div className="rp-bar-inner">
          <div className="rp-brand">
            <span className="kicker">AstroSearchX · SPHEREx</span>
            <h1 id="rp-title" className="rp-title">Findings</h1>
          </div>
          <nav className="rp-nav" aria-label="Sections">
            {SECTIONS.map(([id, label]) => <button key={id} type="button" onClick={() => jump(id)}>{label}</button>)}
          </nav>
          <button ref={closeRef} type="button" className="iconbtn" onClick={onClose} aria-label="Close results, back to the sky"><CloseIcon /></button>
        </div>
      </header>
      <main className="rp-inner">
        <p className="rp-intro">Anything new is a <b>candidate</b>, not a discovery, until other telescopes confirm it.</p>
        <Crawler survey={survey} />
        <Headline />
        <GaiaAgreement file={files.gaia_validation} index={index} />
        <FakeMotions file={files.false_movers} index={index} />
        <Reach file={files.injection_recovery} index={index} />
        <OnlySpherex bd={files.brown_dwarfs} lc={files.light_curve_r_dra} index={index} />
        <p className="rp-credits">
          Data: SPHEREx (NASA/IPAC IRSA), Gaia DR3 (ESA), 2MASS, AllWISE and CatWISE2020 (IRSA), SIMBAD (CDS).
        </p>
      </main>
    </div>
  );
}
