import { COVERAGE_LAYERS, surveyProgress } from '../lib/crawl.js';
import './CandidatesPanel.css';

/** The coverage layer's key and progress line ("312 of 49,152 patches searched"), on the map. */
export default function SurveyLegend({ crawl }) {
  if (crawl.status === 'loading') return <span className="hud-note" role="status">Loading the survey</span>;
  if (crawl.status === 'none') return <span className="hud-note" role="status">The survey needs the PlanetX server, which is not connected.</span>;
  if (crawl.status === 'error') return <span className="hud-note" role="alert">Survey unavailable: {crawl.error}.</span>;
  if (crawl.status !== 'ok') return null;
  const { status, coverage } = crawl.info;
  return (
    <div className="hud-note crawl-legend" role="status">
      <span>{surveyProgress(status)}</span>
      {status.total > 0 && (
        <span className="crawl-keys">
          {COVERAGE_LAYERS.filter((l) => coverage[l.key]).map((l) => (
            <span key={l.key}><i className={`sw ${l.swatch}`} aria-hidden="true" />{l.label}</span>
          ))}
        </span>
      )}
    </div>
  );
}
