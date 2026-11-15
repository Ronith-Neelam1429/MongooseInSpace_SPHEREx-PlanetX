"""Offline tests for tools/prewarm.py: its planning and bookkeeping, with a fake API."""
import os
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import prewarm  # noqa: E402


def test_propagate_moves_barnard_north():
    ra, dec = prewarm.propagate(269.4520769586187, 4.693364966576667, -801.551, 10362.394, 26.87)
    assert dec == pytest.approx(4.693365 + 10.362394 * 26.87 / 3600, abs=1e-6)
    assert ra < 269.452


def test_targets_match_frontend_targets_js():
    src = (ROOT / "frontend/src/lib/targets.js").read_text()
    for t in prewarm.TARGETS:  # every position and motion appears verbatim in targets.js
        for value in (t.ra, t.dec, t.pmra, t.pmdec):
            assert re.search(rf"[ :]{re.escape(repr(value))}\b", src), (t.id, value)


def test_barnard_view_is_the_documented_url():
    t = prewarm.select_targets("barnard")[0]
    assert prewarm.view_centre(t, 2026.9) == (269.4473, 4.7685)


def test_view_centre_moves_fast_stars():
    t = prewarm.select_targets("wise0855")[0]
    ra, dec = prewarm.view_centre(t, 2026.9)
    assert ra < t.ra - 0.05  # 8.1"/yr west for 26.9 yr is ~0.06 deg
    assert dec > t.dec


def test_sky_patch_slow_star_is_small_and_fast_star_is_capped_on_today():
    slow = prewarm.sky_patch(prewarm.select_targets("r-dra")[0], 2026.9, 1999.5, 90, 180)
    assert 180 <= slow["side"] < 260
    barnard = prewarm.sky_patch(prewarm.select_targets("barnard")[0], 2026.9, 1999.5, 90, 180)
    assert 440 < barnard["side"] < 490  # 10.4"/yr for 27 years is ~285" plus 2 x 90" of margin
    runaway = prewarm.Target("x", "x", 10.0, 0.0, 0.0, 100000.0)  # 100"/yr: path longer than the largest patch
    far = prewarm.sky_patch(runaway, 2026.9, 1999.5, 90, 180)
    assert far["side"] == prewarm.MAX_SIDE
    assert far["dec"] == pytest.approx(prewarm.propagate(10.0, 0.0, 0.0, 100000.0, 26.9)[1], abs=5e-4)  # snapped


def test_cutout_size_clamps():
    assert prewarm.cutout_size_deg(10) == 0.01
    assert prewarm.cutout_size_deg(900) == 0.25
    assert prewarm.cutout_size_deg(10**6) == 0.5


def test_frame_per_pass_picks_mid_pass_frame_oldest_first():
    frames = [{"mjd": m} for m in (100, 101, 102, 103, 104, 300, 301, 302, 600)]
    got = prewarm.frame_per_pass(list(reversed(frames)))
    assert [f["mjd"] for f in got] == [102, 301, 600]


def test_dir_stats_and_cache_report_warnings(tmp_path):
    old = tmp_path / "cutouts" / "aa" / "old.fits"
    new = tmp_path / "cutouts" / "bb" / "new.fits"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_bytes(b"x" * 3_000_000)
    os.utime(old, (1, 1))
    since = time.time() - 5
    new.write_bytes(b"x" * 2_000_000)
    assert prewarm.dir_stats(tmp_path) == (2, 5_000_000)
    assert prewarm.dir_stats(tmp_path, since) == (1, 2_000_000)
    assert prewarm.dir_stats(tmp_path / "missing") == (0, 0)
    assert prewarm.cache_report(tmp_path, 100, since)["warnings"] == []
    over_total = prewarm.cache_report(tmp_path, 4, since)  # 4.8 MiB > 4 MiB cap, demo set 1.9 MiB
    assert "over its cap" in over_total["warnings"][0]
    over_demo = prewarm.cache_report(tmp_path, 2, since)  # demo set is 95% of a 2 MiB cap
    assert "demo set" in over_demo["warnings"][0]


