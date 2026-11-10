"""Login / logout / current user."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.api.errors import ApiError
from app.core.auth import (
    COOKIE_NAME,
    check_credentials,
    credentials_configured,
    issue_token,
    login_limiter,
    session_seconds,
    verify_token,
)
from app.services.event_bus import record_event

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    ip = _client_ip(request)
    wait = login_limiter.retry_after(ip)
    if wait:
        raise ApiError(429, "TOO_MANY_ATTEMPTS", f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).",
                       {"retry_after_seconds": wait})
    if not credentials_configured():
        raise ApiError(503, "AUTH_NOT_CONFIGURED", "Login is not configured on the server. Set AUTH_EMAIL and AUTH_PASSWORD in AlgoTradingPy/.env.")
    if not check_credentials(body.email, body.password):
        remaining = login_limiter.record_failure(ip)
        record_event("WARNING", "LOGIN_FAILED", "auth", f"Failed login attempt from {ip}", {"ip": ip, "attempts_left": remaining})
        await asyncio.sleep(1.0)  # slows down guessing
        raise ApiError(401, "INVALID_CREDENTIALS", "Incorrect email or password.", {"attempts_left": remaining})

    login_limiter.reset(ip)
    token = issue_token()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=session_seconds(),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    record_event("INFO", "LOGIN", "auth", f"Logged in from {ip}", {"ip": ip})
    user = verify_token(token)
    assert user is not None
    return user


@router.get("/me")
def me(request: Request) -> dict[str, Any]:
    # AuthMiddleware has already validated the session and stored it on the request.
    return request.state.user


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax")
    return {"ok": True}
