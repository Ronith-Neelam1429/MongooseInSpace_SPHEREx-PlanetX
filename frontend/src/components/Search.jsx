import { forwardRef, useId, useState } from 'react';
import { TARGETS } from '../lib/targets.js';

/** Name / coordinate search with the project's test targets as suggestions. */
const Search = forwardRef(function Search({ value, onChange, onSubmit, onPick, busy, error, disabled }, ref) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const listId = useId();
  const q = value.trim().toLowerCase();
  const items = TARGETS.filter((t) => !q || t.label.toLowerCase().includes(q) || t.aliases.some((a) => a.includes(q)));
  const show = open && items.length > 0;

  const pick = (t) => {
    setOpen(false);
    setActive(-1);
    onPick(t);
  };

  const onKeyDown = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setOpen(true); setActive((i) => Math.min(items.length - 1, i + 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((i) => Math.max(-1, i - 1)); }
    else if (e.key === 'Escape') { setOpen(false); setActive(-1); e.currentTarget.blur(); }
    else if (e.key === 'Enter' && show && active >= 0) { e.preventDefault(); pick(items[active]); }
  };

  return (
    <form
      className="search"
      role="search"
      onSubmit={(e) => { e.preventDefault(); setOpen(false); onSubmit(value); }}
    >
      <input
        ref={ref}
        type="text"
        value={value}
        onChange={(e) => { onChange(e.target.value); setOpen(true); setActive(-1); }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={onKeyDown}
        placeholder="Object or coordinates"
        aria-label="Search by object name or coordinates"
        role="combobox"
        aria-expanded={show}
        aria-controls={listId}
        aria-activedescendant={show && active >= 0 ? `${listId}-${active}` : undefined}
        autoComplete="off"
        spellCheck="false"
        disabled={disabled}
      />
      {busy && <span className="search-busy" aria-label="Resolving" />}
      {show && (
        <ul className="suggest" id={listId} role="listbox">
          {items.map((t, i) => (
            <li
              key={t.id}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              className={i === active ? 'on' : ''}
              onMouseDown={(e) => { e.preventDefault(); pick(t); }}
              onMouseEnter={() => setActive(i)}
            >
              <span>{t.label}</span>
              <span className="dim">{t.tag}</span>
            </li>
          ))}
        </ul>
      )}
      {error && !show && <p className="search-error" role="alert">{error}</p>}
    </form>
  );
});

export default Search;
