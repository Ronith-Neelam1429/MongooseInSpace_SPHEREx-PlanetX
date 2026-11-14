"""Survey endpoints: crawler progress and patch coverage for the sky-map overlay."""
from fastapi import APIRouter

router = APIRouter(prefix="/survey", tags=["survey"])
