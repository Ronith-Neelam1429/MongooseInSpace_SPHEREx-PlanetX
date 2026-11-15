"""Regenerate the fixture result files in this folder (docs/RESULTS.md shapes).

Made-up but realistic numbers for developing and testing the Results page without
running the analysis tools. Never copy these into results/: they are not measurements.

    uv run python tests/fixtures/results/make_fixtures.py
"""
import json
import math
import random
from pathlib import Path

OUT = Path(__file__).parent
GENERATED_AT = "2026-11-14T22:00:00Z"
rng = random.Random(42)


def header(name, command, notes):
    return {"name": name, "version": 1, "generated_at": GENERATED_AT, "command": command,
            "notes": ["Fixture: made-up numbers for developing the Results page, not a real run.", *notes]}


def r(v, n=4):
    return round(v, n)


def gaia_validation():
    fields = [(270.0 + 2 * i, 66.56 - 0.8 * (i % 3)) for i in range(6)]
    objects, per_field = [], []
    for fra, fdec in fields:
        n = rng.randint(45, 70)
        per_field.append({"ra": fra, "dec": fdec, "n_objects": n + rng.randint(5, 20), "n_matched": n})
        for _ in range(n):
            g = rng.uniform(9.5, 18.5)
            snr = max(4.0, 400 * 10 ** (-0.4 * (g - 10)) * rng.uniform(0.7, 1.3))
            err = min(4.0, max(0.08, 18.0 / snr * rng.uniform(0.8, 1.2)))
            fast = rng.random() < 0.06
            total = rng.uniform(0.6, 4.5) if fast else 10 ** rng.uniform(-2.7, -0.9)
            pa = rng.uniform(0, 2 * math.pi)
            gra, gdec = total * math.sin(pa), total * math.cos(pa)
            gerr = 10 ** rng.uniform(-4.6, -3.2) * (1 + max(0.0, g - 15))
            sra, sdec = gra + rng.gauss(0, err * 1.08), gdec + rng.gauss(0, err * 1.08)
            mover = math.hypot(sra, sdec) / err > 5 and math.hypot(sra, sdec) > 0.5
            if not fast and rng.random() < 0.015:  # a blend posing as a mover
                sra, mover = sra + 5 * err, True
            truth = "static" if total < 0.05 else ("real_mover" if total > 3 * err and total > 0.5 else "ambiguous")
            objects.append({
                "ra": r(fra + rng.uniform(-0.1, 0.1), 6), "dec": r(fdec + rng.uniform(-0.1, 0.1), 6),
                "g_mag": r(g, 2), "snr": r(snr, 1), "n_passes": rng.choice([3, 3, 3, 4, 5]),
                "spherex_pm_ra": r(sra), "spherex_pm_dec": r(sdec), "spherex_pm_err": r(err),
                "gaia_pm_ra": r(gra, 5), "gaia_pm_dec": r(gdec, 5), "gaia_pm_err": r(gerr, 6),
                "engine_is_mover": mover, "truth": truth,
            })
    confusion = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for o in objects:
        if o["truth"] == "real_mover":
            confusion["tp" if o["engine_is_mover"] else "fn"] += 1
        elif o["truth"] == "static":
            confusion["fp" if o["engine_is_mover"] else "tn"] += 1
    res = sorted(math.hypot(o["spherex_pm_ra"] - o["gaia_pm_ra"], o["spherex_pm_dec"] - o["gaia_pm_dec"]) for o in objects)
    median = res[len(res) // 2]
    pulls = [(o["spherex_pm_ra"] - o["gaia_pm_ra"]) / math.hypot(o["spherex_pm_err"], o["gaia_pm_err"]) for o in objects]
    mean = sum(pulls) / len(pulls)
    doc = header("gaia_validation", "uv run python tools/gaia_validation.py --fields 6",
                 ["Gaia DR3 motions are taken as the truth; SPHEREx is measured on its own (no archival refit)."])
    doc.update({
        "fields": per_field,
        "objects": objects,
        "summary": {
            "n_objects": sum(f["n_objects"] for f in per_field), "n_matched": len(objects),
            "residual_median": r(median), "residual_mad": r(sorted(abs(x - median) for x in res)[len(res) // 2]),
            "pull_std": r(math.sqrt(sum((p - mean) ** 2 for p in pulls) / len(pulls)), 3),
            "confusion": confusion,
            "false_mover_rate": r(confusion["fp"] / max(1, confusion["fp"] + confusion["tn"])),
            "recovered_fraction": r(confusion["tp"] / max(1, confusion["tp"] + confusion["fn"])),
        },
    })
    return doc


EVIDENCE = {
    "blend": "A brighter neighbour 5.8\" away pulls the centroid in one pass only.",
    "saturated_halo": "Sits in the halo of a saturated star; its position jumps with the halo.",
    "edge": "Within 2 px of the cutout edge in the first pass.",
    "flagged_pixels": "Half of its pixels carry HOT or BLOOM flags in one pass.",
    "low_snr": "SNR below 8 in every pass; the track is noise.",
    "wavelength_centroid": "Its centroid shifts with wavelength: a red and a blue star in one pixel.",
    "parallax_misfit": "A nearby star whose parallax the 3-pass fit took for motion.",
    "chance_link": "Two unrelated faint sources linked across passes.",
    "other": "Not understood yet.",
}


def false_movers():
    causes = (["blend"] * 6 + ["saturated_halo"] * 4 + ["edge"] * 2 + ["flagged_pixels"] * 2 + ["low_snr"] * 3
              + ["wavelength_centroid", "parallax_misfit", "chance_link", "chance_link", "other"])
    movers = [{
        "ra": r(rng.uniform(268, 282), 6), "dec": r(rng.uniform(64, 68), 6), "field_ra": 270.0, "field_dec": 66.56,
        "pm_total": r(rng.uniform(1, 9), 3), "significance": r(rng.uniform(5, 14), 2), "snr": r(rng.uniform(6, 80), 1),
        "truth": "static", "cause": c, "evidence": EVIDENCE[c],
    } for c in causes]
    movers += [{
        "ra": r(rng.uniform(268, 282), 6), "dec": r(rng.uniform(64, 68), 6), "field_ra": 270.0, "field_dec": 66.56,
        "pm_total": r(rng.uniform(0.6, 4), 3), "significance": r(rng.uniform(6, 30), 2), "snr": r(rng.uniform(20, 300), 1),
        "truth": "real_mover", "cause": None, "evidence": "Gaia DR3 lists the same motion.",
    } for _ in range(9)]
    by_cause = {}
    for c in causes:
        by_cause[c] = by_cause.get(c, 0) + 1
    doc = header("false_movers", "uv run python tools/false_mover_audit.py",
                 ["Causes come from automatic pixel checks; 'other' was not inspected."])
    doc.update({"movers": movers, "summary": {"n_movers": len(movers), "n_fake": len(causes), "by_cause": by_cause}})
    return doc


def brown_dwarfs():
    objects = []

    def add(kind, name, ch4, h2o, pm, snr, flag):
        objects.append({
            "ra": r(rng.uniform(0, 360), 6), "dec": r(rng.uniform(-60, 70), 6), "name": name, "pm_total": r(pm, 3),
            "kind": kind, "methane_depth": r(ch4, 3), "methane_err": r(rng.uniform(0.02, 0.06), 3),
            "water_depth": r(h2o, 3), "water_err": r(rng.uniform(0.02, 0.06), 3),
            "color_1_4um": r(rng.uniform(0.5, 3.5) if flag == "suggests_brown_dwarf" else rng.uniform(-0.3, 0.6), 2),
            "snr": r(snr, 1), "flag": flag,
        })

    for _ in range(40):
        add("reference_star", None, rng.gauss(0.0, 0.04), rng.gauss(0.02, 0.04), rng.uniform(0, 0.08), rng.uniform(30, 400), "star_like")
    for name, ch4, h2o in [("eps Ind B", 0.52, 0.41), ("WISE J085510.83-071442.5", 0.61, 0.18),
                           ("2MASS J04151954-0935066", 0.47, 0.44), ("Luhman 16", 0.21, 0.33), ("Gliese 229 B", 0.55, 0.47)]:
        add("known_brown_dwarf", name, ch4, h2o, rng.uniform(1, 8), rng.uniform(15, 80), "suggests_brown_dwarf")
    for i in range(12):
        cool = i < 2
        add("mover", None, rng.uniform(0.25, 0.45) if cool else rng.gauss(0.02, 0.05),
            rng.uniform(0.25, 0.4) if cool else rng.gauss(0.02, 0.05), rng.uniform(0.5, 4), rng.uniform(8, 120),
            "suggests_brown_dwarf" if cool else rng.choice(["star_like", "star_like", "too_faint", "contaminated"]))
    doc = header("brown_dwarfs", "uv run python tools/brown_dwarf_search.py",
                 ["Depths are fractions of the continuum. Methane and water together suggest a brown dwarf; they do not prove one."])
    doc.update({"objects": objects, "summary": {
        "n_objects": len(objects), "n_suggests_brown_dwarf": sum(o["flag"] == "suggests_brown_dwarf" for o in objects)}})
    return doc


def light_curve_r_dra():
    points = []
    for start, n in [(60890, 18), (61080, 20), (61260, 14)]:
        for i in range(n):
            mjd = start + i * 2.6 + rng.uniform(-0.5, 0.5)
            flux = 2400 * (1 + 0.62 * math.sin(2 * math.pi * (mjd - 60900) / 245.6))
            points.append({"mjd": r(mjd, 3), "flux": r(flux * rng.gauss(1, 0.03), 1), "flux_err": r(flux * 0.025, 1),
                           "wavelength": r(rng.uniform(2.18, 2.26), 3)})
    doc = header("light_curve_r_dra", "uv run python tools/brown_dwarf_search.py --light-curve",
                 ["Only exposures between 2.18 and 2.26 um, so the change is in brightness, not in wavelength."])
    doc.update({"points": points, "period_days_fit": 241.0, "period_days_literature": 245.6})
    return doc


def injection_recovery():
    grid = []
    for pm in [0.25, 0.5, 1, 2, 4, 8, 16]:
        for mag in [10, 11, 12, 13, 14, 15, 16]:
            n = 40
            p = 1 / (1 + math.exp(-(math.log2(pm) - 0.2 - 0.55 * (mag - 12)) * 2.2)) * (0.98 if mag < 15 else 0.85)
            rec = sum(rng.random() < p for _ in range(n))
            grid.append({"pm_arcsec_yr": pm, "mag_bin": mag, "n_injected": n, "n_recovered": rec,
                         "n_flagged_mover": rec, "recovered_fraction": r(rec / n, 3)})
    doc = header("injection_recovery", "uv run python tools/injection_recovery.py --fields 4",
                 ["Brightness is the SPHEREx magnitude near 3.4 um."])
    doc.update({"grid": grid, "false_positives": {"n_fields": 4, "n_static_injected": 400, "n_flagged_mover": 3},
                "summary": {"pm_50": 1.4, "pm_90": 3.6, "mag_50": 14.6}})
    return doc


if __name__ == "__main__":
    for make in (gaia_validation, false_movers, brown_dwarfs, light_curve_r_dra, injection_recovery):
        doc = make()
        path = OUT / f"{doc['name']}.json"
        path.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
        print(path.name, path.stat().st_size)
