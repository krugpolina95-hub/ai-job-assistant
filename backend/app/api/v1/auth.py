from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, Request, Response

from app.api.cookies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    clear_session_cookies,
    set_csrf_cookie,
    set_session_cookies,
)
from app.api.deps import AuthServiceDep, CsrfProtectedAuth, CurrentAuth, SettingsDep
from app.schemas.auth import LoginIn, RegisterIn, UserOut
from app.schemas.errors import ErrorOut

router = APIRouter(prefix="/auth", tags=["auth"])

UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED: {"model": ErrorOut, "description": "No valid session"}
}


@router.post(
    "/register",
    status_code=HTTPStatus.CREATED,
    response_model=UserOut,
    responses={HTTPStatus.CONFLICT: {"model": ErrorOut, "description": "Email already registered"}},
)
async def register(
    body: RegisterIn,
    request: Request,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
) -> UserOut:
    """Creates the account and signs the user in (session cookie + CSRF cookie)."""
    result = await service.register(body, user_agent=request.headers.get("user-agent"))
    set_session_cookies(
        response,
        token=result.session.token,
        expires_at=result.session.expires_at,
        persistent=result.session.persistent,
        secure=settings.session_cookie_secure,
    )
    return UserOut.model_validate(result.user)


@router.post(
    "/login",
    response_model=UserOut,
    responses={
        HTTPStatus.UNAUTHORIZED: {"model": ErrorOut, "description": "Invalid email or password"}
    },
)
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
) -> UserOut:
    result = await service.login(
        str(body.email),
        body.password.get_secret_value(),
        remember_me=body.remember_me,
        user_agent=request.headers.get("user-agent"),
        previous_token=request.cookies.get(SESSION_COOKIE),
    )
    set_session_cookies(
        response,
        token=result.session.token,
        expires_at=result.session.expires_at,
        persistent=result.session.persistent,
        secure=settings.session_cookie_secure,
    )
    return UserOut.model_validate(result.user)


@router.post(
    "/logout",
    status_code=HTTPStatus.NO_CONTENT,
    responses={
        **UNAUTHENTICATED,
        HTTPStatus.FORBIDDEN: {"model": ErrorOut, "description": "CSRF check failed"},
    },
)
async def logout(
    auth: CsrfProtectedAuth, response: Response, service: AuthServiceDep, settings: SettingsDep
) -> None:
    await service.logout(auth)
    clear_session_cookies(response, secure=settings.session_cookie_secure)


@router.get("/me", response_model=UserOut, responses=UNAUTHENTICATED)
async def me(
    auth: CurrentAuth, request: Request, response: Response, settings: SettingsDep
) -> UserOut:
    # Re-issue a lost CSRF cookie (e.g. a browser restart dropped it) so the client can
    # keep making unsafe requests without logging in again.
    if CSRF_COOKIE not in request.cookies:
        set_csrf_cookie(response, max_age=None, secure=settings.session_cookie_secure)
    return UserOut.model_validate(auth.user)
