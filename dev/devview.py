"""Barebones dev viewer: run the motion engine on a target and write an HTML page.

    uv run python dev/devview.py barnard
    uv run python dev/devview.py r-dra --detectors 1,2,3 --open
    uv run python dev/devview.py "269.45 4.77" --size 0.08

The page blinks between survey passes (median of each pass's frames), can show
each pass minus the first, draws movers' tracks and marks variables. It's a
development tool for checking the engine, not the product UI (frontend/).
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import subprocess
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from astropy import log  # noqa: E402
from astropy.io import fits  # noqa: E402
from astropy.time import Time  # noqa: E402
from astropy.wcs import WCS  # noqa: E402
from PIL import Image  # noqa: E402
from reproject import reproject_interp  # noqa: E402

from fetch import TARGETS, fetch_all, find_visits, pick_visits, resolve  # noqa: E402
from planetx.engine import EngineResult, find_motion, frame_from_hdul  # noqa: E402
from planetx.engine.detect import frame_background  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
DISPLAY_SCALE_ARCSEC = 3.0  # finer than SPHEREx's 6.15"/px so markers sit cleanly


def display_wcs(ra: float, dec: float, size_deg: float) -> tuple[WCS, tuple[int, int]]:
    """North-up, east-left TAN grid centred on the target."""
    n = int(round(size_deg * 3600 / DISPLAY_SCALE_ARCSEC))
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [(n + 1) / 2, (n + 1) / 2]
    w.wcs.cdelt = [-DISPLAY_SCALE_ARCSEC / 3600, DISPLAY_SCALE_ARCSEC / 3600]
    return w, (n, n)


def pass_images(result: EngineResult, wcs: WCS, shape: tuple[int, int]) -> list[np.ndarray]:
    """Median of each pass's frames, each frame first scaled to noise units so
    frames at very different wavelengths combine sensibly."""
    images = []
    for ids in result.passes:
        stack = []
        for i in ids:
            f = result.frames[i]
            bkg, std = frame_background(f)
            norm = (f.data - bkg) / std
            arr, _ = reproject_interp((norm, f.wcs), wcs, shape_out=shape)
            stack.append(arr)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels
            images.append(np.nanmedian(np.array(stack), axis=0))
    return images


def to_png(img: np.ndarray, lo: float, hi: float, diverging: bool = False) -> str:
    """Base64 PNG; flipped so north is at the top."""
    if diverging:
        x = np.clip(img / hi, -1, 1)
        x = np.nan_to_num(x)
        r = np.where(x > 0, 255, 255 * (1 + x))
        b = np.where(x < 0, 255, 255 * (1 - x))
        g = 255 * (1 - np.abs(x))
        rgb = np.stack([r, g, b], -1)
    else:
        x = np.arcsinh((img - lo) / (hi - lo) * 10) / np.arcsinh(10)
        x = np.clip(np.nan_to_num(x, nan=0.0), 0, 1) * 255
        rgb = np.stack([x, x, x], -1)
    im = Image.fromarray(np.flipud(rgb).astype(np.uint8))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def sky_to_svg(wcs: WCS, shape, ra: float, dec: float) -> tuple[float, float]:
    x, y = wcs.all_world2pix(ra, dec, 0)
    return float(x), float(shape[0] - 1 - y)


def build_page(target: str, coord, result: EngineResult, size_deg: float, detectors) -> str:
    wcs, shape = display_wcs(coord.ra.deg, coord.dec.deg, size_deg)
    imgs = pass_images(result, wcs, shape)
    allpix = np.concatenate([i[np.isfinite(i)] for i in imgs])
    lo, hi = np.percentile(allpix, 5), np.percentile(allpix, 99.7)
    diffs = [i - imgs[0] for i in imgs]
    dhi = max(np.nanpercentile(np.abs(np.concatenate([d[np.isfinite(d)] for d in diffs[1:]])), 99.5), 1e-6) if len(imgs) > 1 else 1.0
    pngs = [to_png(i, lo, hi) for i in imgs]
    dpngs = [to_png(d, 0, dhi, diverging=True) for d in diffs]

    def mjd_str(m):
        return Time(m, format="mjd").iso[:10]

    pass_labels = []
    for k, ids in enumerate(result.input_passes):
        m = [result.input_frames[i].mjd for i in ids]
        pass_labels.append(f"Pass {k + 1}: {mjd_str(min(m))} to {mjd_str(max(m))} ({len(ids)} frames{', stacked' if result.stacked else ''})")

    # Overlays: per pass, plus tracks.
    svg_tracks = []
    svg_points: list[list[str]] = [[] for _ in result.passes]
    for o in result.movers:
        pts = [sky_to_svg(wcs, shape, s.ra, s.dec) for s in o.sources]
        svg_tracks.append(
            '<polyline class="track" points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>'
        )
        for s, (x, y) in zip(o.sources, pts):
            svg_points[s.pass_index].append(f'<circle class="mover" cx="{x:.1f}" cy="{y:.1f}" r="5"/>')
    for o in result.variables:
        for s in o.sources:
            x, y = sky_to_svg(wcs, shape, s.ra, s.dec)
            svg_points[s.pass_index].append(f'<rect class="variable" x="{x - 6:.1f}" y="{y - 6:.1f}" width="12" height="12"/>')
    statics = []
    for o in result.objects:
        if not o.is_mover and not o.is_variable:
            x, y = sky_to_svg(wcs, shape, o.ra, o.dec)
            statics.append(f'<circle class="static" cx="{x:.1f}" cy="{y:.1f}" r="3"/>')

    rows = []
    for o in result.movers:
        rows.append(
            f"<tr><td>mover</td><td>{o.ra:.5f} {o.dec:+.5f}</td><td>{o.pm_total:.2f} ± {o.pm_err:.2f}\"/yr"
            + (f"<br><span class=muted>parallax {o.parallax:.2f} ± {o.parallax_err:.2f}\"</span>" if o.parallax is not None else "")
            + "</td>"
            f"<td>{o.position_angle_deg:.0f}°</td><td>{o.pm_significance:.1f}σ</td><td>{o.chi2_dof:.2f}</td>"
            f"<td>{len(o.sources)}</td><td>{'yes' if any(d.saturated for s in o.sources for d in s.detections) else ''}</td></tr>"
        )
    # Near-misses worth a human look: tracked in 3+ passes, moved > 1 px,
    # 3-5 sigma (e.g. WISE 0855 in stacks).
    pix = float(np.median([f.pixel_scale_arcsec for f in result.frames]))
    candidates = [
        o for o in result.objects
        if not o.is_mover and len(o.sources) >= 3 and o.pm_significance >= 3
        and o.pm_total * (o.sources[-1].mjd - o.sources[0].mjd) / 365.25 >= pix
    ]
    for o in candidates:
        rows.append(
            f"<tr><td>candidate</td><td>{o.ra:.5f} {o.dec:+.5f}</td><td>{o.pm_total:.2f} ± {o.pm_err:.2f}\"/yr</td>"
            f"<td>{o.position_angle_deg:.0f}°</td><td>{o.pm_significance:.1f}σ</td><td>{o.chi2_dof:.2f}</td>"
            f"<td>{len(o.sources)}</td><td></td></tr>"
        )
        pts = [sky_to_svg(wcs, shape, s_.ra, s_.dec) for s_ in o.sources]
        svg_tracks.append('<polyline class="track cand" points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>')
        for s_, (x, y) in zip(o.sources, pts):
            svg_points[s_.pass_index].append(f'<circle class="cand" cx="{x:.1f}" cy="{y:.1f}" r="5"/>')
    for o in sorted(result.variables, key=lambda o: -o.variability["significance"]):
        v = o.variability
        flux = f"{v['change']} ({v['n_pairs']} same-wavelength pairs)"
        if "caution" in v:
            flux += f" <em class=\"muted\">⚠ {html.escape(v['caution'])}</em>"
        rows.append(
            f"<tr><td>variable</td><td>{o.ra:.5f} {o.dec:+.5f}</td><td colspan=2>{v['detector']} {flux}</td>"
            f"<td>{v['significance']:.1f}σ</td><td>×{v['ratio']:.2f}</td><td>{len(o.sources)}</td><td></td></tr>"
        )
    table = "\n".join(rows) or '<tr><td colspan="8">Nothing moving or varying above threshold.</td></tr>'
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in result.notes)
    n, _ = shape
    view = 512

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Engine dev view: {html.escape(target)}</title>
<style>
:root {{ --bg:#0d1117; --fg:#e6edf3; --muted:#8b949e; --line:#30363d; --mover:#ff7b72; --var:#e3b341; --static:#58a6ff; }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui,sans-serif; }}
h1 {{ font-size:18px; margin:0 0 4px; }} .muted {{ color:var(--muted); }}
.wrap {{ display:flex; gap:20px; flex-wrap:wrap; align-items:flex-start; }}
.sky {{ position:relative; width:min({view}px,100%); aspect-ratio:1; border:1px solid var(--line); }}
.sky img, .sky svg {{ position:absolute; inset:0; width:100%; height:100%; image-rendering:pixelated; }}
.track {{ fill:none; stroke:var(--mover); stroke-width:1.2; stroke-dasharray:3 2; }}
.mover {{ fill:none; stroke:var(--mover); stroke-width:1.5; }}
.variable {{ fill:none; stroke:var(--var); stroke-width:1.5; }}
.cand {{ fill:none; stroke:#d2a8ff; stroke-width:1.5; }} .track.cand {{ stroke:#d2a8ff; }}
.static {{ fill:none; stroke:var(--static); stroke-width:.8; opacity:.6; }}
.controls {{ display:flex; gap:8px; flex-wrap:wrap; margin:10px 0; align-items:center; }}
button {{ background:#21262d; color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:5px 10px; cursor:pointer; }}
button.on {{ background:#1f6feb; border-color:#1f6feb; }}
table {{ border-collapse:collapse; margin-top:8px; }} td,th {{ border-bottom:1px solid var(--line); padding:4px 8px; text-align:left; font-variant-numeric:tabular-nums; }}
.key span {{ margin-right:14px; }}
</style></head><body>
<h1>{html.escape(target)} <span class="muted">· RA {coord.ra.deg:.5f} Dec {coord.dec.deg:+.5f} · {size_deg}° · detectors {",".join(map(str, detectors))}</span></h1>
<div class="muted">North up, east left. Each pass = median of its frames (noise-normalised). Engine: {len(result.input_frames)} frames{' (stacked per pass)' if result.stacked else ''}, {len(result.objects)} tracked objects, systematic floor {result.pm_floor_arcsec_yr:.2f}"/yr.</div>
<div class="controls">
  <button id="play" class="on">Pause</button>
  {''.join(f'<button class="pass" data-i="{k}">P{k + 1}</button>' for k in range(len(pngs)))}
  <button id="diff">Difference vs P1</button>
  <button id="ov" class="on">Overlays</button>
  <button id="st">Static stars</button>
  <label class="muted">speed <input id="speed" type="range" min="200" max="2000" value="700" step="100"></label>
</div>
<div class="wrap">
  <div>
    <div class="sky">
      <img id="img" alt="sky patch">
      <svg viewBox="0 0 {n} {n}" preserveAspectRatio="none">
        <g id="tracks">{''.join(svg_tracks)}</g>
        {''.join(f'<g class="pp" data-i="{k}">{"".join(p)}</g>' for k, p in enumerate(svg_points))}
        <g id="statics" style="display:none">{''.join(statics)}</g>
      </svg>
    </div>
    <div id="label" class="muted"></div>
    <div class="key muted"><span style="color:var(--mover)">○ mover / track</span><span style="color:#d2a8ff">○ candidate (3-5σ)</span><span style="color:var(--var)">□ variable</span><span style="color:var(--static)">○ static</span></div>
  </div>
  <div>
    <table><tr><th>type</th><th>RA Dec (deg)</th><th>motion</th><th>PA</th><th>signif.</th><th>χ²/dof · ratio</th><th>passes</th><th>saturated</th></tr>
    {table}</table>
    <ul class="muted">{notes}</ul>
  </div>
</div>
<script>
const imgs = {pngs!r}, diffs = {dpngs!r}, labels = {pass_labels!r};
let i = 0, playing = true, diff = false, timer = null;
const img = document.getElementById('img'), label = document.getElementById('label');
function show() {{
  img.src = 'data:image/png;base64,' + (diff ? diffs : imgs)[i];
  label.textContent = labels[i] + (diff ? ' minus pass 1 (red = brighter, blue = fainter)' : '');
  document.querySelectorAll('.pp').forEach(g => g.style.display = (+g.dataset.i === i) ? '' : 'none');
  document.querySelectorAll('button.pass').forEach(b => b.classList.toggle('on', +b.dataset.i === i));
}}
function tick() {{ i = (i + 1) % imgs.length; show(); }}
function restart() {{ clearInterval(timer); if (playing) timer = setInterval(tick, +document.getElementById('speed').value); }}
document.getElementById('play').onclick = e => {{ playing = !playing; e.target.textContent = playing ? 'Pause' : 'Play'; e.target.classList.toggle('on', playing); restart(); }};
document.querySelectorAll('button.pass').forEach(b => b.onclick = () => {{ i = +b.dataset.i; playing = false; document.getElementById('play').textContent = 'Play'; document.getElementById('play').classList.remove('on'); restart(); show(); }});
document.getElementById('diff').onclick = e => {{ diff = !diff; e.target.classList.toggle('on', diff); show(); }};
document.getElementById('ov').onclick = e => {{ const on = e.target.classList.toggle('on'); document.querySelector('.sky svg').style.display = on ? '' : 'none'; }};
document.getElementById('st').onclick = e => {{ const on = e.target.classList.toggle('on'); document.getElementById('statics').style.display = on ? '' : 'none'; }};
document.getElementById('speed').oninput = restart;
show(); restart();
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help=f"one of {', '.join(TARGETS)}, a SIMBAD name, or 'ra dec' in degrees")
    ap.add_argument("--detectors", help="comma-separated detector numbers 1-6 (default: per target)")
    ap.add_argument("--per-pass", type=int, default=6, help="max frames per pass per detector (default 6)")
    ap.add_argument("--size", type=float, default=0.1, help="cutout size in degrees (default 0.1)")
    ap.add_argument("--refresh", action="store_true", help="re-query IRSA for new visits")
    ap.add_argument("--stack", action="store_true", help="detect on per-pass co-adds (deeper; for faint targets)")
    ap.add_argument("--open", action="store_true", help="open the page when done (macOS)")
    args = ap.parse_args()
    log.setLevel("WARNING")

    coord, detectors = resolve(args.target)
    if args.detectors:
        detectors = tuple(int(d) for d in args.detectors.split(","))
        if not all(1 <= d <= 6 for d in detectors):
            ap.error("detectors must be between 1 and 6")
    print(f"{args.target}: RA {coord.ra.deg:.5f} Dec {coord.dec.deg:+.5f}, detectors {detectors}")
    visits = pick_visits(find_visits(coord, detectors, refresh=args.refresh), args.per_pass)
    if not visits:
        sys.exit("no SPHEREx visits found here")
    print(f"fetching {len(visits)} cutouts ...")
    frames = []
    for v, p in fetch_all(visits, coord, args.size):
        with fits.open(p) as h:
            frames.append(frame_from_hdul(h, v.obs_id))
    result = find_motion(frames, stack=args.stack)
    print(f"passes {[len(p) for p in result.passes]}; {len(result.movers)} movers, {len(result.variables)} variables")
    for o in result.movers:
        print(f"  mover {o.pm_total:.2f}\"/yr PA {o.position_angle_deg:.0f} at {o.pm_significance:.1f}σ")
    OUT.mkdir(exist_ok=True)
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in args.target)
    path = OUT / f"{slug}.html"
    path.write_text(build_page(args.target, coord, result, args.size, detectors))
    print(f"wrote {path}")
    if args.open:
        subprocess.run(["open", str(path)], check=False)


if __name__ == "__main__":
    main()
