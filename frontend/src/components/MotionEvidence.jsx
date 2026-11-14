import './MotionEvidence.css';

/**
 * The evidence behind one engine object: real SPHEREx images of it per pass and its measured
 * positions over time. Fills its container's width (340-900 px); no fixed positioning.
 *
 * Props:
 *   object: a MotionObject from the engine result (see backend/planetx/api/schemas.py)
 *   field:  { ra, dec, size_deg } of the run it came from (`result.field` from runMotion)
 */
export default function MotionEvidence({ object, field }) {
  if (!object || !field) return null;
  return null;
}
