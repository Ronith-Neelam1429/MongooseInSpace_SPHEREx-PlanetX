import { useMemo, useState } from 'react';
import {
  equatorialToEcliptic, equatorialToGalactic, formatDec, formatFov, formatRa,
} from '../lib/coords.js';
import { API_BASE } from '../lib/services.js';

const fmt = (v, d = 2) => (v == null || !Number.isFinite(v) ? '—' : v.toFixed(d));

function Section({ title, count, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="section">
      <button type="button" className="section-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span>{title}</span>
        {count != null && <span className="badge">{count}</span>}
        <span className="chev" aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
      {open && <div className="section-body">{children}</div>}
    </section>
  );
}

function Status({ state, onRetry, what }) {
  if (state.status === 'loading') return <p className="muted"><span className="spinner" aria-hidden="true" /> Loading {what}…</p>;
  if (state.status === 'error') {
    return (
      <p className="error" role="alert">
        Could not load {what}: {state.error}{' '}
        <button type="button" className="link" onClick={onRetry}>Retry</button>
      </p>
    );
  }
  return null;
}

function cadenceNote(beta) {
  const b = Math.abs(beta);
  if (b >= 80) return 'Near an ecliptic pole: SPHEREx looks here on almost every orbit, so expect many visits.';
  if (b >= 60) return 'High ecliptic latitude: more SPHEREx visits than average.';
  return 'Typical cadence: SPHEREx maps the whole sky every ~6 months, so only a few visits so far.';
}

export default function InfoPanel({
  sel, simbad, gaia, visits, focusId, onFocus, onClose, onRetry, onCopyLink, copied,
}) {
  const coords = useMemo(() => {
    const g = equatorialToGalactic(sel.ra, sel.dec);
    const e = equatorialToEcliptic(sel.ra, sel.dec);
    return { g, e };
  }, [sel.ra, sel.dec]);

  const movers = useMemo(() => {
    if (gaia.status !== 'ok') return [];
    return gaia.stars
      .filter((s) => s.pmra != null && s.pmdec != null)
      .map((s) => ({ ...s, pm: Math.hypot(s.pmra, s.pmdec) }))
      .sort((a, b) => b.pm - a.pm)
      .slice(0, 12);
  }, [gaia]);

  return (
    <aside className="panel" aria-label="Selected position details">
      <header className="panel-head">
        <div>
          <h2>{sel.label || 'Selected position'}</h2>
          <p className="muted small">Search radius {formatFov(sel.radius)}</p>
        </div>
        <div className="panel-actions">
          <button type="button" className="btn ghost" onClick={onCopyLink}>{copied ? 'Copied!' : 'Copy link'}</button>
          <button type="button" className="btn ghost icon" onClick={onClose} aria-label="Close panel">✕</button>
        </div>
      </header>

      <div className="panel-scroll">
        <Section title="Coordinates">
          <table className="kv">
            <tbody>
              <tr><th>RA, Dec (deg)</th><td className="mono">{sel.ra.toFixed(6)}, {sel.dec >= 0 ? '+' : ''}{sel.dec.toFixed(6)}</td></tr>
              <tr><th>RA</th><td className="mono">{formatRa(sel.ra)}</td></tr>
              <tr><th>Dec</th><td className="mono">{formatDec(sel.dec)}</td></tr>
              <tr><th>Galactic l, b</th><td className="mono">{coords.g.l.toFixed(4)}°, {coords.g.b >= 0 ? '+' : ''}{coords.g.b.toFixed(4)}°</td></tr>
              <tr><th>Ecliptic λ, β</th><td className="mono">{coords.e.lambda.toFixed(4)}°, {coords.e.beta >= 0 ? '+' : ''}{coords.e.beta.toFixed(4)}°</td></tr>
            </tbody>
          </table>
          <p className="note">{cadenceNote(coords.e.beta)}</p>
        </Section>

        <Section title="SPHEREx visits" count={visits.status === 'ok' ? visits.visits.length : null}>
          {visits.status === 'none' && (
            <p className="muted">
              Backend not connected yet. This will list SPHEREx visit dates from{' '}
              <code>GET /visits?ra={sel.ra.toFixed(4)}&amp;dec={sel.dec.toFixed(4)}</code>.
              {!API_BASE && ' Set VITE_API_BASE to enable it.'}
            </p>
          )}
          <Status state={visits} onRetry={() => onRetry('visits')} what="SPHEREx visits (IRSA can be slow)" />
          {visits.status === 'ok' && (visits.visits.length === 0 ? <p className="muted">No SPHEREx visits found here yet.</p> : (
            <ul className="visits">
              {visits.visits.map((v, i) => (
                <li key={v.obs_id || i}>
                  <span className="mono">{String(v.date || '').slice(0, 10) || '—'}</span>
                  {v.band && <span className="muted small"> {v.band}</span>}
                  {v.detector != null && <span className="muted small"> · D{v.detector}</span>}
                </li>
              ))}
            </ul>
          ))}
        </Section>

        <Section title="Known objects (SIMBAD)" count={simbad.status === 'ok' ? simbad.objects.length : null}>
          <Status state={simbad} onRetry={() => onRetry('simbad')} what="SIMBAD objects" />
          {simbad.status === 'ok' && simbad.objects.length >= 200 && <p className="muted small">Showing the 200 nearest objects.</p>}
          {simbad.status === 'ok' && (simbad.objects.length === 0 ? <p className="muted">No catalogued objects in this region.</p> : (
            <ul className="rows">
              {simbad.objects.map((o, i) => {
                const id = `s${i}`;
                return (
                  <li key={id}>
                    <button type="button" className={`row ${focusId === id ? 'active' : ''}`} onClick={() => onFocus(id, o.ra, o.dec)}>
                      <span className="row-main">{o.name}</span>
                      <span className="row-sub">{o.type}</span>
                      <span className="row-num mono">{o.mag != null ? `${o.band} ${fmt(o.mag)}` : ''}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          ))}
        </Section>

        <Section title="Gaia DR3 stars" count={gaia.status === 'ok' ? gaia.stars.length : null}>
          <Status state={gaia} onRetry={() => onRetry('gaia')} what="Gaia DR3 stars" />
          {gaia.status === 'ok' && (
            <>
              {gaia.years ? (
                <p className="note">
                  Arrows show where each star moves in <strong>{gaia.years.toLocaleString()} years</strong> (proper motion).
                  <span className="legend"><i className="sw fast" /> ≥ 100 mas/yr <i className="sw slow" /> slower</span>
                  {gaia.stars.length >= gaia.limit && <span className="muted small"> Showing the {gaia.limit} brightest stars.</span>}
                </p>
              ) : <p className="muted">No proper motions in this region.</p>}
              {movers.length > 0 && (
                <>
                  <h3 className="sub">Fastest movers</h3>
                  <ul className="rows">
                    {movers.map((s) => {
                      const id = `g${s.id}`;
                      return (
                        <li key={id}>
                          <button type="button" className={`row ${focusId === id ? 'active' : ''}`} onClick={() => onFocus(id, s.ra, s.dec)}>
                            <span className="row-main mono">Gaia DR3 {s.id}</span>
                            <span className="row-sub">G {fmt(s.gmag)}{s.plx != null ? ` · ϖ ${fmt(s.plx)} mas` : ''}</span>
                            <span className="row-num mono">{s.pm >= 1000 ? `${(s.pm / 1000).toFixed(2)}″/yr` : `${s.pm.toFixed(1)} mas/yr`}</span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </>
              )}
            </>
          )}
        </Section>
      </div>
    </aside>
  );
}
