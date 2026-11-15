# PlanetX: SPHEREx sky explorer (frontend)

The SPHEREx sky as a slowly turning globe. Click anywhere (or search) and the camera flies in and
opens that patch of sky. The side panel then shows, in this order:

- **A one-line verdict** (`lib/patch.js`): after an engine run what it flagged ("1 candidate · 1
  known mover · 1 variable", or "Nothing moved here"); before, what the catalogues list ("1
  catalogued fast mover · not measured yet"). Under it, "Find what moves", which asks the PlanetX
  API to measure motion and brightness changes between SPHEREx passes (needs `VITE_API_BASE`), or
  its progress.
- **Worth a look**: only the flagged objects. The engine's results by name with what they do in
  words ("9.91″/yr north", drawn in cyan on the map), then catalogued fast movers (over 0.1″/yr,
  in red, the colour of Gaia's catalogued motion) and variable stars from SIMBAD and Gaia DR3,
  each listed once. A play button runs the stars' catalogued motion forward from 2016.0, with a
  "Moving only" switch.
- **More about this patch** (folded): every other known object, every object the engine tracked,
  the engine's notes, coordinates in three systems, SPHEREx pass dates, data credits.

Picking an object, in the list or on the map, opens its card. It starts with the **telescope
pictures** (`SkyPictures`): 2MASS (~2000), WISE (~2010) and SPHEREx (2025 on), oldest first, each
labelled with telescope and date, with a play control that steps through them and a ring where the
object is on that date. Click a picture for a large view (`Esc` closes it).
- A catalogued object, before any run: the archive images come from
  `/motion/evidence/surveys` and one SPHEREx exposure per pass (detector 3, at most 4) from
  `/frames` + `/cutout.png`, all of one patch centred between its 1999 and today positions, so
  they blink in register; the ring is its catalogued (Gaia) motion moved to each date
  (`lib/pictures.js`).
- An engine result: the per-pass SPHEREx medians and the archive images from the evidence
  endpoints, with the measured positions; a difference image beside them.

Then a one-line verdict in plain words, and for engine results the proof checklist, the position
chart and the single exposures; numbers sit in a folded **Details**. "Back to the patch" (or `Esc`)
returns to the list. The panel and the large view follow the system's light or dark theme; the
sky map and the pictures stay dark.

**Names.** An engine object uses the API's `name` and `matches` when the backend ran its catalogue
check. Otherwise `lib/names.js` matches its track against the SIMBAD and Gaia lists already loaded
(each compared at the track's dates, within about one SPHEREx pixel, preferring bright and near):
a SIMBAD name, else `Gaia DR3 <id>`, else "Uncatalogued object".

**Picking on the map.** The few notable objects (engine results, fast movers, bright named
objects) keep a name label; labels are clickable. Notable markers have larger hit targets than
field stars, and a plain field star must be pointed at directly. Hovering a marker lights up its
list row, and hovering or focusing a row rings its marker. A click that misses inside the open
patch does nothing; a click outside the dashed circle opens a new patch there.

Also: a wavelength control over the SPHEREx DR1 all-sky cube (AllWISE, 2MASS and DSS2 for
comparison); a **Survey** layer shading the patches the offline crawler has finished, with a
one-line status; and a **Candidates** list of what that survey has flagged, which flies to the
one you pick. Search accepts names or coordinates. The URL (`?ra=&dec=&fov=` or `?target=`) is the
open patch, so a copied link reopens the same one. Shortcuts: `/` focuses search, `Esc` steps back.
With reduced motion turned on in the OS, the globe stays still and the camera jumps instead of flying.

Catalogue queries run in the browser. Each service has mirrors, tried in order and retried once:
SIMBAD TAP (CDS, Harvard), VizieR and Sesame (CDS, Harvard), HiPS (IRSA, ESA, CDS).

## Commands
```bash
npm install
npm run dev       # http://localhost:5173
npm test          # unit tests
npm run lint
npm run build     # static site in dist/ (relative base: GitHub Pages or Vercel)
```

## What needs the backend
Set `VITE_API_BASE` (see `.env.example`) to the PlanetX API (`docs/API.md` in the repo root):
- `GET /visits` for how often SPHEREx imaged the patch (IRSA's SIA service sends no CORS headers,
  so a browser can't ask it directly),
- `GET /motion` for the engine (polled),
- `GET /survey/status`, `GET /survey/coverage` and `GET /candidates` for the survey layer and list.

Without it the panel says the server is not connected. A server without `/candidates` (404) shows
the list as not available.

## Notes on Aladin Lite
`aladin-lite` is pinned to exactly 3.8.2 because `src/components/SkyMap.jsx` works around two
issues in it: cube frames are selected by frequency (its `setSliceNumber` lands on the wrong frame
for this cube), and the renderer's grey clear colour is replaced with black. Re-check both when upgrading.

## Layout
- `src/App.jsx`: app state, search, layers, selection flow, HUD
- `src/components/SkyMap.jsx`: Aladin setup, survey/cube handling, coverage overlay, events
- `src/components/InfoPanel.jsx`: panel for the open patch (stacks under the map on phones)
- `src/components/ObjectCard.jsx`: card for one selected object: pictures, verdict, proof, details
- `src/components/SkyPictures.jsx`: the telescope pictures viewer (play, step, large view) and
  `KnownPictures` for catalogued objects
- `src/components/MotionEvidence.jsx`, `useEvidence.js`: an engine object's evidence (pictures,
  proof checklist, chart, single exposures) and the hooks that fetch it
- `src/lib/pictures.js`: which SPHEREx exposures to show, catalogue positions per date, picture views
- `src/lib/patch.js`: the patch's one-line verdict and which objects are listed first
- `src/components/CandidatesPanel.jsx`: the survey's candidates list
- `src/components/Search.jsx`: name/coordinate combobox with target suggestions
- `src/lib/starfield.js`: canvas layer for stars, markers, name labels and hit-testing
- `src/lib/names.js`: known-object list and name matching for engine objects
- `src/lib/plain.js`: plain-language wording (directions, speeds, verdicts)
- `src/lib/camera.js`: fly-to (great-circle path, zoom arc) and the idle globe spin
- `src/lib/services.js`: SIMBAD / VizieR / Sesame / backend clients with mirror fallback
- `src/lib/coords.js`, `surveys.js`, `targets.js`, `url.js`
