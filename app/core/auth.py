"""Single-user login.

The only account is configured in .env (AUTH_EMAIL / AUTH_PASSWORD); there is no built-in
default. A successful login sets an HMAC-signed, HttpOnly session cookie. The signing key is
derived from a persisted server secret *and* the configured credentials, so changing the
password (or email) immediately invalidates every existing session.

``AuthMiddleware`` enforces the session on every /api, /ws and API-docs request, for both HTTP
and WebSocket, except the few public paths below.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from http.cookies import SimpleCookie
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from app.core.config import get_settings

COOKIE_NAME = "atl_session"

# Reachable without a session: the login itself, logout (must work with an expired cookie),
# the container health check, and the Kite redirect. Zerodha sends the browser to the callback
# directly (often on a different host than the UI, so the session cookie is not sent); the
# callback instead checks a signed, short-lived ``state`` value that only /api/kite/login-url
# (which does need a session) hands out.
PUBLIC_PATHS = frozenset({"/api/auth/login", "/api/auth/logout", "/api/health", "/api/kite/callback"})


# ---------------------------------------------------------------------------
# Credentials and signed session tokens
# ---------------------------------------------------------------------------

def credentials_configured() -> bool:
    s = get_settings()
    return bool(s.auth_email.strip() and s.auth_password)


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def check_credentials(email: str, password: str) -> bool:
    """Constant-time comparison of both fields (always compares both, even if one is wrong)."""
    s = get_settings()
    if not credentials_configured():
        return False
    email_ok = hmac.compare_digest(_digest(email.strip().lower()), _digest(s.auth_email.strip().lower()))
    password_ok = hmac.compare_digest(_digest(password), _digest(s.auth_password))
    return email_ok and password_ok


_secret_lock = threading.Lock()


def _server_secret() -> bytes:
    """AUTH_SECRET if set, otherwise a random secret persisted in data/.auth_secret (survives restarts)."""
    s = get_settings()
    if s.auth_secret:
        return s.auth_secret.encode()
    path = s.data_dir / ".auth_secret"
    with _secret_lock:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_hex(32))
            try:
                path.chmod(0o600)
            except OSError:  # pragma: no cover - not supported on every filesystem
                pass
        return path.read_text().strip().encode()


def _signing_key() -> bytes:
    s = get_settings()
    material = f"{s.auth_email.strip().lower()}\n{s.auth_password}".encode()
    return hmac.new(_server_secret(), material, hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def session_seconds() -> int:
    return int(get_settings().auth_session_hours * 3600)


def issue_token(now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + session_seconds())
    payload = _b64(f"{get_settings().auth_email.strip().lower()}|{expires}".encode())
    signature = _b64(hmac.new(_signing_key(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_token(token: str | None, now: float | None = None) -> dict[str, Any] | None:
    """Return {"email", "expires_at"} for a valid, unexpired token signed with the current credentials."""
    if not token or "." not in token or not credentials_configured():
        return None
    payload, signature = token.rsplit(".", 1)
    expected = _b64(hmac.new(_signing_key(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        email, expires_text = _unb64(payload).decode().rsplit("|", 1)
        expires = int(expires_text)
    except (ValueError, UnicodeDecodeError):
        return None
    if expires < (now if now is not None else time.time()):
        return None
    return {"email": email, "expires_at": expires}


def issue_state(purpose: str, ttl: int = 600, now: float | None = None) -> str:
    """A signed, short-lived value proving a redirect flow was started by the logged-in user."""
    expires = int((now if now is not None else time.time()) + ttl)
    payload = _b64(f"{purpose}|{expires}|{secrets.token_hex(8)}".encode())
    signature = _b64(hmac.new(_signing_key(), f"state:{payload}".encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_state(state: str | None, purpose: str, now: float | None = None) -> bool:
    if not state or "." not in state:
        return False
    payload, signature = state.rsplit(".", 1)
    expected = _b64(hmac.new(_signing_key(), f"state:{payload}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        got_purpose, expires_text, _nonce = _unb64(payload).decode().split("|")
        expires = int(expires_text)
    except (ValueError, UnicodeDecodeError):
        return False
    return got_purpose == purpose and expires >= (now if now is not None else time.time())


# ---------------------------------------------------------------------------
# Brute-force protection
# ---------------------------------------------------------------------------

class LoginLimiter:
    """Locks a client IP out after ``max_failures`` failed logins within ``window`` seconds."""

    def __init__(self, max_failures: int = 5, window: float = 15 * 60) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, ip: str, now: float) -> deque[float]:
        q = self._failures.setdefault(ip, deque())
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def retry_after(self, ip: str, now: float | None = None) -> int:
        """Seconds until this IP may try again (0 = allowed now)."""
        now = now if now is not None else time.time()
        with self._lock:
            q = self._recent(ip, now)
            if len(q) < self.max_failures:
                return 0
            return max(1, int(q[0] + self.window - now) + 1)

    def record_failure(self, ip: str, now: float | None = None) -> int:
        """Record a failure; return how many attempts remain before lock-out."""
        now = now if now is not None else time.time()
        with self._lock:
            q = self._recent(ip, now)
            q.append(now)
            return max(0, self.max_failures - len(q))

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


login_limiter = LoginLimiter()


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def _is_protected(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return False
    return path.startswith(("/api/", "/ws/")) or path in ("/api", "/docs", "/redoc", "/openapi.json") or path.startswith("/docs/")


def session_cookie(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except Exception:  # noqa: BLE001 - malformed cookie header = no session
                return None
            morsel = jar.get(COOKIE_NAME)
            return morsel.value if morsel else None
    return None


class AuthMiddleware:
    """Pure ASGI middleware so it covers WebSocket handshakes as well as HTTP requests."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not get_settings().auth_enabled or not _is_protected(scope["path"]):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":  # CORS preflight carries no cookies
            await self.app(scope, receive, send)
            return
        user = verify_token(session_cookie(scope))
        if user is not None:
            scope.setdefault("state", {})["user"] = user
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await WebSocketClose(code=4401)(scope, receive, send)
            return
        response = JSONResponse(
            {"error": {"code": "UNAUTHENTICATED", "message": "Login required", "details": None}},
            status_code=401,
        )
        await response(scope, receive, send)
