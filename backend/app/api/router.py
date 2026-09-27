"""Registry of public API routers. Each feature module adds one `include_router` line here."""

from fastapi import APIRouter

from app.api import health
from app.api.errors import ERROR_RESPONSES
from app.api.v1 import auth, vacancies

API_V1_PREFIX = "/api/v1"

api_v1_router = APIRouter(prefix=API_V1_PREFIX, responses=ERROR_RESPONSES)
api_v1_router.include_router(health.router)
api_v1_router.include_router(auth.router)
api_v1_router.include_router(vacancies.router)
