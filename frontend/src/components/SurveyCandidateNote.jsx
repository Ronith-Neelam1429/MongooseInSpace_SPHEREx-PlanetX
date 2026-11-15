import { candidateLine, foundDate, matchCandidate } from '../lib/crawl.js';
import { classLabel, fakeMotionLine, motionVerdict, shortName } from '../lib/plain.js';
import CandidateThumb from './CandidateThumb.jsx';
import { VerdictReason, VerdictTag } from './MotionEvidence.jsx';

/**
 * The survey candidate this patch was opened for: its picture and what the survey stored, and the
 * way to its own pictures and proof. Those need an engine run on this field (tens of ~4 MB SPHEREx
 * downloads when the server has not cached them), so it starts only on request; its card then
 * opens once the run finds it again.
 */
export default function SurveyCandidateNote({ candidate: c, motion, onRun }) {
  const lost = motion.status === 'ok' && !matchCandidate(motion.result.objects, c);
  const mv = motionVerdict(c);
  return (
    <section className="cand-note" aria-label="Survey candidate">
      <div className="cand-note-head">
        <CandidateThumb id={c.id} />
        <div className="cand-main">
          <span className="kicker">Survey candidate · {classLabel(c.classification)}</span>
          <span className="row-name">{c.name ? shortName(c.name) : 'Uncatalogued object'}</span>
          {mv && <span><VerdictTag mv={mv} /></span>}
          <span className="row-sub">{fakeMotionLine(c) || candidateLine(c)}</span>
          <span className="cand-meta">score {c.score.toFixed(2)} · patch {c.ipix.toLocaleString('en-US')} · found {foundDate(c.found)}</span>
        </div>
      </div>
      {mv && <VerdictReason mv={mv} />}
      {motion.status === 'idle' && (
        <>
          <button type="button" className="btn" onClick={onRun}>Run the engine here</button>
          <p className="line dim">to see its pictures and proof. Compares about 50 SPHEREx images; a minute or two unless they are cached.</p>
        </>
      )}
      {motion.status === 'loading' && <p className="line dim">Its card opens when the run finds it again.</p>}
      {lost && (
        <p className="line dim">
          This run did not find it again within one SPHEREx pixel of where the survey saw it; what the run found is listed below.
        </p>
      )}
    </section>
  );
}
