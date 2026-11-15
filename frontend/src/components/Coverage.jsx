import { useMemo } from 'react';
import { perDetector, timeline, yearTicks } from '../lib/coverage.js';
import { dateSpan } from '../lib/plain.js';

const W = 344;
const count = (n) => n.toLocaleString('en-US');
// Lower edge of each detector's band (µm), and the top of the last.
const BAND_EDGES = ['0.75', '1.1', '1.6', '2.4', '3.8', '4.4', '5.0'];

/** When SPHEREx imaged this spot: images per week from the survey's start to today, passes shaded. */
function Timeline({ passes, visits }) {
  const t = useMemo(() => timeline(passes, visits), [passes, visits]);
  const years = yearTicks(t);
  const H = 58, top = 12, base = 40;
  const X = (f) => f * W;
  return (
    <svg className="cov-chart" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`SPHEREx images over time: ${passes.map((p) => `${dateSpan(p.start, p.end)}, ${p.n_visits}`).join('; ')}`}>
      {t.passes.map((p) => (
        <rect key={p.start} className="cov-pass" x={X(p.x0)} y={top - 4} width={Math.max(2, X(p.x1 - p.x0))} height={base - top + 4}>
          <title>{`${dateSpan(p.start, p.end)}: ${count(p.n_visits)} images`}</title>
        </rect>
      ))}
      {t.bins.map((b) => {
        const h = Math.max(1.5, ((base - top) * b.n) / t.max);
        return <rect key={b.x0} className="cov-bar" x={X(b.x0)} y={base - h} width={Math.max(1.5, X(b.x1 - b.x0) - 0.5)} height={h} />;
      })}
      <line className="cov-axis" x1="0" x2={W} y1={base + 0.5} y2={base + 0.5} />
      {years.map((y) => (
        <g key={y.label}>
          <line className="cov-axis" x1={X(y.x)} x2={X(y.x)} y1={base} y2={base + 5} />
          <text className="cov-label" x={X(y.x) + 3} y={H - 4}>{y.label}</text>
        </g>
      ))}
      <text className="cov-label" x="0" y={H - 4}>{t.start.getUTCFullYear()}</text>
      <line className="cov-today" x1={X(t.today)} x2={X(t.today)} y1={top - 6} y2={base} />
      <text className="cov-label" x={X(t.today) - 3} y={top - 4} textAnchor="end">today</text>
    </svg>
  );
}

/** Which colours SPHEREx has here: images per detector, from 0.75 to 5 µm. */
function Bands({ visits }) {
  const n = useMemo(() => perDetector(visits), [visits]);
  const max = Math.max(1, ...n);
  const H = 56, top = 12, base = 40, col = W / 6;
  return (
    <svg className="cov-chart" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`SPHEREx images per wavelength band, 0.75 to 5 µm: ${n.join(', ')}`}>
      {n.map((v, i) => {
        const h = v ? Math.max(1.5, ((base - top) * v) / max) : 0;
        return (
          <g key={i}>
            <rect className={v ? 'cov-bar' : 'cov-empty'} x={i * col + 3} y={v ? base - h : base - 1} width={col - 6} height={v ? h : 1}>
              <title>{`Detector ${i + 1}, ${BAND_EDGES[i]}–${BAND_EDGES[i + 1]} µm: ${count(v)} images`}</title>
            </rect>
            <text className="cov-num" x={i * col + col / 2} y={(v ? base - h : base) - 3} textAnchor="middle">{count(v)}</text>
            <text className="cov-label" x={i * col + 3} y={H - 4}>{BAND_EDGES[i]}</text>
          </g>
        );
      })}
      <line className="cov-axis" x1="0" x2={W} y1={base + 0.5} y2={base + 0.5} />
      <text className="cov-label" x={W} y={H - 4} textAnchor="end">5 µm</text>
    </svg>
  );
}

/**
 * The SPHEREx coverage of the open patch: when it was imaged and in which colours, from the
 * /visits answer. The per-band chart needs single visits, which older servers do not send.
 */
export default function Coverage({ visits }) {
  const placed = visits.visits?.some((v) => v.detector != null || v.wavelength_min_um != null);
  return (
    <div className="coverage">
      <h4>When it was imaged</h4>
      <Timeline passes={visits.passes} visits={visits.visits || []} />
      {visits.passes.length <= 4 && (
        <ol className="cov-passes">
          {visits.passes.map((p) => (
            <li key={p.start}><span>{dateSpan(p.start, p.end)}</span><span className="dim">{count(p.n_visits)} images</span></li>
          ))}
        </ol>
      )}
      {placed && (
        <>
          <h4>In which colours <span className="dim">(wavelength, µm)</span></h4>
          <Bands visits={visits.visits} />
        </>
      )}
    </div>
  );
}