def test_budget_stops_on_growth_and_on_low_disk(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 2 * 1024 * 1024)
    prewarm.Budget(tmp_path, max_new_mb=5, min_free_gb=0, start_bytes=0).check()
    with pytest.raises(prewarm.StopRun, match="max-new-mb"):
        prewarm.Budget(tmp_path, max_new_mb=1, min_free_gb=0, start_bytes=0).check()
    with pytest.raises(prewarm.StopRun, match="disk free"):
        prewarm.Budget(tmp_path, max_new_mb=999, min_free_gb=10**6).check()


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.content, self.text = b"png", "err"

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def get(self, url, timeout):
        self.calls += 1
        return self.replies.pop(0)


def api_with(replies, monkeypatch):
    monkeypatch.setattr(prewarm.time, "sleep", lambda s: None)
    api = prewarm.Api("http://x", log=lambda *_: None)
    api.s = FakeSession(replies)
    return api


def test_get_retries_503_and_5xx_but_not_4xx(monkeypatch):
    api = api_with([FakeResp(503, headers={"Retry-After": "1"}), FakeResp(503), FakeResp(200, {"ok": 1})], monkeypatch)
    assert api.get("/api/x") == {"ok": 1}
    api = api_with([FakeResp(422), FakeResp(200, {})], monkeypatch)
    with pytest.raises(RuntimeError, match="422"):
        api.get("/api/x")
    assert api.s.calls == 1
    assert api_with([FakeResp(404)], monkeypatch).get("/api/x", ok404=True) is None


def test_poll_waits_then_returns_and_reports_failure(monkeypatch):
    api = api_with([FakeResp(200, {"status": "running", "stage": "downloading", "frames_done": 1, "frames_total": 9}),
                    FakeResp(200, {"status": "done", "result": {}})], monkeypatch)
    assert api.poll("/api/motion", 60)["status"] == "done"
    api = api_with([FakeResp(200, {"status": "failed", "error": "IRSA down"})], monkeypatch)
    with pytest.raises(RuntimeError, match="IRSA down"):
        api.poll("/api/motion", 60, label="motion")


class FakeApi:
    """Answers like the real API, and records the paths asked."""

    def __init__(self):
        self.paths = []

    def get(self, path, timeout=0, binary=False, ok404=False):
        self.paths.append(path)
        if path.startswith("/api/visits"):
            return {"n_visits": 3, "passes": [1]}
        if path.startswith("/api/frames"):
            return {"frames": [{"mjd": 1, "image_url": "/api/cutout.png?a=1"},
                               {"mjd": 100, "image_url": "/api/cutout.png?a=2"}]}
        if path.startswith("/api/cutout.png"):
            return b"png"
        if "evidence/surveys" in path:
            return {"images": [{"survey": "2mass"}], "unavailable": []}
        if "/evidence" in path:
            return {}
        raise AssertionError(path)

    def poll(self, path, timeout_s, every_s=4.0, label=""):
        self.paths.append(path)
        if label == "motion":
            return {"result": {"objects": [{"id": 0, "kind": "mover", "pm_ra_cosdec": 0.0, "pm_dec": 10.4},
                                           {"id": 1, "kind": "static"}]}}
        return {"result": {"points": [1, 2], "bands": [1]}}


def test_runner_walks_the_website_calls_and_records_failures(tmp_path):
    api = FakeApi()
    r = prewarm.Runner(api, prewarm.Budget(tmp_path, 10, 0), 2026.9, jobs=2)
    r.target(prewarm.select_targets("barnard")[0], with_spectrum=True)
    assert [x.task for x in r.results] == ["visits", "time-lapse frames", "time-lapse surveys", "sky-picture frames",
                                           "sky-picture surveys", "motion + evidence", "spectrum"]
    assert all(x.ok for x in r.results)
    assert any(p.startswith("/api/motion/evidence?") and "object=0" in p for p in api.paths)
    assert "ra=269.447300&dec=4.768500" in " ".join(api.paths)

    def boom(*_, **__):
        raise RuntimeError("502")
    api.get = boom
    r2 = prewarm.Runner(api, prewarm.Budget(tmp_path, 10, 0), 2026.9)
    r2.target(prewarm.select_targets("r-dra")[0], with_spectrum=False)
    assert not r2.results[0].ok
    assert len(r2.results) == 1  # visits failed: the rest of the target is skipped


