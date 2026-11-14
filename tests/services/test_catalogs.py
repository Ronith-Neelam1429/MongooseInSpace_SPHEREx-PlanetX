"""Live connection tests for the catalog / small-body / HiPS services.

Every test hits a real external service and checks content against a known
answer (Barnard's Star, WISE 0855, Ceres), not just an HTTP 200.

Gotchas encoded here (observed 2026-11-14):
  * Python.org builds of CPython ship without a usable CA bundle, so
    astroquery.gaia fails TLS verification; conftest.py points SSL_CERT_FILE
    at certifi's bundle (verification stays on) and disables astroquery's
    response cache.
  * SIMBAD, VizieR and the alasky HiPS host at CDS can be down while their
    mirrors work. The SIMBAD/VizieR/HiPS tests are therefore run per server so
    an outage shows exactly which host is broken.
  * The CatWISE2020 VizieR table id is ``II/365/catwise``; plain ``II/365``
    returns nothing.
"""
import math

import astropy.units as u
import numpy as np
import pytest
import requests
from astropy.coordinates import Angle, SkyCoord
from astropy.time import Time
from astroquery.exceptions import NoResultsWarning

pytestmark = pytest.mark.network

HTTP_TIMEOUT = 150  # SBIdent can take 30 s+; pytest-timeout is 180 s
HIPS_TIMEOUT = 15  # per request; dead mirrors hang, so keep this short

# SIMBAD J2000 position (epoch 2000).
BARNARD_J2000 = SkyCoord(269.45208 * u.deg, 4.69337 * u.deg)
# Same value as conftest.BARNARD_PM_TOTAL_MAS_YR (tests/ is not a package, so
# it cannot be imported relatively).
BARNARD_PM_TOTAL_MAS_YR = 10393.0
BARNARD_PARALLAX_MAS = 547.0
BARNARD_GAIA_DR3_ID = 4472832130942575872
# 08h55m10.831s -07d14m42.53s
WISE0855 = SkyCoord("08h55m10.831s", "-07d14m42.53s")

CDS_DOWN = pytest.mark.xfail(
    strict=False, reason="CDS unistra hosts returning 404 as of 2026-11-14"
)
# XPASS (non-strict) will show up when the CDS hosts recover.
SIMBAD_SERVERS = [
    pytest.param("simbad.cds.unistra.fr", marks=CDS_DOWN),
    "simbad.harvard.edu",
]
VIZIER_SERVERS = [
    pytest.param("vizier.cds.unistra.fr", marks=CDS_DOWN),
    "vizier.cfa.harvard.edu",
]


# --------------------------------------------------------------------------
# 1. SIMBAD
# --------------------------------------------------------------------------
@pytest.mark.parametrize("server", SIMBAD_SERVERS)
@pytest.mark.parametrize(
    "name, expected, tol",
    [
        ("Barnard's Star", BARNARD_J2000, 1 * u.arcmin),
        # WISE 0855 moves ~8"/yr. The position quoted in the discovery paper
        # (08h55m10.83s, epoch ~2013) is ~85" from SIMBAD's J2000-epoch
        # position, so 1' is too tight; 2' still catches a wrong object.
        ("WISE J085510.83-071442.5", WISE0855, 2 * u.arcmin),
    ],
    ids=["barnard", "wise0855"],
)
def test_simbad_resolves_name(server, name, expected, tol):
    from astroquery.simbad import Simbad

    s = Simbad()
    s.server = server
    result = s.query_object(name)

    assert result is not None and len(result) == 1, f"{name!r} not resolved"
    got = SkyCoord(result["ra"][0] * u.deg, result["dec"][0] * u.deg)
    assert got.separation(expected) < tol


def test_simbad_unknown_name_warns_and_returns_empty_table():
    """astroquery 0.4.11: unknown name -> NoResultsWarning + empty table
    (not None). Uses the mirror because the CDS host may be down."""
    from astroquery.simbad import Simbad

    s = Simbad()
    s.server = "simbad.harvard.edu"
    with pytest.warns(NoResultsWarning):
        result = s.query_object("zzz-not-a-real-object-12345")
    assert len(result) == 0


