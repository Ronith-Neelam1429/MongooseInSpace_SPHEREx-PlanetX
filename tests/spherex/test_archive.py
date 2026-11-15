"""Offline tests for planetx.spherex.archive: URL allow-list, SIA parsing, passes, retries."""
import io

import numpy as np
import pytest
import requests
from astropy.io.votable import from_table
from astropy.table import Table

from synthetic import make_mef
from planetx.spherex import archive

GOOD_URL = (
    "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/2025W33_2C/"
    "l2b-v27-2026-228/6/level2_2025W33_2C_0321_1D6_spx_l2b-v27-2026-228.fits"
)

# Verbatim shape of a live IRSA SIA error (HTTP 200).
SIA_ERROR = b"""<?xml version="1.0" encoding="utf-8"?>
<VOTABLE version="1.3" xmlns="http://www.ivoa.net/xml/VOTable/v1.3">
  <RESOURCE type="results">
    <INFO name="QUERY_STATUS" value="ERROR">UsageFault: BAD_REQUEST: bad ra</INFO>
  </RESOURCE>
</VOTABLE>
"""


# --- URL allow-list ------------------------------------------------------------------


def test_allows_spherex_file_url():
    assert archive.validate_spherex_url(GOOD_URL) == GOOD_URL


@pytest.mark.parametrize("url", [
    GOOD_URL.replace("https://", "http://"),  # plain http
    GOOD_URL.replace("irsa.ipac.caltech.edu", "irsa.ipac.caltech.edu.evil.com"),  # lookalike
    GOOD_URL.replace("irsa.ipac.caltech.edu", "evilirsa.ipac.caltech.edu"),
    GOOD_URL.replace("irsa.ipac.caltech.edu", "irsa.ipac.caltech.edu."),  # trailing dot
    GOOD_URL.replace("irsa.ipac.caltech.edu", "IRSA.ipac.caltech.edu"),
    GOOD_URL.replace("irsa.ipac.caltech.edu", "irsa.ipac.caltech.edu:8443"),  # port
    GOOD_URL.replace("irsa.ipac.caltech.edu", "irsa.ipac.caltech.edu:443"),
    GOOD_URL.replace("https://", "https://user:pw@"),  # userinfo
    GOOD_URL.replace("https://irsa.ipac.caltech.edu", "https://irsa.ipac.caltech.edu@evil.com"),
    GOOD_URL.replace("https://irsa.ipac.caltech.edu", "https://evil.com#@irsa.ipac.caltech.edu"),
    GOOD_URL.replace("https://irsa.ipac.caltech.edu", "https://127.0.0.1"),
    "https://irsa.ipac.caltech.edu/ibe/data/spherex/../../../etc/passwd.fits",  # traversal
    "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/../../wise/x.fits",
    "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/./x.fits",
    "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1//x.fits",
    "https://irsa.ipac.caltech.edu/ibe/data/spherex/%2e%2e/%2e%2e/x.fits",  # encoded traversal
    "https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1\\..\\x.fits",
    "https://irsa.ipac.caltech.edu/ibe/data/wise/allwise/x.fits",  # other dataset
    "https://irsa.ipac.caltech.edu/SIA",
    "https://irsa.ipac.caltech.edu/ibe/data/spherexevil/x.fits",  # prefix trick
    GOOD_URL + "?center=1,2&size=1",  # caller-supplied query
    GOOD_URL + "?",
    GOOD_URL + "#frag",
    GOOD_URL.replace(".fits", ".txt"),
    GOOD_URL.replace("level2/", "level2/\n"),  # urlsplit strips newlines silently
    GOOD_URL.replace("level2/", "level2/\t"),
    " " + GOOD_URL,
    "file:///etc/passwd",
    "",
])
def test_rejects_other_urls(url):
    with pytest.raises(ValueError):
        archive.validate_spherex_url(url)


# --- SIA parsing ---------------------------------------------------------------------


