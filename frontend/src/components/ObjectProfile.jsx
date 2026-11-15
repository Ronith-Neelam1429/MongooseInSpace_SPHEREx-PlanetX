import { useEffect, useMemo, useRef, useState } from 'react';
import { API_BASE, fetchProfile } from '../lib/services.js';
import { decimalYear } from '../lib/coords.js';
import { ticks } from '../lib/evidence.js';
import {
  approachModel, distanceView, gravityView, identityLine, lyText, pathModel, profileQuery, researcherGroups, safeLinks, sizeView,
  speedView, temperatureView, whyList, yearText,
} from '../lib/profile.js';
import Stats from './Stats.jsx';
import './ObjectProfile.css';

const Unit = ({ children }) => <span className="pf-unit">{children}</span>;

/** The object's disc next to the reference bodies near its size, bottoms aligned. */
function SizeDiscs({ discs, color }) {
  const slot = (d) => Math.max(2 * d.px + 8, 70);
  const maxPx = Math.max(...discs.discs.map((d) => d.px));
  const lefts = discs.discs.map((_, i) => discs.discs.slice(0, i).reduce((s, d) => s + slot(d), 0));
  const W = discs.discs.reduce((s, d) => s + slot(d), 0);
  const base = 2 * maxPx + 4;
  const H = base + 16;
  const label = discs.discs.map((d) => `${d.label} ${d.rsun.toPrecision(2)} Sun widths`).join(', ');
  return (
    <svg className="pf-discs" viewBox={`0 0 ${W} ${H}`} width={W} role="img" aria-label={`Sizes compared${discs.log ? ' on a log scale' : ''}: ${label}.`}>
      {discs.discs.map((d, i) => {
        const cx = lefts[i] + slot(d) / 2;
        return (
          <g key={d.label} className={d.self ? 'pf-disc-self' : `pf-disc-ref pf-ref-${d.label.toLowerCase()}`}>
            <circle cx={cx} cy={base - d.px} r={d.px} style={d.self && color ? { fill: color } : undefined} />
            <text x={cx} y={H - 2} textAnchor="middle">{d.label}</text>
          </g>
        );
      })}
    </svg>
  );
}

const PW = 320, PH = 190, PAD = 22;

/** The sky track over the profile's span: east to the left as on the sky, today ringed, an arrow toward the future. */
function PathChart({ model }) {
  const xs = model.points.map((q) => -q.x), ys = model.points.map((q) => q.y);
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)], [y0, y1] = [Math.min(...ys), Math.max(...ys)];
  const span = Math.max(x1 - x0, y1 - y0) || 1;
  const s = Math.min((PW - 2 * PAD) / (x1 - x0 || span), (PH - 2 * PAD) / (y1 - y0 || span));
  // A mostly north-south track gets a narrower box (room for the year labels either side).
  const W = Math.round(Math.max(220, Math.min(PW, (x1 - x0) * s + 2 * PAD + 150)));
  const ox = W / 2 - ((x0 + x1) / 2) * s, oy = PH / 2 + ((y0 + y1) / 2) * s;
  const px = (q) => ox - q.x * s, py = (q) => oy - q.y * s;
  const line = model.points.map((q) => `${px(q).toFixed(1)},${py(q).toFixed(1)}`).join(' ');
  const [a, b] = model.points.slice(-2);
  const ang = Math.atan2(py(b) - py(a), px(b) - px(a));
  const tip = { x: px(b), y: py(b) };
  const head = [ang + 2.6, ang - 2.6].map((t) => `${(tip.x + 7 * Math.cos(t)).toFixed(1)},${(tip.y + 7 * Math.sin(t)).toFixed(1)}`);
  const place = (q, dy) => {
    const x = px(q), right = x < W / 2;
    return { x: x + (right ? 7 : -7), y: py(q) + dy, anchor: right ? 'start' : 'end' };
  };
  const startAt = place(model.start, py(model.start) > py(model.end) ? 12 : -6);
  const endAt = place(model.end, py(model.end) >= py(model.start) ? 12 : -6);
  const nowAt = place(model.now, 4);
  const bar = model.scale ? model.scale.deg * s : 0;
  return (
    <svg className="pf-chart" viewBox={`0 0 ${W} ${PH}`} width={W} role="img"
      aria-label={`Its path across the sky from ${yearText(model.start.year)} to ${yearText(model.end.year)}, today's position marked.`}>
      <rect className="pf-plot" x="0.5" y="0.5" width={W - 1} height={PH - 1} />
      <text className="pf-tick" x="8" y="14">N ↑  E ←</text>
      <polyline className="pf-track" points={line} />
      {model.points.map((q) => <circle key={q.year} className="pf-step" cx={px(q)} cy={py(q)} r="1.3" />)}
      <polygon className="pf-head" points={`${tip.x.toFixed(1)},${tip.y.toFixed(1)} ${head.join(' ')}`} />
      <circle className="pf-now" cx={px(model.now)} cy={py(model.now)} r="4" />
      <text className="pf-label" x={startAt.x} y={startAt.y} textAnchor={startAt.anchor}>{yearText(model.start.year)}</text>
      <text className="pf-label" x={endAt.x} y={endAt.y} textAnchor={endAt.anchor}>{yearText(model.end.year)}</text>
      <text className="pf-label pf-label-now" x={nowAt.x} y={nowAt.y} textAnchor={nowAt.anchor}>today</text>
      {bar > 4 && (
        <g className="pf-scale">
          <line x1={W - 10 - bar} x2={W - 10} y1={PH - 12} y2={PH - 12} />
          <text className="pf-tick" x={W - 10 - bar / 2} y={PH - 17} textAnchor="middle">{model.scale.label}</text>
        </g>
      )}
    </svg>
  );
}

