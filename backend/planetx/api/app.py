"""FastAPI application. Run locally with:

    uv run uvicorn planetx.api.app:app --reload

Interactive API docs at http://127.0.0.1:8000/docs
"""
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from planetx.api.routes import sky, survey
from planetx.api.schemas import Health
from planetx.config import settings

app = FastAPI(title="SPHEREx PlanetX API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)

api = APIRouter(prefix="/api")


@api.get("/health")
def health() -> Health:
    return Health()


api.include_router(sky.router)
api.include_router(survey.router)
app.include_router(api)
