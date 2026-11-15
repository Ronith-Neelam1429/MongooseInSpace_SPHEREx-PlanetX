import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import A from 'aladin-lite';
import { SPHEREX_SKY, resolveSkyUrl } from '../lib/surveys.js';
import { COVERAGE_LAYERS } from '../lib/crawl.js';

const C = 299792458;

/**
 * Aladin's renderer clears to a neutral grey regardless of its backgroundColor option, which
 * leaves a grey frame around the all-sky ellipse. Swap that neutral grey for black (the patch
 * stays installed; this app has no other WebGL user). Every other clear colour passes through.
 */
function withBlackClear(create) {
  const proto = window.WebGL2RenderingContext?.prototype;
  if (!proto) return create();
  const clear = proto.clearColor;
  proto.clearColor = function clearColor(r, g, b, a) {
    const grey = r === g && g === b && r > 0.05 && r < 0.2;
    return grey ? clear.call(this, 0, 0, 0, a) : clear.call(this, r, g, b, a);
  };
  return create();
}

/**
 * Show one frame of a HiPS cube. Aladin 3.8.2's setSliceNumber() maps slices through wavelength
 * while its renderer indexes frames linearly in frequency, so for this cube it lands on the wrong
 * (sometimes blank) frame. We hand the renderer the frequency that indexes the frame directly.
 * Relies on Aladin internals; the dependency is pinned to an exact version for that reason.
 * Returns false until the layer's properties have loaded.
 */
function showCubeFrame(layer, frame) {
  if (!layer?.added || !layer.view?.wasm || !layer.emMin || !layer.emMax || !layer.cubeDepth) return false;
  const fMin = C / layer.emMax, fMax = C / layer.emMin;
  layer.view.wasm.setFreq(layer.layer, fMin + ((frame + 0.5) / layer.cubeDepth) * (fMax - fMin));
  layer.view.requestRedraw?.();
  return true;
}

/**
 * Aladin Lite v3 sky with its built-in UI switched off; the app draws its own controls.
 * Reports view changes, cursor moves and non-drag clicks (with canvas pixel coords).
 * `coverage` is optional { done, failed, queued }, each a MOC JSON ({ "<order>": [ipix, ...] }) or
 * null, drawn on the sky: the offline survey's patches by status (COVERAGE_LAYERS).
 */
export default function SkyMap({ initial, frame, grid, coverage, onReady, onViewChange, onPointer, onSkyClick, onSurveyError, onLoading }) {
  const divRef = useRef(null);
  const aladinRef = useRef(null);
  const layerRef = useRef(null);
  const frameRef = useRef(frame);
  const cb = useRef({});
  // Keep the latest callbacks/frame for Aladin's long-lived event handlers.
  useLayoutEffect(() => {
    frameRef.current = frame;
    cb.current = { onReady, onViewChange, onPointer, onSkyClick, onSurveyError, onLoading };
  });
  const retry = useRef(0);
  const [fatal, setFatal] = useState(null);

  // Apply the current cube frame, retrying until the layer's properties have arrived.
  function applyFrame(attempt = 0) {
    clearTimeout(retry.current);
    const l = layerRef.current;
    if (!l?.cube) return;
    if (!showCubeFrame(l.layer, frameRef.current) && attempt < 60) {
      retry.current = setTimeout(() => applyFrame(attempt + 1), 150);
    }
  }
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    A.init.then(() => {
      if (cancelled || aladinRef.current || !divRef.current) return;
      const aladin = withBlackClear(() => A.aladin(divRef.current, {
        // Orthographic: at wide zoom the sky reads as a globe seen from outside.
        projection: 'SIN',
        cooFrame: 'ICRSd',
        fov: initial.fov,
        target: `${initial.ra} ${initial.dec}`,
        hipsList: [],
        log: false,
        backgroundColor: 'rgb(0, 0, 0)',
        showReticle: false,
        showZoomControl: false,
        showFullscreenControl: false,
        showLayersControl: false,
        showGotoControl: false,
        showProjectionControl: false,
        showSimbadPointerControl: false,
        showCooGridControl: false,
        showShareControl: false,
        showSettingsControl: false,
        showContextMenu: false,
        showFrame: false,
        showFov: false,
        showCooLocation: false,
        showStatusBar: false,
        gridOptions: { enabled: false, color: 'rgb(255, 255, 255)', opacity: 0.18, showLabels: true, labelSize: 11, thickness: 1 },
      }));
      aladinRef.current = aladin;
      const report = () => {
        const [ra, dec] = aladin.getRaDec();
        cb.current.onViewChange?.({ ra: ((ra % 360) + 360) % 360, dec, fov: aladin.getFov()[0] });
      };
      aladin.on('positionChanged', report);
      aladin.on('zoomChanged', report);
      aladin.on('mouseMove', (e) => cb.current.onPointer?.(e && Number.isFinite(e.ra) ? e : null));
      aladin.on('click', (e) => {
        if (!e || e.isDragging || !Number.isFinite(e.ra) || !Number.isFinite(e.dec)) return;
        cb.current.onSkyClick?.({ ra: ((e.ra % 360) + 360) % 360, dec: e.dec, x: e.x, y: e.y });
      });
      divRef.current.addEventListener('mouseleave', () => cb.current.onPointer?.(null));
      divRef.current.addEventListener('AL:loading.started', () => cb.current.onLoading?.(true));
      divRef.current.addEventListener('AL:loading.stopped', () => cb.current.onLoading?.(false));
      setReady(true);
      cb.current.onReady?.({ aladin, el: divRef.current });
      report();
    }).catch((err) => {
      if (!cancelled) setFatal(String(err?.message || err));
    });
    return () => { cancelled = true; };
  }, [initial]);

  // The SPHEREx cube: one layer for good; frame changes only move the slice.
  useEffect(() => {
    const aladin = aladinRef.current;
    if (!ready || !aladin) return undefined;
    let stale = false;
    resolveSkyUrl().then((url) => {
      if (stale) return;
      const layer = A.HiPS(url, { name: SPHEREX_SKY.label });
      aladin.setBaseImageLayer(layer);
      layerRef.current = { layer, cube: true };
      applyFrame();
    }).catch((err) => {
      if (!stale) cb.current.onSurveyError?.(err.message);
    });
    return () => { stale = true; clearTimeout(retry.current); };
  }, [ready]);

  useEffect(() => { applyFrame(); }, [frame]);

  useEffect(() => {
    if (ready) aladinRef.current?.setCooGrid({ enabled: !!grid });
  }, [grid, ready]);

  useEffect(() => {
    const aladin = aladinRef.current;
    if (!ready || !aladin || !coverage) return undefined;
    // Drawn in this order, so finished and failed patches sit over the queued outlines.
    const mocs = COVERAGE_LAYERS.filter((l) => coverage[l.key]).map((l) => A.MOCFromJSON(coverage[l.key], l.style));
    mocs.forEach((moc) => aladin.addMOC(moc));
    return () => mocs.forEach((moc) => aladin.removeOverlay(moc));
  }, [coverage, ready]);

  return (
    <>
      <div ref={divRef} className="sky" aria-label="Sky map. Click to explore a position." />
      {fatal && (
        <div className="fatal" role="alert">
          <p>The sky map needs WebGL2, which this browser does not provide.</p>
          <p className="dim">{fatal}</p>
        </div>
      )}
    </>
  );
}
