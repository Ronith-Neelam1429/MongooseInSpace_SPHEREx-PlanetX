import { useEffect, useRef, useState } from 'react';
import { slots } from '../lib/crawl.js';
import { candidateThumbnailUrl } from '../lib/services.js';
import './CandidatesPanel.css';

// A thumbnail can cost the server a few ~4 MB SPHEREx downloads the first time: two at once, and
// only for pictures that stay in view a moment (scrolling past starts nothing). The server caps
// them too (503 past its limit), which the one retry below absorbs.
const thumbSlots = slots(2);
const THUMB_DWELL_MS = 300;
const THUMB_RETRY_MS = 5000;

/**
 * A small SPHEREx picture of a survey candidate (GET /candidates/{id}/thumbnail.png), loaded only
 * while it is in view (inside `root`, a ref to the scrolling element, else the window). Retries
 * once after a busy or failed answer, then shows an empty frame.
 */
export default function CandidateThumb({ id, root }) {
  const box = useRef(null);
  const [src, setSrc] = useState(null);
  const [state, setState] = useState('wait'); // wait | loading | ok | none
  const release = useRef(null);
  const tries = useRef(0);
  const retry = useRef(0);
  const url = candidateThumbnailUrl(id);

  useEffect(() => {
    if (!url || state !== 'wait' || !box.current) return undefined;
    let timer = 0;
    let cancel = null;
    const stop = () => { clearTimeout(timer); cancel?.(); cancel = null; };
    const io = new IntersectionObserver(([e]) => {
      stop();
      if (!e.isIntersecting) return;
      timer = setTimeout(() => {
        cancel = thumbSlots((done) => {
          release.current = done;
          setState('loading');
          setSrc(tries.current ? `${url}?retry=${tries.current}` : url);
        });
      }, THUMB_DWELL_MS);
    }, { root: root?.current || null });
    io.observe(box.current);
    return () => { io.disconnect(); stop(); };
  }, [url, state, root]);

  // A picture that goes away mid-load gives its slot back.
  useEffect(() => () => { release.current?.(); clearTimeout(retry.current); }, []);

  const finish = (ok) => {
    release.current?.();
    release.current = null;
    if (ok) { setState('ok'); return; }
    setState('none');
    if (tries.current++ < 1) {
      setSrc(null);
      retry.current = setTimeout(() => setState('wait'), THUMB_RETRY_MS);
    }
  };

  return (
    <span ref={box} className={`cand-thumb ${state}`} aria-hidden="true">
      {src && state !== 'none' && <img src={src} alt="" width="56" height="56" decoding="async" onLoad={() => finish(true)} onError={() => finish(false)} />}
    </span>
  );
}
