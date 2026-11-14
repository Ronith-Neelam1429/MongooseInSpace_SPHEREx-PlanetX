# SPHEREx PlanetX: Sky Explorer (frontend)

Full-sky Aladin Lite v3 map. Click anywhere to center and zoom, and an info panel opens for that spot:

- **Coordinates**: RA/Dec in decimal and sexagesimal, plus galactic and ecliptic. Ecliptic latitude sets how often SPHEREx revisits a spot.
- **Known objects (SIMBAD)**: name, type and magnitude. Positions are moved to the current epoch using proper motion. Click a row to center on it.
- **Gaia DR3 stars**: drawn with proper-motion arrows (pink if ≥ 100 mas/yr), with a list of the fastest movers.
- **SPHEREx visits**: calls the backend `GET /visits?ra=&dec=` (placeholder until the API exists).

You can also search by object name or coordinates, use the shortcuts for the 5 test targets, switch the background (AllWISE / 2MASS / DSS2) and turn the overlays on or off.
The view is kept in the URL (`?ra=&dec=&fov=`, or `?target=<name>`), so any link restores it.

All catalog queries run in the browser. Each service has mirrors, tried in order: SIMBAD TAP (CDS → Harvard), VizieR and Sesame (CDS → Harvard), HiPS tiles (IRSA → ESA → CDS).
If one data centre is down, the map keeps working.

## Commands
```bash
npm install
npm run dev       # http://localhost:5173
npm test          # unit tests (coordinates, parsing, URL state)
npm run lint
npm run build     # static site in dist/ (relative base, works on GitHub Pages or Vercel)
```

## Backend contract (proposed)
Set `VITE_API_BASE` (see `.env.example`) to the FastAPI base URL.

```
GET /visits?ra=<deg>&dec=<deg>
-> { "visits": [ { "date": "2025-05-12T03:14:00Z", "obs_id": "...", "band": "D3", "detector": 3 } ] }
```
With no `VITE_API_BASE` set, the panel shows a "backend not connected" note.

## Layout
- `src/App.jsx`: app state, search, targets, layer toggles, selection flow
- `src/components/SkyMap.jsx`: Aladin Lite setup and events
- `src/components/InfoPanel.jsx`: side panel (bottom sheet on phones)
- `src/lib/coords.js`: parsing, formatting, galactic/ecliptic conversion, proper-motion propagation
- `src/lib/services.js`: SIMBAD / VizieR / Sesame / backend clients with mirror fallback
- `src/lib/overlays.js`: Aladin overlays (query circle, SIMBAD markers, Gaia arrows)
- `src/lib/surveys.js`, `src/lib/targets.js`, `src/lib/url.js`
