"""Offline tests for the catalogue fetchers: parsing of recorded answers and failure handling.

The response texts were recorded from the live services on 2026-11-14 (trimmed).
"""
import pytest
import requests

from planetx.classify import catalogs
from planetx.classify.catalogs import (
    CatalogError,
    display_name,
    fetch_gaia,
    fetch_simbad,
    fetch_skybot,
    is_variable_otype,
    parse_gaia_csv,
    parse_simbad_csv,
    parse_skybot_json,
)

REAL_SEND = catalogs._send  # tests/conftest.py replaces it for offline tests

GAIA_CSV = """source_id,ra,dec,ref_epoch,pmra,pmdec,phot_g_mean_mag,phot_rp_mean_mag,phot_variable_flag
4472832130942575872,269.44850252543836,4.739420051112412,2016.0,-801.5509783684709,10362.394206546573,8.1939745,6.9580913,NOT_AVAILABLE
4472836460256445696,269.3287597266394,4.7598912690740445,2016.0,-2.739156834609066,-2.0106640861485916,10.60065,9.44206,VARIABLE
4472832822425127552,269.4420077569429,4.734345358343144,2016.0,,,18.944616,,NOT_AVAILABLE
"""

# The type join repeats a row when SIMBAD's type table lists a type twice (seen for "EmO").
SIMBAD_CSV = """main_id,ra,dec,otype,description,pmra,pmdec,G,V,K
"NAME Barnard's star",269.4520769586187,4.693364966576667,"BY*","BY Dra Variable",-801.551,10362.394,8.193974,9.511,4.524
"V* V3215 Oph",269.42636781366,4.6521865157199995,"EB*","Eclipsing Binary",-2.715,-6.44,15.525364,,
"TYC  425-262-1",269.4664581074621,4.705611349569168,"SB*","Spectroscopic Binary",0.578,-8.671,,11.5,10.575
"1RXS J175749.5+043955",269.4562708333333,4.665280555555555,"X","X-ray Source",,,,,
"1RXS J175749.5+043955",269.4562708333333,4.665280555555555,"X","X-ray Source",,,,,
"""

SKYBOT_JSON = (
    '[{"Num":213446,"Name":"2002 AV66","RA (hms)":"17 57 59.0642","DEC (dms)":"-22 59 33.230","Class":"MB>Inner",'
    '"VMag (mag)":21.6,"Err (arcsec)":0.013,"d (arcsec)":160.001,"dRA (arcsec\\/h)":81.8799,"dDEC (arcsec\\/h)":1.3,'
    '"dg (ua)":2.84407084718,"dh (ua)":2.06066081789,"ref_epoch":2461000},'
    '{"Name":"2015 RQ390","RA (hms)":"17 57 48.3760","DEC (dms)":"-23 03 42.831","Class":"NEA>Apollo",'
    '"VMag (mag)":25.1,"Err (arcsec)":0,"d (arcsec)":223.063,"dg (ua)":3.52039027831,"ref_epoch":2461000}]'
)
# SkyBoT reported this failure with HTTP 200.
SKYBOT_ERROR = (
    '{"flag":-1,"ticket":188187956934473708,"status":200,"message":"SkyBoT conesearch -> computeEphemeris: '
    'error: planetary_ephem: satellite: calceph_compute_unit error #0\\n"}'
)


class Response:
    def __init__(self, status_code=200, text=""):
        self.status_code, self.text = status_code, text


def answers(monkeypatch, *responses):
    """Serve the given responses (or raise the given exceptions) in order; returns the calls made."""
    calls, queue = [], list(responses)

    def send(method, url, *, params, timeout, what):
        calls.append({"method": method, "url": url, "params": params, "timeout": timeout})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(catalogs, "_send", send)
    return calls


def test_gaia_rows_become_stars_with_motion_in_arcsec_per_year():
    barnard, variable, faint = parse_gaia_csv(GAIA_CSV)
    assert barnard.name == "Gaia DR3 4472832130942575872" and barnard.catalog == "gaia"
    assert (barnard.pm_ra_cosdec, barnard.pm_dec) == pytest.approx((-0.80155, 10.36239), abs=1e-5)
    assert barnard.pm_total == pytest.approx(10.393, abs=1e-3)
    assert barnard.epoch_mjd == 57388.5  # J2016.0
    assert barnard.kind == "High proper-motion star" and barnard.magnitude == pytest.approx(8.194, abs=1e-3)
    assert variable.variable and variable.kind == "Star" and not barnard.variable
    # A faint star with a two-parameter solution: no proper motion, which is not the same as zero.
    assert faint.pm_ra_cosdec is None and faint.pm_dec is None and faint.pm_total == 0.0


