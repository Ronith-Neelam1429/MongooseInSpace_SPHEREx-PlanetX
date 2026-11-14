# PlanetX: SPHEREx sky explorer (frontend)

The SPHEREx sky as a slowly turning globe. Click anywhere (or search) and the camera flies in and
opens that field:

- **Sky**: the SPHEREx DR1 all-sky cube from IRSA, in monochrome, with a wavelength control
  covering all 100 usable frames (0.75–5.0 µm). AllWISE, 2MASS and DSS2 are available for comparison.
- **Proper motion**: Gaia DR3 stars drawn on the sky, sized and shaded by G magnitude. Their
  motion is animated forward from epoch 2016.0, and stars faster than 100 mas/yr are drawn in red.
  The **Movers** toggle fades everything slower and draws each fast star's full predicted path.
  For Barnard's Star, the motion carries the 2016 Gaia position onto the star in the 2025 SPHEREx image.
- **Motion engine**: "Run engine" in the field panel asks the PlanetX API to measure motion and brightness
  changes between SPHEREx passes of the field. Movers, candidates and variables are listed and drawn in cyan
  (a line through the per-pass positions, ending in a ring; variables get a diamond). Red remains Gaia's
  catalogued motion, so the two can be compared directly. Needs `VITE_API_BASE`.
- **Catalogued**: SIMBAD objects, moved to the current epoch using their proper motion.
- **Position**: equatorial, galactic and ecliptic coordinates, plus how often SPHEREx revisits the spot.
- Hover or click any star or object for its details. Search accepts names or coordinates, and the
  five project test targets come up as suggestions. The view is kept in the URL
  (`?ra=&dec=&fov=` or `?target=`). Shortcuts: `/` focuses search, `Esc` closes the field.
  With reduced motion turned on in the OS, the globe stays still, the camera jumps instead of
  flying, and the motion animation starts paused.

All queries run in the browser. Each service has mirrors, tried in order and retried once:
SIMBAD TAP (CDS, Harvard), VizieR and Sesame (CDS, Harvard), HiPS (IRSA, ESA, CDS).

## Commands
```bash
npm install
npm run dev       # http://localhost:5173
npm test          # unit tests
npm run lint
npm run build     # static site in dist/ (relative base: GitHub Pages or Vercel)
```

## SPHEREx visit dates and the motion engine need the backend
IRSA's SIA service sends no CORS headers, so a browser can't list SPHEREx exposures directly.
Set `VITE_API_BASE` (see `.env.example`) once the PlanetX API serves:

```
GET /visits?ra=<deg>&dec=<deg>
-> { "visits": [ { "date": "2025-05-12T03:14:00Z", "obs_id": "...", "band": "D3" } ] }
```
Until then, the field panel says the API is not connected.
The motion engine uses `GET /motion?ra=&dec=&size_deg=` (polled; see `docs/API.md` in the repo root).

## Notes on Aladin Lite
`aladin-lite` is pinned to exactly 3.8.2 because `src/components/SkyMap.jsx` works around two
issues in it: cube frames are selected by frequency (its `setSliceNumber` lands on the wrong frame
for this cube), and the renderer's grey clear colour is replaced with black. Re-check both when upgrading.

## Layout
- `src/App.jsx`: app state, search, layers, selection flow, HUD
- `src/components/SkyMap.jsx`: Aladin setup, survey/cube handling, events
- `src/components/InfoPanel.jsx`: field panel (stacks under the map on phones)
- `src/components/Search.jsx`: name/coordinate combobox with target suggestions
- `src/lib/starfield.js`: canvas layer for stars, proper-motion animation, labels, hit-testing
- `src/lib/camera.js`: fly-to (great-circle path, zoom arc) and the idle globe spin
- `src/lib/services.js`: SIMBAD / VizieR / Sesame / backend clients with mirror fallback
- `src/lib/coords.js`, `surveys.js`, `targets.js`, `url.js`
