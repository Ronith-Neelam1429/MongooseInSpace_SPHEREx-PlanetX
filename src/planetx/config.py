"""Runtime settings, read once from environment variables.

Everything has a working local default so `uv run uvicorn planetx.api.app:app`
works out of the box; deployments override via env vars.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import certifi

# python.org CPython builds ship without a CA bundle, so TLS to ESA/IRSA fails.
# Point at certifi's bundle (verification stays on). Must run before any HTTPS call.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    # Disk cache for downloaded cutouts and other IRSA responses.
    cache_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("PLANETX_CACHE_DIR", ".cache/planetx"))
    )
    # SQLite database for the survey patch queue (and later, candidates).
    db_path: Path = field(
        default_factory=lambda: Path(os.environ.get("PLANETX_DB", "data/survey.sqlite"))
    )
    # Observatories the motion engine may combine: "spherex" (SPHEREx only),
    # "multi" (+ 2MASS, AllWISE, CatWISE), "all" (+ NEOWISE), or a comma list.
    # Parsed by planetx.observatories.parse_observatories.
    observatories: str = field(
        default_factory=lambda: os.environ.get("PLANETX_OBSERVATORIES", "multi")
    )
    # Browser origins allowed to call the API (the Vite dev server by default).
    cors_origins: list[str] = field(
        default_factory=lambda: _split(
            os.environ.get(
                "PLANETX_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
            )
        )
    )


settings = Settings()