def test_simbad_rows_keep_j2000_positions_and_readable_names():
    barnard, eclipsing, tycho, xray = parse_simbad_csv(SIMBAD_CSV)  # the repeated row is dropped
    assert barnard.name == "Barnard's Star" and barnard.kind == "BY Dra Variable" and barnard.variable
    assert barnard.epoch_mjd == 51544.5 and barnard.pm_dec == pytest.approx(10.362394)
    assert barnard.magnitude == pytest.approx(8.193974)  # G before V and K
    assert eclipsing.name == "V3215 Oph" and eclipsing.variable
    assert tycho.name == "TYC 425-262-1" and tycho.magnitude == 11.5 and not tycho.variable
    assert xray.pm_ra_cosdec is None and xray.magnitude is None and xray.kind == "X-ray Source"


@pytest.mark.parametrize("main_id, shown", [
    ("NAME Barnard's star", "Barnard's Star"),
    ("NAME Proxima Centauri", "Proxima Centauri"),
    ("V* RW Aur A", "RW Aur A"),
    ("* alf Cen", "alf Cen"),
    ("Gaia DR3 4472832130942575872", "Gaia DR3 4472832130942575872"),
    ("2MASS J17574849+0441405", "2MASS J17574849+0441405"),
])
def test_display_name(main_id, shown):
    assert display_name(main_id) == shown


@pytest.mark.parametrize("otype, variable", [
    ("Mi*", True), ("Mi?", True), ("TT*", True), ("Or*", True), ("EB*", True), ("V*?", True), ("LP?", True),
    ("*", False), ("PM*", False), ("G", False), ("BD*", False), ("WD?", False), ("", False),
])
def test_variable_object_types(otype, variable):
    assert is_variable_otype(otype) is variable


def test_skybot_bodies_are_named_and_placed():
    numbered, unnumbered = parse_skybot_json(SKYBOT_JSON, mjd=61000.0)
    assert numbered.name == "(213446) 2002 AV66" and numbered.kind == "Main-belt asteroid"
    assert (numbered.ra, numbered.dec) == pytest.approx((269.496101, -22.992564), abs=1e-6)
    assert numbered.geocentric_au == pytest.approx(2.844, abs=1e-3) and numbered.v_mag == 21.6 and numbered.mjd == 61000.0
    assert unnumbered.name == "2015 RQ390" and unnumbered.kind == "Near-Earth asteroid"
    assert unnumbered.position_err_arcsec == 0.0


def test_skybot_empty_answer_is_no_body_and_an_error_object_is_an_error():
    assert parse_skybot_json("", mjd=61000.0) == []
    with pytest.raises(CatalogError, match="computeEphemeris"):
        parse_skybot_json(SKYBOT_ERROR, mjd=61000.0)
    with pytest.raises(CatalogError, match="unreadable"):
        parse_skybot_json("<html>502 Bad Gateway</html>", mjd=61000.0)
    with pytest.raises(CatalogError, match="unexpected SkyBoT row"):
        parse_skybot_json('[{"Name": "x"}]', mjd=61000.0)


def test_fetch_gaia_sends_one_cone_query_and_parses_it(monkeypatch):
    calls = answers(monkeypatch, Response(200, GAIA_CSV))
    stars = fetch_gaia(269.4485, 4.7394, 600.0)
    assert len(stars) == 3 and len(calls) == 1
    query = calls[0]["params"]["QUERY"]
    assert "CIRCLE('ICRS', 269.4485000, 4.7394000, 0.166667)" in query
    assert "ORDER BY phot_g_mean_mag" in query and f"TOP {catalogs.GAIA_MAX_ROWS}" in query
    assert calls[0]["method"] == "POST" and calls[0]["timeout"] == catalogs.GAIA_TIMEOUT_S


@pytest.mark.parametrize("response, message", [
    (Response(400, "<VOTABLE><INFO name='QUERY_STATUS' value='ERROR'>Cannot parse query</INFO></VOTABLE>"), "HTTP 400"),
    (Response(503, "Service Unavailable"), "HTTP 503"),
    (Response(200, "<!doctype html><title>Page unavailable</title>"), "unexpected Gaia response"),
    (CatalogError("Gaia request failed: ReadTimeout"), "ReadTimeout"),
])
def test_fetch_gaia_failures_are_catalog_errors(monkeypatch, response, message):
    answers(monkeypatch, response)
    with pytest.raises(CatalogError, match=message):
        fetch_gaia(269.4485, 4.7394, 600.0)


