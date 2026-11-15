"""FastAPI application. Run locally with:

    uv run uvicorn planetx.api.app:app --reload

Interactive API docs at http://127.0.0.1:8000/docs
"""
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from planetx.api import motion as runs
from planetx.api import spectrum as spectra
from planetx.api.routes import candidates, evidence, motion, profile, results, sky, spectrum, survey
from planetx.api.schemas import Health
from planetx.config import settings
from planetx.profile import service as profiles


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    runs.shutdown()  # queued engine runs would otherwise delay exit
    spectra.shutdown()
    profiles.shutdown()


app = FastAPI(title="SPHEREx PlanetX API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)

api = APIRouter(prefix="/api")


@api.get("/health")
async def health() -> Health:
    return Health()


api.include_router(sky.router)
api.include_router(motion.router)
api.include_router(evidence.router)
api.include_router(candidates.router)
api.include_router(survey.router)
api.include_router(spectrum.router)
api.include_router(results.router)
api.include_router(profile.router)
app.include_router(api)
