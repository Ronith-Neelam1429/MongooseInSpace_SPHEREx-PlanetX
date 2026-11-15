import { useEffect, useRef, useState } from 'react';
import { candidateLine, foundDate } from '../lib/crawl.js';
import { classLabel, shortName } from '../lib/plain.js';
import { fetchCandidates } from '../lib/services.js';
import { failPage, mergePage } from './candidatesList.js';
import CandidateThumb from './CandidateThumb.jsx';
import { CloseIcon } from './ObjectCard.jsx';
import './CandidatesPanel.css';

const PAGE = 50;
const FILTERS = [
  ['', 'All'],
  ['unknown_candidate', 'Unknown'],
  ['known_star', 'Known stars'],
  ['known_asteroid', 'Asteroids'],
  ['variable_star', 'Variable'],
  ['artifact', 'Artifacts'],
];
const SORTS = [['score', 'Best first'], ['date', 'Newest first']];

/**
 * What the offline survey has flagged so far (GET /api/candidates), ranked by score or newest
 * first. Picking one flies to its field and opens its card. Everything here is a candidate, not a
 * confirmed find.
 */
export default function CandidatesPanel({ onGo, onClose }) {
  const [filter, setFilter] = useState('');
  const [sort, setSort] = useState('score');
  const [attempt, setAttempt] = useState(0);
  const [list, setList] = useState({ status: 'loading' });
  const body = useRef(null);
  // The in-flight "Show more" request, aborted when the filter changes or the panel closes.
  const moreCtrl = useRef(null);
  // The list a page belongs to: filter and sort (mergePage drops a page asked for another one).
  const view = `${filter}|${sort}`;

  useEffect(() => {
    const ctrl = new AbortController();
    fetchCandidates({ limit: PAGE, classification: filter || undefined, sort, signal: ctrl.signal }).then((r) => {
      if (!ctrl.signal.aborted) setList(r ? { status: 'ok', filter: view, ...r } : { status: 'none' });
    }, (err) => {
      if (!ctrl.signal.aborted) setList({ status: 'error', error: err?.message || String(err) });
    });
    return () => { ctrl.abort(); moreCtrl.current?.abort(); };
  }, [filter, sort, attempt, view]);

  const reload = (next) => { setList({ status: 'loading' }); next(); };

  const more = async () => {
    const requested = view;
    moreCtrl.current?.abort();
    const ctrl = new AbortController();
    moreCtrl.current = ctrl;
    setList((l) => ({ ...l, loadingMore: true }));
    try {
      const r = await fetchCandidates({
        limit: PAGE, offset: list.candidates.length, classification: filter || undefined, sort, signal: ctrl.signal,
      });
      if (!ctrl.signal.aborted) setList((l) => mergePage(l, r, requested));
    } catch (err) {
      if (!ctrl.signal.aborted) setList((l) => failPage(l, err?.message || String(err), requested));
    }
  };

  return (
    <aside className="panel candidates" aria-label="Survey candidates">
      <header className="panel-head">
        <div className="panel-tools">
          <span className="kicker"><i className="dot measured-bg" />Automatic survey</span>
          <span className="tools">
            <button type="button" className="iconbtn" onClick={onClose} aria-label="Close candidates"><CloseIcon /></button>
          </span>
        </div>
        <h2 className="title">Candidates</h2>
        <p className="coords">Flagged by our survey as moving or changing brightness. Each is a candidate that still needs confirming.</p>
      </header>

      <div className="panel-body" ref={body}>
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
          <div className="filters cand-sort" role="group" aria-label="Order">
            {SORTS.map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={`opt ${sort === value ? 'on' : ''}`}
                aria-pressed={sort === value}
                onClick={() => { if (value !== sort) reload(() => setSort(value)); }}
              >
                {label}
              </button>
            ))}
          </div>
          {list.status === 'loading' && <p className="state"><span className="ticker" aria-hidden="true" />Loading candidates</p>}
          {list.status === 'none' && <p className="line dim">The candidates list needs the PlanetX server, which is not connected (or does not offer it yet).</p>}
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
              <p className="line dim">
                {list.total.toLocaleString('en-US')} so far, {sort === 'date' ? 'newest first' : 'most interesting first'}. Pick one to open it on the map.
              </p>
              <ol className="rowlist cand-list">
                {list.candidates.map((c, i) => (
                  <li key={c.id}>
                    <button type="button" className="row cand-row" onClick={() => onGo(c)}>
                      <CandidateThumb id={c.id} root={body} />
                      <span className="cand-main">
                        <span className="cand-top">
                          {sort === 'score' && <span className="cand-rank tabular">{i + 1}</span>}
                          <span className="row-name" title={c.name || undefined}>{c.name ? shortName(c.name) : 'Uncatalogued object'}</span>
                          <span className="badge">{classLabel(c.classification)}</span>
                        </span>
                        <span className="row-sub">{candidateLine(c)}</span>
                        <span className="cand-meta">
                          patch {c.ipix.toLocaleString('en-US')} · found {foundDate(c.found)}
                        </span>
                      </span>
                      {Number.isFinite(c.score) && (
                        <span className="row-score" title="How interesting the survey rates it, 0 to 1: unexplained clear motion ranks highest">
                          <span className="tabular">{c.score.toFixed(2)}</span>
                          <span className="cand-score-label">score</span>
                        </span>
                      )}
                    </button>
                  </li>
                ))}
              </ol>
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