# --------------------------------------------------------------------------
# 2. Gaia DR3
# --------------------------------------------------------------------------
def test_gaia_dr3_cone_search_barnard(barnard, monkeypatch):
    from astroquery.gaia import Gaia

    monkeypatch.setattr(Gaia, "ROW_LIMIT", 50)
    rows = Gaia.cone_search(barnard, radius=10 * u.arcsec).get_results()

    assert len(rows) >= 1
    # Faint neighbours may have no PM (masked); astropy turns those to nan.
    pm_total = np.hypot(
        np.ma.filled(rows["pmra"].astype(float), np.nan),
        np.ma.filled(rows["pmdec"].astype(float), np.nan),
    )
    assert np.isfinite(pm_total).any(), "no source with a proper motion"
    row = rows[int(np.nanargmax(pm_total))]

    assert int(row["source_id"]) == BARNARD_GAIA_DR3_ID
    assert math.hypot(row["pmra"], row["pmdec"]) == pytest.approx(
        BARNARD_PM_TOTAL_MAS_YR, rel=0.02
    )
    assert row["parallax"] == pytest.approx(BARNARD_PARALLAX_MAS, abs=3)
    # Gaia DR3 reference epoch
    assert float(row["ref_epoch"]) == pytest.approx(2016.0)


# --------------------------------------------------------------------------
# 3. VizieR: CatWISE2020 and 2MASS
# --------------------------------------------------------------------------
def _vizier(server, **kw):
    from astroquery.vizier import Vizier

    return Vizier(columns=["**", "+_r"], row_limit=50, vizier_server=server, **kw)


@pytest.mark.parametrize("server", VIZIER_SERVERS)
def test_vizier_catwise2020_columns_and_cone(server, barnard):
    tables = _vizier(server).query_region(
        barnard, radius=60 * u.arcsec, catalog="II/365/catwise"
    )
    assert len(tables) == 1
    t = tables[0]
    assert len(t) > 0
    for col in ("pmRA", "pmDE", "e_pmRA", "e_pmDE", "MJD", "RA_ICRS", "DE_ICRS"):
        assert col in t.colnames, f"missing CatWISE column {col}"
    assert t["pmRA"].unit == u.arcsec / u.yr
    # WISE/NEOWISE detection epochs: MJD 55200 (2010) - 59600 (2022)
    mjd = t["MJD"].compressed()
    assert mjd.min() > 55000 and mjd.max() < 60000
    # every returned row is inside the requested cone
    # _r carries no unit in the returned table, so verify the cone from the
    # positions themselves, with explicit units.
    pos = SkyCoord(
        np.asarray(t["RA_ICRS"], dtype=float) * u.deg,
        np.asarray(t["DE_ICRS"], dtype=float) * u.deg,
    )
    assert (pos.separation(barnard) <= 60 * u.arcsec).all()


def _catwise_total_pm(t) -> np.ndarray:
    """Total PM in arcsec/yr per row (nan where PM is masked)."""
    pmra = np.ma.filled(t["pmRA"].astype(float), np.nan)
    pmde = np.ma.filled(t["pmDE"].astype(float), np.nan)
    return np.hypot(pmra, pmde)


@pytest.mark.parametrize("server", VIZIER_SERVERS)
def test_vizier_catwise2020_does_not_recover_barnard(server, barnard):
    """Barnard's Star (W1~4.5, saturated) is NOT a usable CatWISE mover.

    Live data (cfa mirror): the nearest source is ~21" from the J2016 position
    and the largest PM within 60" is ~3.7"/yr, far from Barnard's 10.4"/yr.
    This pins that fact so nobody builds a Barnard CatWISE demo on it; WISE
    0855 is the positive control (next test).
    """
    t = _vizier(server).query_region(
        barnard, radius=60 * u.arcsec, catalog="II/365/catwise"
    )[0]
    pm = _catwise_total_pm(t)
    barnard_like = np.isfinite(pm) & (pm > 0.8 * BARNARD_PM_TOTAL_MAS_YR / 1000)
    assert not barnard_like.any(), f"unexpected ~10\"/yr CatWISE mover: {pm.max()}"


# Literature: WISE 0855-0714 total proper motion ~8.1"/yr (Luhman 2014,
# ApJ 786 L18; Kirkpatrick+2019 ~8.1"/yr). Live CatWISE2020 gives ~8.0"/yr.
WISE0855_PM_ARCSEC_YR = 8.1


@pytest.mark.parametrize("server", VIZIER_SERVERS)
def test_vizier_catwise2020_recovers_wise0855_proper_motion(server):
    # The discovery-epoch position is ~40" from the CatWISE (2015-16) one.
    t = _vizier(server).query_region(
        WISE0855, radius=150 * u.arcsec, catalog="II/365/catwise"
    )[0]
    pm = _catwise_total_pm(t)
    assert np.isfinite(pm).any()
    best = float(np.nanmax(pm))
    assert best == pytest.approx(WISE0855_PM_ARCSEC_YR, abs=0.5)
    row = t[int(np.nanargmax(pm))]
    # RA proper motion is negative (moving west) in the literature too
    assert row["pmRA"] < 0


