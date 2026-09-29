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
from app.api.deps import (
    AuthServiceDep,
    ClientInfoDep,
    CsrfProtectedAuth,
    CurrentAuth,
    PasswordResetServiceDep,
    SettingsDep,
)
from app.schemas.auth import (
    ChangePasswordIn,
    ForgotPasswordIn,
    LoginIn,
    RegisterIn,
    ResetPasswordIn,
    UserOut,
)
from app.schemas.errors import ErrorOut

router = APIRouter(prefix="/auth", tags=["auth"])

UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED: {"model": ErrorOut, "description": "No valid session"}
}
RATE_LIMITED: dict[int | str, dict[str, Any]] = {
    HTTPStatus.TOO_MANY_REQUESTS: {
        "model": ErrorOut,
        "description": "Too many attempts; see the Retry-After header",
    }
}


@router.post(
    "/register",
    status_code=HTTPStatus.CREATED,
    response_model=UserOut,
    responses={
        HTTPStatus.CONFLICT: {"model": ErrorOut, "description": "Email already registered"},
        **RATE_LIMITED,
    },
)
async def register(
    body: RegisterIn,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
    client: ClientInfoDep,
) -> UserOut:
    """Creates the account and signs the user in (session cookie + CSRF cookie)."""
    result = await service.register(body, client)
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
        HTTPStatus.UNAUTHORIZED: {"model": ErrorOut, "description": "Invalid email or password"},
        **RATE_LIMITED,
    },
)
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
    client: ClientInfoDep,
) -> UserOut:
    """A locked account (too many failed attempts) gets the same 401 as a wrong password."""
    result = await service.login(
        str(body.email),
        body.password.get_secret_value(),
        remember_me=body.remember_me,
        client=client,
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


@router.post(
    "/change-password",
    status_code=HTTPStatus.NO_CONTENT,
    responses={
        **UNAUTHENTICATED,
        HTTPStatus.BAD_REQUEST: {"model": ErrorOut, "description": "Wrong current password"},
        HTTPStatus.FORBIDDEN: {"model": ErrorOut, "description": "CSRF check failed"},
        **RATE_LIMITED,
    },
)
async def change_password(
    body: ChangePasswordIn,
    auth: CsrfProtectedAuth,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
    client: ClientInfoDep,
) -> None:
    """Signs out every session, this one included, and sets a fresh session cookie."""
    issued = await service.change_password(
        auth,
        body.current_password.get_secret_value(),
        body.new_password.get_secret_value(),
        client,
    )
    set_session_cookies(
        response,
        token=issued.token,
        expires_at=issued.expires_at,
        persistent=issued.persistent,
        secure=settings.session_cookie_secure,
    )


@router.post(
    "/forgot-password",
    status_code=HTTPStatus.ACCEPTED,
    response_class=Response,
    responses=RATE_LIMITED,
)
async def forgot_password(
    body: ForgotPasswordIn, service: PasswordResetServiceDep, client: ClientInfoDep
) -> Response:
    """Always `202` with an empty body, whether or not the email has an account. If it has,
    a single-use reset link is emailed."""
    await service.request_reset(str(body.email), client)
    return Response(status_code=HTTPStatus.ACCEPTED)


@router.post(
    "/reset-password",
    status_code=HTTPStatus.NO_CONTENT,
    responses={
        HTTPStatus.BAD_REQUEST: {"model": ErrorOut, "description": "Invalid or expired link"},
        **RATE_LIMITED,
    },
)
async def reset_password(
    body: ResetPasswordIn,
    response: Response,
    service: PasswordResetServiceDep,
    settings: SettingsDep,
    client: ClientInfoDep,
) -> None:
    """Sets the new password and signs the user out everywhere; they log in again."""
    await service.reset_password(body.token, body.new_password.get_secret_value(), client)
    clear_session_cookies(response, secure=settings.session_cookie_secure)
