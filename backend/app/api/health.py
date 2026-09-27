from fastapi import APIRouter, Response, status

from app.api.deps import ReadinessChecksDep, SettingsDep
from app.schemas.health import CheckStatus, HealthOut, ReadinessOut
from app.services.readiness import check_readiness

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    return HealthOut()


@router.get(
    "/ready",
    response_model=ReadinessOut,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessOut}},
)
async def ready(
    response: Response, settings: SettingsDep, checks: ReadinessChecksDep
) -> ReadinessOut:
    results = await check_readiness(checks, settings.readiness_timeout_seconds)
    is_ready = all(results.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessOut(
        status=CheckStatus.OK if is_ready else CheckStatus.UNAVAILABLE,
        checks={
            name: CheckStatus.OK if ok else CheckStatus.UNAVAILABLE for name, ok in results.items()
        },
    )
