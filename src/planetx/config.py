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


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    # Disk cache for downloaded cutouts and other IRSA responses.
    cache_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("PLANETX_CACHE_DIR", ".cache/planetx"))
    )
    # Cap on the cutout cache (MB); least recently used cutouts are evicted beyond it.
    cache_max_mb: int = field(default_factory=lambda: _positive_int("PLANETX_CACHE_MAX_MB", 2048))
    # SQLite database for the survey patch queue (and later, candidates).
    db_path: Path = field(
        default_factory=lambda: Path(os.environ.get("PLANETX_DB", "data/survey.sqlite"))
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