const AW = 320, AH = 118, AX = [34, 312], AY = [10, 92];

/** Distance from the Sun against year along the same straight-line path, the closest point labelled. */
function ApproachChart({ model }) {
  const years = model.points.map((q) => q.year), lys = model.points.map((q) => q.ly);
  const [t0, t1] = [Math.min(...years), Math.max(...years)];
  const yMax = Math.max(...lys) * 1.1;
  const sx = (t) => AX[0] + ((t - t0) / (t1 - t0)) * (AX[1] - AX[0]);
  const sy = (v) => AY[1] - (v / yMax) * (AY[1] - AY[0]);
  const yTicks = ticks(0, yMax, 3).filter((v) => v <= yMax);
  const c = model.closest;
  const right = sx(c.year) > (AX[0] + AX[1]) / 2;
  return (
    <svg className="pf-chart" viewBox={`0 0 ${AW} ${AH}`} width={AW} role="img" aria-label={`Distance from the Sun over time. ${model.phrase}`}>
      {yTicks.map((v) => (
        <g key={v}>
          <line className={v === 0 ? 'pf-zero' : 'pf-grid'} x1={AX[0]} x2={AX[1]} y1={sy(v)} y2={sy(v)} />
          <text className="pf-tick" x={AX[0] - 5} y={sy(v) + 3} textAnchor="end">{v}</text>
        </g>
      ))}
      <text className="pf-tick" x={AX[0] - 5} y={AY[0] - 1} textAnchor="end">ly</text>
      <polyline className="pf-dist" points={model.points.map((q) => `${sx(q.year).toFixed(1)},${sy(q.ly).toFixed(1)}`).join(' ')} />
      <circle className="pf-closest" cx={sx(c.year)} cy={sy(c.ly)} r="3" />
      <text className="pf-label" x={sx(c.year) + (right ? -6 : 6)} y={sy(c.ly) + 14} textAnchor={right ? 'end' : 'start'}>
        closest: {lyText(c.ly)} ly, {yearText(c.year)}
      </text>
      {model.now && model.now.year >= t0 && model.now.year <= t1 && (
        <circle className="pf-now" cx={sx(model.now.year)} cy={sy(model.now.ly)} r="3.5"><title>Today: {lyText(model.now.ly)} ly</title></circle>
      )}
      <text className="pf-tick" x={AX[0]} y={AH - 4}>{yearText(t0)}</text>
      <text className="pf-tick" x={AX[1]} y={AH - 4} textAnchor="end">{yearText(t1)}</text>
    </svg>
  );
}

