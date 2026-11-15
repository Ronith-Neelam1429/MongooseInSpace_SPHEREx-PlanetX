import { useState } from 'react';
import { CLASSES, SHOW_ALL, SHOW_MOVING, isAll, isMovingOnly } from '../lib/classes.js';

const count = (n) => n.toLocaleString('en-US');

// The colour tokens of index.css that match each class's canvas colour, so a glyph follows the
// theme of what it sits on (the panel can be light; the map is always dark).
const TOKEN = {
  candidate: 'var(--measured)', mover: 'var(--measured)', fast: 'var(--signal)', variable: 'var(--variable)',
  star: 'var(--fg)', galaxy: 'var(--galaxy)', other: 'var(--dim)',
};

/** The legend glyph of a class, drawn the way the map draws it. */
export function ClassMark({ cls, size = 12 }) {
  const c = TOKEN[cls.id] || `rgb(${cls.rgb})`;
  const h = size / 2;
  let shape;
  if (cls.mark === 'dot') shape = <circle cx={h} cy={h} r={size * 0.22} fill={c} />;
  else if (cls.mark === 'square') shape = <rect x={size * 0.2} y={size * 0.2} width={size * 0.6} height={size * 0.6} fill="none" stroke={c} />;
  else if (cls.mark === 'diamond') shape = <path d={`M${h} 1L${size - 1} ${h}L${h} ${size - 1}L1 ${h}Z`} fill="none" stroke={c} />;
  else shape = <circle cx={h} cy={h} r={h - 1.5} fill="none" stroke={c} strokeWidth="1.25" strokeDasharray={cls.mark === 'dashed' ? '2.5 2' : undefined} />;
  return <svg className="mark-glyph" width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">{shape}</svg>;
}

/**
 * The map's class filter and legend: one switch per kind of object, with how many the open field
 * holds, and presets for "only what moves" and "everything". Classes switched off fade on the map
 * and drop out of the side panel's lists.
 */
export default function ClassFilter({ filter, counts, onChange }) {
  // Folded on a phone, where the map is small.
  const [open, setOpen] = useState(() => !window.matchMedia?.('(max-width: 760px)').matches);
  const moving = isMovingOnly(filter);
  const all = isAll(filter);
  return (
    <section className={`classes ${open ? 'open' : ''}`} aria-label="Filter objects by kind">
      <div className="classes-head">
        <button type="button" className="opt classes-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>Filter</button>
        <div className="classes-presets" role="group" aria-label="Presets">
          <button type="button" className={`opt ${moving ? 'on' : ''}`} aria-pressed={moving} aria-label="Moving only" title="Show only what moves, or might: the best place to start" onClick={() => onChange(SHOW_MOVING)}>Moving</button>
          <button type="button" className={`opt ${all ? 'on' : ''}`} aria-pressed={all} onClick={() => onChange(SHOW_ALL)}>All</button>
        </div>
      </div>
      {open && (
        <ul className="classes-list">
          {CLASSES.map((c) => {
            const on = filter[c.id];
            const n = counts?.[c.id] ?? 0;
            return (
              <li key={c.id}>
                <button
                  type="button"
                  className={`class-row ${on ? 'on' : ''} ${n === 0 ? 'empty' : ''}`}
                  aria-pressed={on}
                  aria-label={counts ? `${c.label}: ${count(n)}` : c.label}
                  title={c.hint}
                  onClick={() => onChange({ ...filter, [c.id]: !on })}
                >
                  <ClassMark cls={c} />
                  <span className="class-label">{c.label}</span>
                  <span className="class-count tabular">{counts ? count(n) : ''}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