@pytest.mark.parametrize("server", VIZIER_SERVERS)
def test_vizier_2mass_source_near_barnard_j2000(server):
    tables = _vizier(server).query_region(
        BARNARD_J2000, radius=30 * u.arcsec, catalog="II/246"
    )
    assert len(tables) == 1
    t = tables[0]
    assert len(t) > 0
    for col in ("RAJ2000", "DEJ2000", "Jmag", "Hmag", "Kmag"):
        assert col in t.colnames
    # Barnard's Star is J=5.24 (bright, saturated-ish). 2MASS observed it
    # 1998-2000 and PM is ~10"/yr, so allow ~10" from the J2000 position.
    bright = t[t["Jmag"] < 6]
    assert len(bright) == 1, "expected exactly one J<6 source in the cone"
    pos = SkyCoord(float(bright["RAJ2000"][0]) * u.deg, float(bright["DEJ2000"][0]) * u.deg)
    assert pos.separation(BARNARD_J2000) < 10 * u.arcsec
    assert bright["Jmag"][0] == pytest.approx(5.24, abs=0.1)


# --------------------------------------------------------------------------
# 4. Known small bodies: SkyBoT and JPL SBIdent
# --------------------------------------------------------------------------
CERES_EPOCH = Time("2026-06-01 00:00:00", scale="utc")


@pytest.fixture(scope="module")
def ceres():
    """Ceres RA/Dec (geocentric, UT) at CERES_EPOCH from JPL Horizons."""
    from astroquery.jplhorizons import Horizons

    eph = Horizons(
        id="1", id_type="smallbody", location="500", epochs=CERES_EPOCH.jd
    ).ephemerides()
    assert len(eph) == 1
    return SkyCoord(eph["RA"][0] * u.deg, eph["DEC"][0] * u.deg)


def test_skybot_cone_search_finds_ceres(ceres):
    from astroquery.imcce import Skybot

    rows = Skybot.cone_search(ceres, 60 * u.arcsec, CERES_EPOCH, cache=False)

    assert 1 in set(rows["Number"]), f"Ceres not in {list(rows['Name'])}"
    row = rows[list(rows["Number"]).index(1)]
    assert row["Name"] == "Ceres"
    assert SkyCoord(row["RA"], row["DEC"]).separation(ceres) < 2 * u.arcsec
    assert row["centerdist"] < 2 * u.arcsec
    assert 7 < float(row["V"]) < 11  # Ceres is ~8.8 mag
    # Gotcha: do NOT assert on rows["epoch"]. astroquery 0.4.11 reports it
    # truncated (2461190.0 for a 2461192.5 query; the astroquery docstring
    # example shows the same). The RA/Dec match to Horizons above proves the
    # requested epoch was honoured.


def test_skybot_empty_field_warns_and_returns_empty_table():
    """astroquery 0.4.11: no objects -> NoResultsWarning + empty table
    (does not raise, despite what older SkyBoT clients did)."""
    from astroquery.imcce import Skybot

    with pytest.warns(NoResultsWarning):
        rows = Skybot.cone_search(
            SkyCoord(269.4485 * u.deg, 89.0 * u.deg),
            5 * u.arcsec,
            CERES_EPOCH,
            cache=False,
        )
    assert len(rows) == 0


def _sbident_angle(angle: Angle, sign: bool) -> str:
    """SBIdent wants hh-mm-ss / dd-mm-ss, with 'M' replacing a minus sign."""
    s = abs(angle).to_string(sep="-", precision=1, pad=True)
    return ("M" if sign and angle < 0 else "") + s


