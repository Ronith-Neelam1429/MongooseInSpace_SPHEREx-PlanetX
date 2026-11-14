import { useEffect, useRef, useState } from 'react';
import A from 'aladin-lite';
import { resolveSurveyUrl } from '../lib/surveys.js';

/**
 * Full-sky Aladin Lite v3 map. Calls onReady({ aladin, A }) once, then reports
 * view changes (onViewChange) and clicks that were not drags (onSkyClick).
 */
export default function SkyMap({ initial, survey, onReady, onViewChange, onSkyClick }) {
  const divRef = useRef(null);
  const cb = useRef({});
  cb.current = { onReady, onViewChange, onSkyClick };
  const aladinRef = useRef(null);
  const appliedSurvey = useRef(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([A.init, resolveSurveyUrl(survey)]).then(([, surveyUrl]) => {
      if (cancelled || aladinRef.current || !divRef.current) return;
      appliedSurvey.current = survey;
      const aladin = A.aladin(divRef.current, {
        survey: surveyUrl,
        hipsList: [],
        log: false,
        projection: 'AIT',
        cooFrame: 'ICRSd',
        fov: initial.fov,
        target: `${initial.ra} ${initial.dec}`,
        showReticle: true,
        showZoomControl: true,
        showFullscreenControl: false,
        showLayersControl: false,
        showGotoControl: false,
        showProjectionControl: false,
        showSimbadPointerControl: false,
        showCooGridControl: true,
        showShareControl: false,
        showSettingsControl: false,
        showContextMenu: false,
        showFrame: true,
        showFov: true,
        showCooLocation: true,
      });
      aladinRef.current = aladin;
      const report = () => {
        const [ra, dec] = aladin.getRaDec();
        cb.current.onViewChange?.({ ra, dec, fov: aladin.getFov()[0] });
      };
      aladin.on('positionChanged', report);
      aladin.on('zoomChanged', report);
      aladin.on('click', (e) => {
        if (!e || e.isDragging || !Number.isFinite(e.ra) || !Number.isFinite(e.dec)) return;
        cb.current.onSkyClick?.(((e.ra % 360) + 360) % 360, e.dec);
      });
      cb.current.onReady?.({ aladin, A });
      report();
    }).catch((err) => {
      if (!cancelled) setError(String(err?.message || err));
    });
    return () => { cancelled = true; };
    // Map is created once; later prop changes are applied by the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!aladinRef.current || !survey || appliedSurvey.current === survey) return undefined;
    let stale = false;
    appliedSurvey.current = survey;
    resolveSurveyUrl(survey).then((url) => {
      if (!stale) aladinRef.current.setBaseImageLayer(url);
    });
    return () => { stale = true; };
  }, [survey]);

  return (
    <div className="skymap-wrap">
      <div ref={divRef} className="skymap" aria-label="Interactive sky map" />
      {error && (
        <div className="map-error" role="alert">
          The sky map could not start: {error}. A browser with WebGL2 is required.
        </div>
      )}
    </div>
  );
}
