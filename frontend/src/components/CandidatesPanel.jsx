import { useEffect, useRef, useState } from 'react';
import { formatDec, formatRa } from '../lib/coords.js';
import { classLabel, compass, shortName, speed } from '../lib/plain.js';
import { fetchCandidates } from '../lib/services.js';
import { failPage, mergePage } from './candidatesList.js';
import { CloseIcon } from './ObjectCard.jsx';

const PAGE = 50;
const FILTERS = [
  ['', 'All'],
  ['unknown_candidate', 'Unknown'],
  ['known_star', 'Known stars'],
  ['known_asteroid', 'Asteroids'],
  ['variable_star', 'Variable'],
];

function describe(c) {
  const parts = [];
  // The survey also stores brightness variables, whose fitted motion is only noise: show a speed
  // only when it stands out from its error (3 sigma, the engine's candidate threshold).
  if (c.pm_total > 0 && c.significance >= 3) parts.push(`${speed(c.pm_total)}″/yr ${compass(c.position_angle_deg)}`);
  if (c.is_variable) parts.push('changes brightness');
  if (c.found) parts.push(`found ${c.found}`);
  return parts.join(' · ');
}

/**
 * What the offline survey has flagged so far (GET /api/candidates), most interesting first.
 * Picking one flies to its patch of sky. Everything here is a candidate, not a confirmed find.
 */
export default function CandidatesPanel({ onGo, onClose }) {
  const [filter, setFilter] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [list, setList] = useState({ status: 'loading' });
  // The in-flight "Show more" request, aborted when the filter changes or the panel closes.
  const moreCtrl = useRef(null);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchCandidates({ limit: PAGE, classification: filter || undefined, signal: ctrl.signal }).then((r) => {
      if (!ctrl.signal.aborted) setList(r ? { status: 'ok', filter, ...r } : { status: 'none' });
    }, (err) => {
      if (!ctrl.signal.aborted) setList({ status: 'error', error: err?.message || String(err) });
    });
    return () => { ctrl.abort(); moreCtrl.current?.abort(); };
  }, [filter, attempt]);

  const reload = (next) => { setList({ status: 'loading' }); next(); };

  const more = async () => {
    const requested = filter;
    moreCtrl.current?.abort();
    const ctrl = new AbortController();
    moreCtrl.current = ctrl;
    setList((l) => ({ ...l, loadingMore: true }));
    try {
      const r = await fetchCandidates({
        limit: PAGE, offset: list.candidates.length, classification: requested || undefined, signal: ctrl.signal,
      });
      if (!ctrl.signal.aborted) setList((l) => mergePage(l, r, requested));
    } catch (err) {
      if (!ctrl.signal.aborted) setList((l) => failPage(l, err?.message || String(err), requested));
    }
  };

  return (
    <aside className="panel" aria-label="Survey candidates">
      <header className="panel-head">
        <div className="panel-tools">
          <span className="kicker"><i className="dot measured-bg" />Automatic survey</span>
          <span className="tools">
            <button type="button" className="iconbtn" onClick={onClose} aria-label="Close candidates"><CloseIcon /></button>
          </span>
        </div>
        <h2 className="title">Candidates</h2>
        <p className="coords">Flagged by our survey as moving or changing. Each still needs confirming.</p>
      </header>

      <div className="panel-body">
        <section>
          <div className="filters" role="group" aria-label="Show">
            {FILTERS.map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={`opt ${filter === value ? 'on' : ''}`}
                aria-pressed={filter === value}
                onClick={() => { if (value !== filter) reload(() => setFilter(value)); }}
              >
                {label}
              </button>
            ))}
          </div>
          {list.status === 'loading' && <p className="state"><span className="ticker" aria-hidden="true" />Loading candidates</p>}
          {list.status === 'none' && <p className="line dim">The candidates list is not available from this server yet.</p>}
          {list.status === 'error' && (
            <p className="state err" role="alert">
              Could not load the candidates: {list.error}.{' '}
              <button type="button" className="textbtn" onClick={() => reload(() => setAttempt(attempt + 1))}>Retry</button>
            </p>
          )}
          {list.status === 'ok' && list.candidates.length === 0 && (
            <p className="line dim">
              {filter ? 'No candidates of this kind yet.' : 'No candidates yet. The survey adds what it finds as it works outward from the north ecliptic pole.'}
            </p>
          )}
          {list.status === 'ok' && list.candidates.length > 0 && (
            <>
              <p className="line dim">{list.total.toLocaleString('en-US')} so far, most interesting first.</p>
              <ul className="rowlist">
                {list.candidates.map((c) => (
                  <li key={c.id}>
                    <button type="button" className="row result" onClick={() => onGo(c)}>
                      <span className="row-name" title={c.name || undefined}>{c.name ? shortName(c.name) : 'Uncatalogued object'}</span>
                      <span className="badge">{classLabel(c.classification)}</span>
                      <span className="row-sub">{describe(c) || `${formatRa(c.ra)} ${formatDec(c.dec)}`}</span>
                      {Number.isFinite(c.score) && <span className="row-score" title="How interesting the survey rates it">{Math.round(c.score * 100)}%</span>}
                    </button>
                  </li>
                ))}
              </ul>
              {list.candidates.length < list.total && (
                <p className="line">
                  <button type="button" className="textbtn" disabled={list.loadingMore} onClick={more}>
                    {list.loadingMore ? 'Loading' : 'Show more'}
                  </button>
                </p>
              )}
            </>
          )}
        </section>
      </div>
    </aside>
  );
}