function CopyJson({ profile }) {
  const [said, setSaid] = useState(null);
  const timer = useRef(0);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(JSON.stringify(profile, null, 2));
      setSaid('Copied');
    } catch (err) {
      console.error('Copy failed', err);
      setSaid('Copy failed');
    }
    timer.current = setTimeout(() => setSaid(null), 1600);
  };
  return (
    <button type="button" className="textbtn pf-copy" onClick={copy}>
      <span aria-live="polite">{said || 'Copy JSON'}</span>
    </button>
  );
}

function Researchers({ profile }) {
  const groups = useMemo(() => researcherGroups(profile), [profile]);
  const links = safeLinks(profile);
  const sources = Array.isArray(profile.sources) ? profile.sources.filter((x) => typeof x === 'string') : [];
  const notes = Array.isArray(profile.notes) ? profile.notes.filter((x) => typeof x === 'string') : [];
  return (
    <details className="more pf-research">
      <summary>For researchers</summary>
      {groups.map((g) => (
        <div key={g.title}>
          <h4>{g.title}</h4>
          <dl className="kv">{g.rows.map(([k, v]) => <div key={k} className="pf-row"><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
        </div>
      ))}
      {links.length > 0 && (
        <>
          <h4>Links</h4>
          <ul className="pf-links">
            {links.map((l) => <li key={l.url}><a href={l.url} target="_blank" rel="noopener noreferrer">{l.label}</a></li>)}
          </ul>
        </>
      )}
      {sources.length > 0 && <><h4>Sources</h4><p className="line dim">{sources.join(' · ')}</p></>}
      {notes.length > 0 && <><h4>Notes</h4>{notes.map((t) => <p key={t} className="line dim">{t}</p>)}</>}
      <p className="pf-actions"><CopyJson profile={profile} /></p>
    </details>
  );
}

function ProfileBody({ profile: p }) {
  const v = useMemo(() => {
    const nowYear = decimalYear();
    return {
      who: identityLine(p),
      dist: distanceView(p),
      size: sizeView(p),
      temp: temperatureView(p),
      speed: speedView(p),
      why: whyList(p),
      path: pathModel(p, nowYear),
      approach: approachModel(p, nowYear),
      gravity: gravityView(p),
    };
  }, [p]);
  const tiles = [
    v.dist && { label: 'Distance', value: <>{v.dist.value}<Unit> {v.dist.unit}</Unit></>, sub: v.dist.sub, title: 'Distance from the Sun' },
    v.size && { label: 'Size', value: v.size.value, sub: v.size.sub, title: "Its width compared with the Sun's" },
    v.temp && {
      label: 'Temperature',
      value: <>{v.temp.color && <i className="pf-swatch" style={{ background: v.temp.color }} aria-hidden="true" />}{v.temp.value}<Unit> {v.temp.unit}</Unit></>,
      sub: v.temp.sub, title: 'Surface temperature; the swatch is the colour of a body that hot',
    },
    v.speed && { label: 'Speed', value: <>{v.speed.value}<Unit> {v.speed.unit}</Unit></>, sub: v.speed.sub, title: 'How fast it moves' },
  ].filter(Boolean);
  const facts = [
    v.dist && ['Distance', [v.dist.phrase, v.dist.light, v.dist.note].filter(Boolean).join(' ')],
    v.temp && ['Colour', v.temp.phrase],
    v.speed?.phrase && ['Speed', v.speed.phrase],
  ].filter(Boolean);
  return (
    <>
      {v.who && <p className="line dim pf-who">{v.who}</p>}
      {tiles.length > 0 && <Stats className="pf-stats" label="About this object in numbers" items={tiles} />}
      {facts.length > 0 && (
        <dl className="facts pf-facts">{facts.map(([k, t]) => <div key={k} className="pf-row"><dt>{k}</dt><dd>{t}</dd></div>)}</dl>
      )}

      {v.size && (
        <div className="pf-part">
          <h4 className="pf-h">Size</h4>
          {v.size.discs && <SizeDiscs discs={v.size.discs} color={v.temp?.color} />}
          <p className="line">{v.size.phrase}{v.size.estimate && <span className="dim"> (an estimate from its brightness and temperature)</span>}.</p>
          {v.size.discs?.log && <p className="pf-note">Drawn on a log scale: the real differences are larger.</p>}
        </div>
      )}

      {v.why.length > 0 && (
        <div className="pf-part">
          <h4 className="pf-h">Why it moves</h4>
          <ul className="pf-why">{v.why.map((w) => <li key={w.key} className="line">{w.text}</li>)}</ul>
        </div>
      )}

      {v.path && (
        <div className="pf-part">
          <h4 className="pf-h">Its path</h4>
          <PathChart model={v.path} />
          <p className="pf-note">
            Across our sky over {v.path.span.toLocaleString('en-US')} years, {yearText(v.path.start.year)} to {yearText(v.path.end.year)}.
            {v.path.note && ` ${v.path.note}`}
          </p>
          {v.approach && (
            <>
              {v.approach.inSpan && <ApproachChart model={v.approach} />}
              <p className="pf-note">{v.approach.phrase}</p>
            </>
          )}
        </div>
      )}

      {v.gravity && (
        <div className="pf-part">
          <h4 className="pf-h">Gravity</h4>
          {v.gravity.summary && <p className="line">{v.gravity.summary}</p>}
          {v.gravity.companions.length > 0 && (
            <ul className="pf-companions">
              {v.gravity.companions.map((c) => <li key={c.key} className="line"><b>{c.name}</b>{c.text && <span className="dim"> · {c.text}</span>}</li>)}
            </ul>
          )}
          {v.gravity.wobble && <p className="line dim">{v.gravity.wobble}</p>}
        </div>
      )}

      <Researchers profile={p} />
    </>
  );
}

/**
 * "About this object": distance, size, temperature, speed, why it moves, its path and what pulls
 * on it, from GET /api/object/profile. `params` is what the card knows (see lib/profile.js
 * knownProfileParams / engineProfileParams). Hidden without a backend.
 */
export default function ObjectProfile({ params }) {
  const query = profileQuery(params);
  const [attempt, setAttempt] = useState(0);
  const key = `${query}#${attempt}`;
  const [held, setHeld] = useState({ key: null });
  const latest = useRef(params);
  useEffect(() => { latest.current = params; });

  // A new object (or a retry) asks again; leaving it aborts the request.
  useEffect(() => {
    if (!API_BASE || !query) return undefined;
    const ctrl = new AbortController();
    fetchProfile(latest.current, { signal: ctrl.signal }).then(
      (profile) => { if (!ctrl.signal.aborted) setHeld({ key, status: 'ok', profile }); },
      (err) => { if (!ctrl.signal.aborted) setHeld({ key, status: 'error', error: err?.message || String(err) }); },
    );
    return () => ctrl.abort();
  }, [key, query]);

  if (!API_BASE || !query) return null;
  const state = held.key === key ? held : { status: 'loading' };
  return (
    <section className="profile" aria-labelledby="profile-title" aria-busy={state.status === 'loading'}>
      <h3 id="profile-title">About this object</h3>
      {state.status === 'loading' && (
        <div className="pf-skeleton" role="status" aria-label="Looking up what is known about it">
          <span /><span /><span />
        </div>
      )}
      {state.status === 'error' && (
        <p className="line dim" role="alert">
          Could not look it up ({state.error}).{' '}
          <button type="button" className="textbtn" onClick={() => setAttempt((n) => n + 1)}>Retry</button>
        </p>
      )}
      {state.status === 'ok' && <ProfileBody profile={state.profile} />}
    </section>
  );
}