def _sbident(center: SkyCoord, hwidth_deg: float, vmag_lim: float):
    params = {
        "mpc-code": "500",
        "obs-time": CERES_EPOCH.utc.strftime("%Y-%m-%d_%H:%M:%S"),
        "vmag-lim": vmag_lim,
        "sb-kind": "a",
        "fov-ra-center": _sbident_angle(center.ra.to(u.hourangle), False),
        "fov-dec-center": _sbident_angle(center.dec, True),
        "fov-ra-hwidth": hwidth_deg,
        "fov-dec-hwidth": hwidth_deg,
    }
    r = requests.get(
        "https://ssd-api.jpl.nasa.gov/sb_ident.api", params=params, timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    return r.json()


def test_jpl_sbident_finds_ceres(ceres):
    data = _sbident(ceres, hwidth_deg=0.1, vmag_lim=12)

    assert "version" in data["signature"]
    assert data["n_first_pass"] >= 1
    fields = data["fields_first"]
    names = [row[0] for row in data["data_first_pass"]]
    assert any(n.startswith("1 Ceres") for n in names), names
    row = next(r for r in data["data_first_pass"] if r[0].startswith("1 Ceres"))
    # offset from FOV centre (arcsec) and V mag
    assert float(row[fields.index('Dist. from center Norm (")')]) < 5.0
    assert 7 < float(row[fields.index("Visual magnitude (V)")]) < 11


def test_jpl_sbident_empty_field_reports_no_records():
    """Empty field: HTTP 200 with a 'warning' and no data table (no error)."""
    data = _sbident(SkyCoord(0 * u.deg, -89 * u.deg), hwidth_deg=0.05, vmag_lim=12)

    assert data.get("warning") == "no matching records"
    assert "data_first_pass" not in data


# --------------------------------------------------------------------------
# 5. HiPS backgrounds for Aladin Lite
# --------------------------------------------------------------------------
MOCSERVER = "https://alasky.cds.unistra.fr/MocServer/query"
HIPS_IDS = {"allWISE": "CDS/P/allWISE/color", "2MASS": "CDS/P/2MASS/color"}
# Hard-coded copy of the MocServer hips_service_url* lists (2026-11-14), used
# when the MocServer itself (hosted on alasky) is unreachable. First = primary.
HIPS_FALLBACK_URLS = {
    "allWISE": [
        "https://alasky.cds.unistra.fr/AllWISE/RGB-W4-W2-W1",
        "https://alaskybis.cds.unistra.fr/AllWISE/RGB-W4-W2-W1",
        "https://irsa.ipac.caltech.edu/data/hips/CDS/AllWISE/RGB-W4-W2-W1",
        "https://skies.esac.esa.int/AllWISEColor",
        "https://hips.china-vo.org/m/CDS_P_allWISE_color",
    ],
    "2MASS": [
        "https://alasky.cds.unistra.fr/2MASS/Color",
        "https://alaskybis.cds.unistra.fr/2MASS/Color",
        "https://irsa.ipac.caltech.edu/data/hips/CDS/2MASS/Color",
    ],
}


def _hips_urls(survey: str) -> list[str]:
    """Service URLs from MocServer, else the hard-coded list."""
    try:
        r = requests.get(
            MOCSERVER,
            params={"ID": HIPS_IDS[survey], "get": "record"},
            timeout=HIPS_TIMEOUT,
        )
        r.raise_for_status()
        urls = []
        for line in r.text.splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip().startswith("hips_service_url"):
                urls.append(v.strip())
        if urls:
            return urls
    except requests.RequestException:
        pass
    return HIPS_FALLBACK_URLS[survey]


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) from the first SOFn marker; raises on malformed JPEG."""
    assert data[:3] == b"\xff\xd8\xff", "not a JPEG (bad SOI)"
    i = 2
    while i + 9 < len(data):
        assert data[i] == 0xFF, "corrupt JPEG marker stream"
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = int.from_bytes(data[i + 5 : i + 7], "big")
            w = int.from_bytes(data[i + 7 : i + 9], "big")
            return w, h
        i += 2 + int.from_bytes(data[i + 2 : i + 4], "big")
    raise AssertionError("no SOF marker found")


def _check_hips_base(base: str, hips_id: str):
    props = requests.get(f"{base}/properties", timeout=HIPS_TIMEOUT)
    assert props.status_code == 200, f"{base}/properties -> {props.status_code}"
    kv = dict(
        (k.strip(), v.strip())
        for k, v in (l.split("=", 1) for l in props.text.splitlines() if "=" in l)
    )
    assert kv["creator_did"] == f"ivo://{hips_id}"
    assert "jpeg" in kv["hips_tile_format"]
    assert int(kv["hips_order"]) >= 3
    width = int(kv["hips_tile_width"])

    tile = requests.get(f"{base}/Norder3/Dir0/Npix0.jpg", timeout=HIPS_TIMEOUT)
    assert tile.status_code == 200, f"tile -> {tile.status_code}"
    assert tile.headers["Content-Type"].startswith("image/jpeg")
    assert _jpeg_size(tile.content) == (width, width)


@CDS_DOWN
@pytest.mark.parametrize("survey", HIPS_IDS)
def test_hips_primary_cds_host_serves_survey(survey):
    """The canonical alasky host must serve properties + tile."""
    _check_hips_base(HIPS_FALLBACK_URLS[survey][0], HIPS_IDS[survey])


@pytest.mark.parametrize("survey", HIPS_IDS)
def test_hips_at_least_one_mirror_serves_survey(survey):
    """Aladin Lite can be pointed at any registered mirror; one must work."""
    urls = _hips_urls(survey)
    failures = {}
    for base in urls:
        try:
            _check_hips_base(base, HIPS_IDS[survey])
            return
        except (AssertionError, requests.RequestException, KeyError) as exc:
            failures[base] = f"{type(exc).__name__}: {exc}"
    pytest.fail(f"no mirror serves {HIPS_IDS[survey]}: {failures}")
