/**
 * A row of small figures at the top of the panel or a card: [{ label, value, sub, tone, title }].
 * `value` null shows a dash; `value` undefined shows that it is still loading.
 */
export default function Stats({ items, label, className = '' }) {
  return (
    <dl className={`stats ${className}`} aria-label={label} style={{ gridTemplateColumns: `repeat(${items.length}, minmax(0, 1fr))` }}>
      {items.map((it) => (
        <div key={it.label} className="stat" title={it.title}>
          <dt>{it.label}</dt>
          <dd className={`stat-value ${it.tone || ''}`}>
            {it.value === undefined ? <span className="stat-wait" aria-label="loading" /> : it.value ?? '–'}
          </dd>
          {it.sub && <dd className="stat-sub">{it.sub}</dd>}
        </div>
      ))}
    </dl>
  );
}