def _sia_table(rows) -> Table:
    """A table with the live SIA column names and units (em_* in metres)."""
    names = ("obs_id", "obs_collection", "energy_bandpassname", "t_min", "t_max",
             "em_min", "em_max", "access_url")
    return Table(rows=rows, names=names)


def _row(obs_id, det, t_min, collection="spherex_dr1", em=(1.6301e-06, 2.4346e-06)):
    url = GOOD_URL.replace("0321_1D6", f"{obs_id}D{det}")
    return (obs_id, collection, f"SPHEREx-D{det}", t_min, t_min + 0.0013, em[0], em[1], url)


def _votable_bytes(table: Table) -> bytes:
    buf = io.BytesIO()
    from_table(table).to_xml(buf)
    return buf.getvalue()


def test_parse_sia_votable_reads_live_column_names():
    table = archive.parse_sia_votable(_votable_bytes(_sia_table([_row("2025W33_2C_0321_1", 3, 60902.864715)])))
    assert list(table["obs_id"]) == ["2025W33_2C_0321_1"]


def test_parse_sia_votable_raises_on_query_status_error():
    with pytest.raises(archive.UpstreamError, match="BAD_REQUEST") as err:
        archive.parse_sia_votable(SIA_ERROR)
    assert not err.value.transient


def test_parse_sia_votable_treats_html_busy_page_as_transient():
    with pytest.raises(archive.UpstreamError) as err:
        archive.parse_sia_votable(b"<html><body>Server busy, retry later.</body></html>")
    assert err.value.transient


def test_visits_from_table_converts_units_detector_and_time():
    (visit,) = archive.visits_from_table(_sia_table([_row("2025W33_2C_0321_1", 3, 60902.864715)]))
    assert visit.detector == 3
    assert visit.collection == "spherex_dr1"
    assert visit.mjd == pytest.approx(60902.864715 + 0.00065)
    assert visit.date == "2025-08-15"
    assert visit.wavelength_min_um == pytest.approx(1.6301)
    assert visit.wavelength_max_um == pytest.approx(2.4346)
    assert visit.access_url.endswith("2025W33_2C_0321_1D3_spx_l2b-v27-2026-228.fits")


def test_visits_from_table_tolerates_missing_wavelength_and_unknown_detector():
    table = _sia_table([_row("a", 3, 60902.0)])
    table["energy_bandpassname"] = ["other"]
    table["em_min"] = [np.nan]
    (visit,) = archive.visits_from_table(table)
    assert visit.detector is None and visit.wavelength_min_um is None


def _visit(mjd):
    return archive.visits_from_table(_sia_table([_row("x", 1, mjd)]))[0]


def test_group_passes_splits_at_gaps_over_30_days():
    mjds = [60902.0, 60903.5, 60910.0, 61085.0, 61086.0, 61270.0]
    passes = archive.group_passes([_visit(m) for m in mjds])
    assert [p.n_visits for p in passes] == [3, 2, 1]
    assert passes[0].start == "2025-08-15" and passes[0].end == "2025-08-23"
    assert archive.group_passes([]) == []


def test_group_passes_gap_of_exactly_30_days_stays_in_one_pass():
    assert len(archive.group_passes([_visit(60900.0), _visit(60930.0)])) == 1


