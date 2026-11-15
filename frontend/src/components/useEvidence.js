import { useEffect, useMemo, useState } from 'react';
import { describe, fetchEvidence, fetchSurveyImages, predictionMotion, proofChecks } from '../lib/evidence.js';

// Data hooks behind an engine object's card: its evidence, the verdict and the proof checks.
/**
 * The evidence behind one engine object, fetched once per object and field: the SPHEREx images
 * per pass and measured positions (GET /api/motion/evidence, re-running the field when the
 * backend no longer keeps it), then the same sky in 2MASS and WISE. Returns
 * { status: 'loading' | 'ok' | 'none' | 'error', evidence, job, error, retry, surveys } where
 * `surveys` is { status, data, error } for the older images (asked for once the evidence is in).
 *
 *   object: a MotionObject from the engine result (see backend/planetx/api/schemas.py)
 *   field:  { ra, dec, size_deg } of the run it came from (`result.field` from runMotion)
 */
export function useEvidence(object, field) {
  // The request depends on these numbers only, so new instances of the same object and field
  // (a parent re-render) do not fetch again.
  const ra = field?.ra, dec = field?.dec, sizeDeg = field?.size_deg;
  const id = object?.id, objectRa = object?.ra, objectDec = object?.dec;
  const key = object && field ? `${ra},${dec},${sizeDeg},${id},${objectRa},${objectDec}` : null;
  const [state, setState] = useState({ key: null });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!key) return undefined;
    const ctrl = new AbortController();
    fetchEvidence({ ra, dec, size_deg: sizeDeg }, { id, ra: objectRa, dec: objectDec }, {
      signal: ctrl.signal,
      onProgress: (job) => { if (!ctrl.signal.aborted) setState({ key, status: 'loading', job }); },
    }).then(
      (evidence) => { if (!ctrl.signal.aborted) setState({ key, status: evidence ? 'ok' : 'none', evidence }); },
      (err) => { if (!ctrl.signal.aborted) setState({ key, status: 'error', error: err?.message || String(err) }); },
    );
    return () => ctrl.abort();
  }, [key, attempt, ra, dec, sizeDeg, id, objectRa, objectDec]);

  // Older survey images: asked for once the SPHEREx evidence is in, and never in its way. They
  // depend on the object only through the motion that places it (numbers, so a new instance of
  // the same object does not ask again).
  const evidence = state.key === key && state.status === 'ok' ? state.evidence : null;
  const { pmEast, pmNorth, err: pmErr, source } = object ? predictionMotion(object) : {};
  const [older, setOlder] = useState({ evidence: null });
  useEffect(() => {
    if (!evidence) return undefined;
    const ctrl = new AbortController();
    fetchSurveyImages({ pmEast, pmNorth, err: pmErr, source }, evidence, { signal: ctrl.signal }).then(
      (data) => { if (!ctrl.signal.aborted) setOlder({ evidence, status: data ? 'ok' : 'none', data }); },
      (err) => { if (!ctrl.signal.aborted) setOlder({ evidence, status: 'error', error: err?.message || String(err) }); },
    );
    return () => ctrl.abort();
  }, [evidence, pmEast, pmNorth, pmErr, source]);

  // Answers for a previous object are ignored: until this object's arrive, it is loading.
  const now = state.key === key ? state : { status: 'loading' };
  return {
    key,
    status: key ? now.status : 'none',
    evidence,
    job: now.job,
    error: now.error,
    retry: () => { setState({ key: null }); setAttempt(attempt + 1); },
    surveys: older.evidence === evidence ? older : { status: 'loading' },
  };
}

/** The verdict in plain words, from the evidence: { tone, headline, lines } (see describe). */
export function useVerdict(object, ev) {
  return useMemo(() => (ev.status === 'ok' ? describe(object, ev.evidence) : null), [object, ev.status, ev.evidence]);
}

/** The proof checklist, only for what the engine thinks moved (for a fixed star "passed" would mislead). */
export function useChecks(object, ev) {
  const moving = object.kind === 'mover' || object.kind === 'candidate';
  return useMemo(() => (moving && ev.status === 'ok' ? proofChecks(object, ev.evidence) : null), [moving, object, ev.status, ev.evidence]);
}
