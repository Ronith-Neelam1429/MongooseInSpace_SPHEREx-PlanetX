"""Reference values for test targets (import as ``from targets import ...``)."""

# Gaia DR3 4472832130942575872 (Barnard's Star), epoch J2016.0.
BARNARD_RA_DEG = 269.44850252
BARNARD_DEC_DEG = 4.73942005
BARNARD_PMRA_COSDEC = -801.551  # mas/yr
BARNARD_PMDEC = 10362.394  # mas/yr
BARNARD_DISTANCE_PC = 1.83

# Cutout centres of the engine's real-target tests (tests/engine/test_engine_real.py),
# fixed so those tests never ask SIMBAD. They are what tools/fetch.py's `resolve` gave
# on 2026-11-14 (SIMBAD J2000 positions moved by proper motion to 2026.0), kept at full
# precision because tools/fetch.py names its cached cutouts after them.
ENGINE_TARGETS_DEG = {
    "barnard": (269.4462678797139, 4.768204454786656),
    "wise0855": (133.75976524998046, -7.24218218007099),
    "eps-indi-b": (331.09666878308445, -56.8006491764846),
    "rw-aur": (76.95658657548722, 30.401291346097146),
    "r-dra": (248.16723307211967, 66.75502382621286),
    # North Ecliptic Pole (RA 18h, Dec +66.56): where the survey crawl starts.
    "nep": (270.0, 66.5607),
}
