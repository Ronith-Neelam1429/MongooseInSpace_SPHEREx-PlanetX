# SPHEREx data API (frontend guide)

Base URL for local development: `http://127.0.0.1:8000` (run
`uv run uvicorn planetx.api.app:app --reload`). Interactive docs: `/docs`.
All angles are in degrees (ICRS), times are MJD (UTC) plus a `YYYY-MM-DD` date, and
wavelengths are in microns. Response shapes are in `backend/planetx/api/schemas.py`.

## Typical flow

1. `GET /api/frames?ra=&dec=&size_deg=` lists the frames for a position.
2. For each frame, load `image_url` (PNG) for display, or `data_url` (float JSON)
   for blink, stack or difference on the client.
3. All frames for one `(ra, dec, size_deg)` share **one** grid: north up, east left,
   ~6.15"/px. Pixel `(row, col)` is the same sky spot in every frame.

## Endpoints

### `GET /api/visits?ra=&dec=&radius_arcsec=`
SPHEREx exposures from `spherex_dr1` and `spherex_qr3` whose images cover the cone.
- `radius_arcsec`: default 10, range 0.1-300.
- Returns `VisitsResponse`. `visits` is sorted by `mjd`. `passes` groups visits into
  survey passes; a new pass starts after a gap of more than 30 days.
- `obs_id` is shared by the detectors of one exposure. `(obs_id, detector)` is unique,
  and so is `access_url` (use it as a React key).
- `wavelength_min_um`/`wavelength_max_um` cover the **whole detector's** band, not
  the band at the target.

```sh
curl 'http://127.0.0.1:8000/api/visits?ra=269.4485&dec=4.7394'
# Barnard's Star: 364 visits in 3 passes (2025-08..10, 2026-03..05, 2026-08..09)
```

### `GET /api/frames?ra=&dec=&size_deg=&detector=`
The visits within 10" of `(ra, dec)`, each with ready-made cutout URLs.
- `size_deg`: default 0.1, range 0.01-0.5. This is the side of the square grid.
- `detector`: optional, 1-6. Each detector covers one wavelength range (from SIA, in um):
  D1 0.75-1.12, D2 1.10-1.65, D3 1.63-2.43, D4 2.40-3.85, D5 3.81-4.43, D6 4.41-5.01.
- `image_url` and `data_url` are root-relative (`/api/cutout.png?...`) and already
  URL-encoded. Prefix them with the API origin.
- `wavelength_um` is always `null` here. The band at the target is known only once
  the cutout loads: read it from `CutoutData.wavelength_um`.
- **Cost**: each frame is a separate ~4 MB download from IRSA on first use. A
  position typically has about 360 frames (about 60 per detector). Load lazily, filter
  by `detector`, and keep at most about 4 requests in flight. The server allows 4
  concurrent IRSA downloads and answers **503 with `Retry-After`** when it is
  saturated. Repeat requests are served from the disk cache in about 50 ms.

```sh
curl 'http://127.0.0.1:8000/api/frames?ra=269.4485&dec=4.7394&size_deg=0.1&detector=3'
```

### `GET /api/cutout.png?url=&ra=&dec=&size_deg=&vmin=&vmax=`
One frame resampled onto the common grid, as an 8-bit grayscale + alpha PNG with an
asinh stretch.
- `url` must be an `access_url` from `/api/visits` or `/api/frames`. Any other URL
  gets a 400.
- **No-data or masked pixels are transparent** (alpha 0).
- By default each frame is stretched to its own 0.5-99.5 percentiles, so brightness
  is not comparable between frames. For a fair blink, pass the same `vmin`/`vmax`
  (in `bunit`, MJy/sr) for every frame, for example values taken from one frame's
  JSON.
- Responses carry `Cache-Control: public, max-age=86400`.

```sh
curl -o frame.png 'http://127.0.0.1:8000/api/cutout.png?url=https%3A%2F%2Firsa.ipac.caltech.edu%2Fibe%2Fdata%2Fspherex%2Fdr1%2Flevel2%2F2025W33_2C%2Fl2b-v27-2026-228%2F3%2Flevel2_2025W33_2C_0321_1D3_spx_l2b-v27-2026-228.fits&ra=269.4485&dec=4.7394&size_deg=0.1'
```

### `GET /api/cutout.json?url=&ra=&dec=&size_deg=`
The same frame as float pixels (`CutoutData`): `width`, `height`, `ra`, `dec`,
`pixel_scale_arcsec`, `mjd`, `wavelength_um` (band centre at the target),
`bunit`, `data_b64`.

`data_b64` is base64 of little-endian float32 values, row-major, `height` rows of
`width` values. **Row 0 is the top (north) and column 0 the left (east)**, the same
layout as canvas `ImageData`. `NaN` means no data or a masked pixel.

```js
async function loadCutout(apiOrigin, dataUrl) {
  const c = await (await fetch(apiOrigin + dataUrl)).json();
  const bytes = Uint8Array.from(atob(c.data_b64), ch => ch.charCodeAt(0));
  // Float32Array uses the platform byte order; every browser platform is little-endian.
  const pixels = new Float32Array(bytes.buffer);  // length = c.width * c.height
  const at = (row, col) => pixels[row * c.width + col];  // NaN = no data
  return { ...c, pixels, at };
}
// Difference of two frames on the shared grid: a[i] - b[i], skip NaN.
```

## Errors

| Status | Meaning |
|-------:|---------|
| 400 | `url` is not an allowed IRSA SPHEREx file URL, or `vmax <= vmin` |
| 404 | IRSA has no cutout for this file at this position (e.g. outside the file) |
| 422 | Parameter missing or out of range (FastAPI validation body) |
| 502 | IRSA failed after retries (`detail` gives the reason) |
| 503 | Too many IRSA downloads in flight; retry after `Retry-After` seconds |

## Pixel masking

The display products set to NaN only the pixels flagged NONFUNC, MISSING_DATA, HOT
or COLD, plus non-finite pixels. IRSA's photometry tool rejects more flags
(`PHOTOMETRY_BAD_FLAGS` in `planetx/cutouts.py`, which include BLOOM and SUR_ERROR).
Those flags also cover the cores of bright stars such as Barnard's Star, so they are
not applied to the images. The detection pipeline should apply them.
