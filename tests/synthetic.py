"""Synthetic SPHEREx MEF cutouts with a known WCS, for offline tests."""
import io

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS


SCALE_DEG = 6.15 / 3600
SHAPE = (61, 61)  # input image (rows, cols)
# SIP constant offset (pixels). Real SPHEREx headers carry A_0_0/B_0_0 terms; the
# wavelength lookup must ignore them.
SIP_A00 = 3.0


def synthetic_header(ra: float, dec: float, rotation_deg: float = 30.0) -> fits.Header:
    """IMAGE header like SPHEREx's: TAN-SIP, rotated, with the x-axis parity of the
    real files (PC1_1 > 0: RA grows to the right), plus the W wavelength WCS."""
    theta = np.radians(rotation_deg)
    rot = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    pc = rot @ np.diag([1.0, -1.0])  # flipped parity, then rotated
    h = fits.Header()
    h["WCSAXES"] = 2
    h["CTYPE1"], h["CTYPE2"] = "RA---TAN-SIP", "DEC--TAN-SIP"
    h["CRVAL1"], h["CRVAL2"] = ra, dec
    h["CRPIX1"], h["CRPIX2"] = (SHAPE[1] + 1) / 2, (SHAPE[0] + 1) / 2
    h["CDELT1"] = h["CDELT2"] = SCALE_DEG
    h["CUNIT1"] = h["CUNIT2"] = "deg"
    for i in range(2):
        for j in range(2):
            h[f"PC{i + 1}_{j + 1}"] = pc[i, j]
    h["A_ORDER"] = h["B_ORDER"] = 2
    h["A_0_0"], h["B_0_0"] = SIP_A00, 0.0
    h["A_2_0"] = h["B_0_2"] = 1e-5
    h["RADESYS"] = "ICRS"
    h["MJD-AVG"] = 60902.5
    h["BUNIT"] = "MJy / sr"
    h["DETECTOR"] = 3
    # Wavelength lookup: same card layout as the live files (see WCS-WAVE table).
    h["WCSAXESW"] = 2
    for n in (1, 2):
        h[f"CTYPE{n}W"] = "WAVE-TAB"
        h[f"CRPIX{n}W"], h[f"CDELT{n}W"], h[f"CRVAL{n}W"] = 1.0, 1.0, 1.0
        h[f"CUNIT{n}W"] = "um"
        h[f"PS{n}_0W"], h[f"PS{n}_1W"] = "WCS-WAVE", "VALUES"
    h["PV1_3W"], h["PV2_3W"] = 1, 2
    h["PS1_2W"], h["PS2_2W"] = "X", "Y"
    return h


def wavelength_at_raw_pixel(x0: float) -> float:
    """Synthetic table: wavelength = 1 + 0.001 * (1-based pixel x)."""
    return 1.0 + 0.001 * (x0 + 1)


def _wave_table() -> fits.BinTableHDU:
    nodes = np.linspace(1, 2040, 9).astype(np.int32)
    values = np.empty((1, 9, 9, 2), dtype=np.float32)
    values[0, :, :, 0] = 1.0 + 0.001 * nodes[np.newaxis, :]  # varies along X only
    values[0, :, :, 1] = 0.04
    return fits.BinTableHDU.from_columns([
        fits.Column(name="X", format="9J", dim="(9)", array=nodes[np.newaxis]),
        fits.Column(name="Y", format="9J", dim="(9)", array=nodes[np.newaxis]),
        fits.Column(name="VALUES", format="162E", dim="(2,9,9)", array=values),
    ], name="WCS-WAVE")


def make_mef(ra: float, dec: float, sources=(), flags=None, rotation_deg: float = 30.0) -> bytes:
    """Synthetic cutout: Gaussian point sources (sigma 1.5 px) at given (ra, dec, amp)."""
    header = synthetic_header(ra, dec, rotation_deg)
    wcs = WCS(header)
    yy, xx = np.indices(SHAPE)
    image = np.zeros(SHAPE, dtype=np.float32)
    for s_ra, s_dec, amp in sources:
        x0, y0 = wcs.world_to_pixel_values(s_ra, s_dec)
        image += amp * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2) / (2 * 1.5**2))
    hdul = fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(image, header=header, name="IMAGE"),
        fits.ImageHDU(np.zeros(SHAPE, np.int32) if flags is None else flags, name="FLAGS"),
        fits.ImageHDU(np.ones(SHAPE, np.float32), name="VARIANCE"),
        _wave_table(),
    ])
    buf = io.BytesIO()
    hdul.writeto(buf)
    return buf.getvalue()