def test_two_targets_failing_visits_in_a_row_stop_the_run(tmp_path):
    api = FakeApi()

    def down(*_, **__):
        raise RuntimeError("/api/visits: HTTP 502")
    api.get = down
    r = prewarm.Runner(api, prewarm.Budget(tmp_path, 10, 0), 2026.9)
    r.target(prewarm.select_targets("barnard")[0], with_spectrum=True)
    with pytest.raises(prewarm.StopRun, match="looks down"):
        r.target(prewarm.select_targets("r-dra")[0], with_spectrum=False)


def test_502_is_not_retried_by_the_client(monkeypatch):
    api = api_with([FakeResp(502), FakeResp(200, {"ok": 1})], monkeypatch)
    with pytest.raises(RuntimeError, match="502"):
        api.get("/api/x")
    assert api.s.calls == 1


def test_select_targets_rejects_unknown():
    with pytest.raises(SystemExit):
        prewarm.select_targets("barnard,nope")
    assert [t.id for t in prewarm.select_targets("all")] == ["barnard", "eps-indi-b", "wise0855", "rw-aur", "r-dra"]


def test_dry_run_downloads_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PLANETX_CACHE_DIR", str(tmp_path))
    assert prewarm.main(["--dry-run", "--targets", "barnard,wise0855"]) == 0
    out = capsys.readouterr().out
    assert "barnard" in out and "spectrum=no" in out and "wise0855" in out


def test_epoch_now_is_the_month_start_like_the_website():
    from datetime import UTC, datetime
    start = prewarm.decimal_year(datetime(2026, 11, 1, tzinfo=UTC))
    assert prewarm.epoch_now(datetime(2026, 11, 15, 13, 7, tzinfo=UTC)) == start
    assert prewarm.epoch_now(datetime(2026, 11, 30, 23, 59, tzinfo=UTC)) == start
    assert prewarm.epoch_now(datetime(2026, 12, 1, tzinfo=UTC)) > start


def test_barnard_timelapse_patch_matches_the_website_request():
    # What the website requested on 2026-11-15 (epochNow = 2026-11-01), read off the browser.
    from datetime import UTC, datetime
    year = prewarm.epoch_now(datetime(2026, 11, 15, tzinfo=UTC))
    t = prewarm.select_targets("barnard")[0]
    s = prewarm.sky_patch(t, year, prewarm.OLD_YEAR_TIMELAPSE, prewarm.MARGIN_TIMELAPSE, prewarm.MIN_SIDE_TIMELAPSE)
    assert (s["ra"], s["dec"], s["side"]) == (269.449, 4.731, 464)


def test_requests_match_what_the_website_sent():
    # Patch centres and spectrum queries the website's object cards sent on 2026-11-15, read off
    # the browser (before snapping; the website snaps the same way since).
    from datetime import UTC, datetime
    year = prewarm.epoch_now(datetime(2026, 11, 15, tzinfo=UTC))
    site_patch = {"eps-indi-b": (331.070717, -56.791873, 308), "wise0855": (133.788957, -7.244587, 403)}
    for tid, (ra, dec, side) in site_patch.items():
        t = prewarm.select_targets(tid)[0]
        s = prewarm.sky_patch(t, year, prewarm.OLD_YEAR_TIMELAPSE, prewarm.MARGIN_TIMELAPSE, prewarm.MIN_SIDE_TIMELAPSE)
        assert (s["ra"], s["dec"], s["side"]) == (*prewarm.snap_centre(ra, dec), side), tid
    by_id = {t.id: t for t in prewarm.TARGETS}
    assert prewarm.spectrum_query(by_id["eps-indi-b"], year) == (
        "ra=331.076453&dec=-56.793812&pmra=3982.0&pmdec=-2466.8&epoch=2016.000")
    assert prewarm.spectrum_query(by_id["wise0855"], year) == (
        "ra=133.757870&dec=-7.242030&pmra=-8123.7&pmdec=673.2&epoch=2026.833")


def test_snap_centre_wraps_ra():
    assert prewarm.snap_centre(359.9998, 0.0) == (0.0, 0.0)
