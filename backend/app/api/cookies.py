"""Session and CSRF cookies (TD-03).

- `sid`: the opaque session token. HttpOnly, so JavaScript can never read it.
- `csrf`: a random value JavaScript reads and echoes in `X-CSRF-Token` on unsafe requests
  (double-submit). A cross-site page can make the browser send cookies, but can't read them.
"""

from datetime import UTC, datetime

from fastapi import Response

from app.core.security import generate_token

SESSION_COOKIE = "sid"
CSRF_COOKIE = "csrf"
CSRF_HEADER = "X-CSRF-Token"


def set_session_cookies(
    response: Response, *, token: str, expires_at: datetime, persistent: bool, secure: bool
) -> None:
    # Without "remember me" the cookies die with the browser (no Max-Age); the server-side
    # expiry still applies either way.
    max_age = int((expires_at - datetime.now(UTC)).total_seconds()) if persistent else None
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    set_csrf_cookie(response, max_age=max_age, secure=secure)


def set_csrf_cookie(response: Response, *, max_age: int | None, secure: bool) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        generate_token(),
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response, *, secure: bool) -> None:
    for name, httponly in ((SESSION_COOKIE, True), (CSRF_COOKIE, False)):
        response.delete_cookie(name, path="/", secure=secure, httponly=httponly, samesite="lax")
