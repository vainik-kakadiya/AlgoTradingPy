"""Single-user login: every API / WebSocket route is closed without a valid session."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core import auth
from app.core.config import get_settings
from app.main import app
from tests.conftest import TEST_EMAIL, TEST_PASSWORD


@pytest.fixture
def anon(client: TestClient) -> TestClient:
    """A fresh client without cookies (the shared `client` fixture is logged in)."""
    auth.login_limiter._failures.clear()
    return TestClient(app)


def test_api_requires_login(anon: TestClient) -> None:
    for path in ("/api/dashboard", "/api/strategies", "/api/settings", "/api/auth/me", "/openapi.json", "/docs"):
        r = anon.get(path)
        assert r.status_code == 401, path
        assert r.json()["error"]["code"] == "UNAUTHENTICATED"
    assert anon.post("/api/strategies", json={}).status_code == 401


def test_public_paths_stay_open(anon: TestClient) -> None:
    assert anon.get("/api/health").status_code == 200
    assert anon.post("/api/auth/logout").status_code == 200


def test_websocket_rejected_without_session(anon: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        with anon.websocket_connect("/ws/dashboard") as ws:
            ws.receive_text()
    assert exc.value.code == 4401


def test_login_logout_flow(anon: TestClient) -> None:
    r = anon.post("/api/auth/login", json={"email": "  OWNER@example.com ", "password": TEST_PASSWORD})
    assert r.status_code == 200
    assert r.json()["email"] == TEST_EMAIL
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie

    assert anon.get("/api/auth/me").json()["email"] == TEST_EMAIL
    assert anon.get("/api/dashboard").status_code == 200
    with anon.websocket_connect("/ws/dashboard") as ws:
        assert '"hello"' in ws.receive_text()

    anon.post("/api/auth/logout")
    assert anon.get("/api/dashboard").status_code == 401


def test_wrong_credentials_rejected(anon: TestClient) -> None:
    for email, password in ((TEST_EMAIL, "wrong"), ("intruder@example.com", TEST_PASSWORD), ("", "")):
        r = anon.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code in (401, 422)
    assert anon.get("/api/dashboard").status_code == 401


def test_lockout_after_repeated_failures(anon: TestClient) -> None:
    for _ in range(auth.login_limiter.max_failures):
        assert anon.post("/api/auth/login", json={"email": TEST_EMAIL, "password": "nope"}).status_code == 401
    r = anon.post("/api/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
    assert r.status_code == 429  # even the right password is refused while locked out
    assert r.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"
    auth.login_limiter._failures.clear()


def test_forged_and_expired_tokens_rejected(anon: TestClient) -> None:
    good = auth.issue_token()
    payload, sig = good.rsplit(".", 1)
    assert auth.verify_token(good) is not None
    assert auth.verify_token(payload + "." + sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")) is None
    assert auth.verify_token(auth._b64(b"owner@example.com|9999999999") + "." + sig) is None
    assert auth.verify_token(auth.issue_token(now=0)) is None  # expired long ago
    anon.cookies.set(auth.COOKIE_NAME, "garbage.token")
    assert anon.get("/api/dashboard").status_code == 401


def test_changing_password_invalidates_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    token = auth.issue_token()
    assert auth.verify_token(token) is not None
    monkeypatch.setattr(get_settings(), "auth_password", "a new password")
    assert auth.verify_token(token) is None


def test_no_credentials_means_no_login(anon: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "auth_password", "")
    r = anon.post("/api/auth/login", json={"email": TEST_EMAIL, "password": "anything"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "AUTH_NOT_CONFIGURED"


# --------------------------------------------------------------------------- Kite redirect
def _callback(c: TestClient, **params: str):
    return c.get("/api/kite/callback", params=params, follow_redirects=False)


def test_kite_callback_needs_state_or_session(anon: TestClient, client: TestClient, monkeypatch) -> None:
    from app.market_data.kite_client import kite_sessions

    exchanged: list[str] = []
    monkeypatch.setattr(kite_sessions, "complete_login", lambda token: exchanged.append(token))

    # Zerodha redirects the browser without our cookie (e.g. 127.0.0.1 vs localhost): no state -> rejected
    r = _callback(anon, status="success", request_token="tok-1")
    assert r.status_code in (302, 307) and r.headers["location"].endswith("/settings?kite=expired")
    # forged / wrong-purpose / expired state -> rejected
    assert _callback(anon, status="success", request_token="t", state="abc.def").headers["location"].endswith("kite=expired")
    assert _callback(anon, status="success", request_token="t", state=auth.issue_state("other")).headers["location"].endswith("kite=expired")
    old = auth.issue_state("kite-login", ttl=600, now=0)
    assert _callback(anon, status="success", request_token="t", state=old).headers["location"].endswith("kite=expired")
    assert exchanged == []

    # state handed out by /api/kite/login-url -> accepted even without the session cookie
    good = auth.issue_state("kite-login")
    r = _callback(anon, status="success", request_token="tok-2", state=good)
    assert r.headers["location"].endswith("/settings?kite=connected")
    # a logged-in browser is accepted without state too
    assert _callback(client, status="success", request_token="tok-3").headers["location"].endswith("kite=connected")
    assert exchanged == ["tok-2", "tok-3"]


def test_kite_login_url_carries_state(client: TestClient, monkeypatch) -> None:
    from urllib.parse import parse_qs, urlparse

    from app.market_data.kite_client import kite_sessions

    monkeypatch.setattr(kite_sessions, "login_url", lambda: "https://kite.zerodha.com/connect/login?v=3&api_key=k")
    url = client.get("/api/kite/login-url").json()["login_url"]
    qs = parse_qs(urlparse(url).query)
    assert qs["api_key"] == ["k"]
    state = parse_qs(qs["redirect_params"][0])["state"][0]
    assert auth.verify_state(state, "kite-login")