# --- HTTP layer: fake session --------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status_code, self._body = status, body

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    """Replays a script of FakeResponses / exceptions and records each call."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def session(monkeypatch):
    def install(*script):
        fake = FakeSession(*script)
        monkeypatch.setattr(archive, "_session", fake)
        return fake
    return install


def test_fetch_cutout_sends_degrees_and_never_follows_redirects(session):
    body = make_mef(10.0, 20.0)
    fake = session(FakeResponse(200, body))
    assert archive.fetch_cutout(GOOD_URL, 10.0, 20.0, 0.15) == body
    url, kwargs = fake.calls[0]
    assert url == GOOD_URL
    assert kwargs["params"] == {"center": "10.0000000,20.0000000", "size": "0.15000"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (archive.CONNECT_TIMEOUT_S, archive.READ_TIMEOUT_S)


@pytest.mark.parametrize("transient", [
    FakeResponse(503, b"Server busy, retry later."),
    FakeResponse(502, b"<html>Proxy Error</html>"),
    FakeResponse(200, b"<html>busy</html>"),  # 200 but not FITS
    FakeResponse(200, b"SIMPLE  =                    T / truncated"),  # unreadable FITS
    requests.ConnectionError("reset"),
    requests.ReadTimeout("slow"),
    FakeResponse(429, b"Too Many Requests"),
    FakeResponse(408, b"Request Timeout"),
])
def test_fetch_cutout_retries_transient_failures(session, transient):
    body = make_mef(10.0, 20.0)
    fake = session(transient, FakeResponse(200, body))
    assert archive.fetch_cutout(GOOD_URL, 10.0, 20.0, 0.1) == body
    assert len(fake.calls) == 2


@pytest.mark.parametrize("status", [400, 403, 404, 422, 302])
def test_fetch_cutout_fails_fast_on_client_errors_and_redirects(session, status):
    fake = session(FakeResponse(status, b"Input position contains invalid values"))
    with pytest.raises(archive.UpstreamError) as err:
        archive.fetch_cutout(GOOD_URL, 10.0, 20.0, 0.1)
    assert err.value.status == status and not err.value.transient
    assert len(fake.calls) == 1


def test_fetch_cutout_gives_up_after_bounded_attempts(session):
    fake = session(*[FakeResponse(503, b"busy")] * 10)
    with pytest.raises(archive.UpstreamError, match="after 3 attempt") as err:
        archive.fetch_cutout(GOOD_URL, 10.0, 20.0, 0.1)
    assert err.value.status == 503
    assert len(fake.calls) == len(archive.RETRY_BACKOFF_S) + 1


def test_fetch_cutout_refuses_oversized_body(session, monkeypatch):
    monkeypatch.setattr(archive, "MAX_BODY_BYTES", 1000)
    session(FakeResponse(200, b"SIMPLE" + b" " * 5000))
    with pytest.raises(archive.UpstreamError, match="larger than"):
        archive.fetch_cutout(GOOD_URL, 10.0, 20.0, 0.1)


def test_fetch_cutout_validates_url_before_any_request(session):
    fake = session()
    with pytest.raises(ValueError):
        archive.fetch_cutout("https://evil.example/ibe/data/spherex/x.fits", 1.0, 2.0, 0.1)
    assert fake.calls == []


def test_query_visits_merges_collections_sorted_and_caches(session):
    dr1 = _votable_bytes(_sia_table([_row("b", 3, 60950.0), _row("a", 6, 60902.0)]))
    qr3 = _votable_bytes(_sia_table([_row("c", 1, 61300.0, collection="spherex_qr3")]))
    fake = session(FakeResponse(200, dr1), FakeResponse(200, qr3))
    visits = archive.query_visits(269.4485, 4.7394, 10)
    assert [v.obs_id for v in visits] == ["a", "b", "c"]
    assert [v.collection for v in visits] == ["spherex_dr1", "spherex_dr1", "spherex_qr3"]
    params = [kw["params"] for _, kw in fake.calls]
    assert [p["COLLECTION"] for p in params] == ["spherex_dr1", "spherex_qr3"]
    assert params[0]["POS"] == "CIRCLE 269.4485000 4.7394000 0.0027778"
    # Second call is served from the in-memory cache: no new requests.
    assert [v.obs_id for v in archive.query_visits(269.4485, 4.7394, 10)] == ["a", "b", "c"]
    assert len(fake.calls) == 2


def test_query_visits_fails_whole_request_if_one_collection_fails(session):
    dr1 = _votable_bytes(_sia_table([_row("a", 6, 60902.0)]))
    session(FakeResponse(200, dr1), FakeResponse(200, SIA_ERROR))
    with pytest.raises(archive.UpstreamError, match="spherex_qr3"):
        archive.query_visits(1.0, 2.0, 10)


def test_query_visits_refuses_possibly_truncated_results(session, monkeypatch):
    monkeypatch.setattr(archive, "SIA_MAXREC", 2)
    full = _votable_bytes(_sia_table([_row("a", 6, 60902.0), _row("b", 6, 60903.0)]))
    session(FakeResponse(200, full))
    with pytest.raises(archive.UpstreamError, match="truncated"):
        archive.query_visits(1.0, 2.0, 10)


def test_check_cutout_fits_rejects_truncated_download_as_transient():
    body = make_mef(10.0, 20.0)
    for cut in (body[:-2880], body[:-100]):
        with pytest.raises(archive.UpstreamError) as err:
            archive.check_cutout_fits(cut)
        assert err.value.transient


def test_check_cutout_fits_rejects_cutout_without_required_hdus():
    from astropy.io import fits

    hdul = fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((3, 3)), name="IMAGE")])
    buf = io.BytesIO()
    hdul.writeto(buf)
    with pytest.raises(archive.UpstreamError, match="lacks HDUs") as err:
        archive.check_cutout_fits(buf.getvalue())
    assert not err.value.transient


def _free_slots():
    n = 0
    while archive._slots.acquire(blocking=False):
        n += 1
    for _ in range(n):
        archive._slots.release()
    return n


@pytest.mark.parametrize("failure", [
    requests.ConnectionError("reset"),
    requests.exceptions.InvalidURL("bad"),
    FakeResponse(200, b"SIMPLE" + b" " * 5000),  # oversized (limit patched below)
    FakeResponse(404, b"nope"),
])
def test_concurrency_slot_is_released_on_every_error_path(session, monkeypatch, failure):
    monkeypatch.setattr(archive, "MAX_BODY_BYTES", 1000)
    session(*[failure] * 3)
    with pytest.raises(archive.UpstreamError):
        archive.fetch_cutout(GOOD_URL, 10.0, 20.0, 0.1)
    assert _free_slots() == archive.MAX_CONCURRENT_REQUESTS


def test_no_free_slot_before_the_deadline_raises_busy(session, monkeypatch):
    monkeypatch.setattr(archive, "TOTAL_DEADLINE_S", 0.05)
    fake = session()
    for _ in range(archive.MAX_CONCURRENT_REQUESTS):
        archive._slots.acquire()
    try:
        with pytest.raises(archive.IrsaBusy):
            archive.fetch_cutout(GOOD_URL, 10.0, 20.0, 0.1)
    finally:
        for _ in range(archive.MAX_CONCURRENT_REQUESTS):
            archive._slots.release()
    assert fake.calls == []


def test_retries_stop_at_the_total_deadline(session, monkeypatch):
    monkeypatch.setattr(archive, "RETRY_BACKOFF_S", (5.0, 5.0))
    monkeypatch.setattr(archive, "TOTAL_DEADLINE_S", 1.0)
    fake = session(*[FakeResponse(503, b"busy")] * 3)
    with pytest.raises(archive.UpstreamError, match="after 1 attempt"):
        archive.fetch_cutout(GOOD_URL, 10.0, 20.0, 0.1)
    assert len(fake.calls) == 1  # a 5 s back-off would overrun the 1 s budget


def test_check_cutout_fits_rejects_missing_time_or_wcs_as_permanent():
    from astropy.io import fits

    for card in ("MJD-AVG", "CTYPE1"):
        with fits.open(io.BytesIO(make_mef(10.0, 20.0))) as hdul:
            del hdul["IMAGE"].header[card]
            if card == "CTYPE1":
                del hdul["IMAGE"].header["CTYPE2"]
            buf = io.BytesIO()
            hdul.writeto(buf)
        with pytest.raises(archive.UpstreamError) as err:
            archive.check_cutout_fits(buf.getvalue())
        assert not err.value.transient, card


# --- footprints ----------------------------------------------------------------------

# Verbatim s_region of a live spherex_dr1 row near the NEP (Nov 2026).
LIVE_S_REGION = (
    "POLYGON ICRS 276.35323504130406 65.56283762761798 269.4274416869298 63.76709617595698 "
    "264.4491274540355 66.5564518040036 271.90256896810126 68.5869339222068 "
    "276.35323504130406 65.56283762761798"
)


def test_parse_s_region_reads_the_live_format_and_drops_the_closing_vertex():
    vertices = archive.parse_s_region(LIVE_S_REGION)
    assert vertices.shape == (4, 2)
    assert tuple(vertices[2]) == (264.4491274540355, 66.5564518040036)
    assert archive.parse_s_region("POLYGON 1 2 3 4 5 6").shape == (3, 2)  # frame is optional


@pytest.mark.parametrize("bad", [
    "CIRCLE ICRS 1 2 3", "POLYGON ICRS 1 2 3 4", "POLYGON ICRS 1 2 3 4 5", "POLYGON ICRS 1 2 3 x 5 6",
    "POLYGON ICRS 1 2 3 4 5 nan", "POLYGON ICRS 1 2 3 95 5 6", "",
])
def test_parse_s_region_refuses_anything_else(bad):
    with pytest.raises(archive.UpstreamError):
        archive.parse_s_region(bad)


def test_query_footprints_pairs_each_visit_with_its_footprint(session):
    def table(rows, regions):
        t = _sia_table(rows)
        t["s_region"] = regions
        return _votable_bytes(t)

    other = LIVE_S_REGION.replace("276.35323504130406", "276.0")
    dr1 = table([_row("b", 3, 60950.0), _row("a", 6, 60902.0)], [LIVE_S_REGION, other])
    qr3 = table([_row("c", 1, 61300.0, collection="spherex_qr3")], ["POLYGON ICRS 1 2 3 4 5 6"])
    session(FakeResponse(200, dr1), FakeResponse(200, qr3))
    found = archive.query_footprints(269.4485, 4.7394, 10)
    assert [v.obs_id for v, _ in found] == ["a", "b", "c"]
    assert [fp[0, 0] for _, fp in found] == [276.0, 276.35323504130406, 1.0]


def test_parse_s_region_skips_any_stc_s_frame_words():
    words = "POLYGON ICRS TOPOCENTER SPHERICAL2 1 2 3 4 5 6"
    assert archive.parse_s_region(words).tolist() == [[1, 2], [3, 4], [5, 6]]
    assert archive.parse_s_region("polygon j2000 1 2 3 4 5 6").shape == (3, 2)


def test_query_footprints_leaves_out_rows_with_unreadable_footprints(session, caplog):
    def table(rows, regions):
        t = _sia_table(rows)
        t["s_region"] = regions
        return _votable_bytes(t)

    dr1 = table([_row("a", 1, 60902.0), _row("b", 2, 60950.0)], [LIVE_S_REGION, "CIRCLE ICRS 1 2 3"])
    qr3 = table([_row("c", 1, 61300.0, collection="spherex_qr3")], ["POLYGON ICRS 1 2 3 4 5 6"])
    session(FakeResponse(200, dr1), FakeResponse(200, qr3))
    with caplog.at_level("WARNING"):
        found = archive.query_footprints(269.4485, 4.7394, 10)
    assert [v.obs_id for v, _ in found] == ["a", "c"]
    assert "left out 1 of 2 rows" in caplog.text


def test_query_footprints_fails_when_no_row_is_readable(session):
    t = _sia_table([_row("a", 1, 60902.0)])
    t["s_region"] = ["BOX ICRS 1 2 3 4"]
    session(FakeResponse(200, _votable_bytes(t)))
    with pytest.raises(archive.UpstreamError, match="no row has a readable s_region"):
        archive.query_footprints(269.4485, 4.7394, 10)