@pytest.mark.parametrize("args", [(360.0, 0.0, 10.0), (10.0, 91.0, 10.0), (10.0, 0.0, 0.0), (10.0, 0.0, 4000.0),
                                  (float("nan"), 0.0, 10.0)])
def test_bad_cones_are_rejected_before_any_request(monkeypatch, args):
    calls = answers(monkeypatch)
    for fetch in (fetch_gaia, fetch_simbad):
        with pytest.raises(ValueError):
            fetch(*args)
    with pytest.raises(ValueError):
        fetch_skybot(*args, 61000.0)
    assert calls == []


def test_fetch_simbad_falls_back_to_the_second_mirror(monkeypatch):
    calls = answers(monkeypatch, Response(404, "<html>Page unavailable</html>"), Response(200, SIMBAD_CSV))
    objects = fetch_simbad(269.45, 4.69, 200.0)
    assert [o.name for o in objects][:2] == ["Barnard's Star", "V3215 Oph"]
    assert [c["url"] for c in calls] == [f"https://{h}/simbad/sim-tap/sync" for h in catalogs.SIMBAD_HOSTS]
    assert "NOT IN ('Pl', 'Pl?')" in calls[0]["params"]["QUERY"]  # exoplanets sit on their star


def test_fetch_simbad_fails_only_when_every_mirror_fails(monkeypatch):
    answers(monkeypatch, Response(404), CatalogError("SIMBAD (simbad.cds.unistra.fr) request failed: ConnectTimeout"))
    with pytest.raises(CatalogError, match="HTTP 404.*ConnectTimeout"):
        fetch_simbad(269.45, 4.69, 200.0)


def test_fetch_skybot_asks_for_one_moment_and_handles_no_content(monkeypatch):
    calls = answers(monkeypatch, Response(204, ""), Response(200, SKYBOT_JSON), Response(200, SKYBOT_ERROR),
                    Response(400, '{"flag":-1,"status":400,"message":"Bad request"}'))
    assert fetch_skybot(269.4485, 89.0, 36.0, 61000.0) == []
    assert [b.name for b in fetch_skybot(269.4485, -23.0, 230.0, 61000.0)] == ["(213446) 2002 AV66", "2015 RQ390"]
    with pytest.raises(CatalogError, match="computeEphemeris"):
        fetch_skybot(180.0, 1.0, 3600.0, 61000.0)
    with pytest.raises(CatalogError, match="HTTP 400"):
        fetch_skybot(180.0, 1.0, 60.0, 61000.0)
    params = calls[0]["params"]
    assert params["-ep"] == "2461000.500000" and params["-rd"] == "0.010000"  # Julian date, degrees
    assert params["-loc"] == "500" and params["-objFilter"] == "101" and params["-mime"] == "json"


def test_send_retries_a_5xx_once_and_reports_network_errors(monkeypatch):
    monkeypatch.setattr(catalogs, "_send", REAL_SEND)
    seen = []

    def post(url, data, timeout):
        seen.append(timeout)
        return Response(502 if len(seen) == 1 else 200, "ok")

    monkeypatch.setattr(catalogs.requests, "post", post)
    resp = catalogs._send("POST", "https://example.invalid/tap", params={}, timeout=7.0, what="Gaia")
    assert resp.status_code == 200 and seen == [(catalogs.CONNECT_TIMEOUT_S, 7.0)] * 2

    seen.clear()
    monkeypatch.setattr(catalogs.requests, "post", lambda url, data, timeout: seen.append(1) or Response(503))
    assert catalogs._send("POST", "https://example.invalid/tap", params={}, timeout=7.0, what="Gaia").status_code == 503
    assert len(seen) == 2  # one retry, then the 5xx is the answer

    def timeout(url, params, timeout):
        raise requests.ConnectTimeout("no route to host 10.0.0.1")

    monkeypatch.setattr(catalogs.requests, "get", timeout)
    with pytest.raises(CatalogError, match="SkyBoT request failed: ConnectTimeout$"):
        catalogs._send("GET", "https://example.invalid/skybot", params={}, timeout=7.0, what="SkyBoT")
