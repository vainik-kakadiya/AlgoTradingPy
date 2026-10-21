"""Zerodha Kite Connect session management - market data only.

The KiteConnect client is always wrapped in ``MarketDataOnlyKite``, which exposes an
explicit allowlist of non-trading methods. Order placement / modification / cancellation
methods are unreachable through this module.

Access tokens are stored only on the backend, in ``data/.kite_session.json`` (git-ignored),
and are never returned by the API.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.safety import MarketDataOnlyKite
from app.core.timeutil import ist_date, utcnow

logger = logging.getLogger(__name__)


class KiteNotConfigured(RuntimeError):
    pass


class KiteNotAuthenticated(RuntimeError):
    pass


class KiteSessionManager:
    def __init__(self, token_file: Path | None = None) -> None:
        self.settings = get_settings()
        self.token_file = token_file or (self.settings.data_dir / ".kite_session.json")
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._token_date: date | None = None
        self._user: dict[str, Any] | None = None
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if self.settings.kite_access_token:
            self._access_token = self.settings.kite_access_token
            self._token_date = ist_date(utcnow())
            return
        try:
            data = json.loads(self.token_file.read_text())
            self._access_token = data.get("access_token")
            self._token_date = date.fromisoformat(data["date"]) if data.get("date") else None
            self._user = data.get("user")
        except (FileNotFoundError, ValueError, KeyError):
            pass

    def _save(self) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps({
            "access_token": self._access_token,
            "date": self._token_date.isoformat() if self._token_date else None,
            "user": self._user,
        }))

    # ------------------------------------------------------------------ status
    @property
    def configured(self) -> bool:
        return self.settings.kite_configured

    @property
    def has_token_for_today(self) -> bool:
        # Kite access tokens expire daily (around 06:00 IST the next morning).
        return bool(self._access_token) and self._token_date == ist_date(utcnow())

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "authenticated": self.has_token_for_today,
            "token_date": self._token_date.isoformat() if self._token_date else None,
            "user_id": (self._user or {}).get("user_id"),
            "user_name": (self._user or {}).get("user_name"),
        }

    # ------------------------------------------------------------------ clients
    def _raw_client(self) -> Any:
        if not self.configured:
            raise KiteNotConfigured("KITE_API_KEY / KITE_API_SECRET are not configured in AlgoTradingPy/.env")
        from kiteconnect import KiteConnect

        client = KiteConnect(api_key=self.settings.kite_api_key)
        if self._access_token:
            client.set_access_token(self._access_token)
        return client

    def client(self) -> MarketDataOnlyKite:
        """A market-data-only Kite client for the current session."""
        if not self.has_token_for_today:
            raise KiteNotAuthenticated("No valid Kite access token for today; log in via the dashboard")
        return MarketDataOnlyKite(self._raw_client())

    def login_url(self) -> str:
        return MarketDataOnlyKite(self._raw_client()).login_url()

    def complete_login(self, request_token: str) -> dict[str, Any]:
        kite = MarketDataOnlyKite(self._raw_client())
        data = kite.generate_session(request_token, api_secret=self.settings.kite_api_secret)
        with self._lock:
            self._access_token = data["access_token"]
            self._token_date = ist_date(utcnow())
            self._user = {"user_id": data.get("user_id"), "user_name": data.get("user_name")}
            self._save()
        logger.info("Kite session established for user %s", self._user.get("user_id"))
        return self.status()

    def logout(self) -> None:
        with self._lock:
            if self._access_token and self.configured:
                try:
                    MarketDataOnlyKite(self._raw_client()).invalidate_access_token(self._access_token)
                except Exception:  # noqa: BLE001
                    logger.warning("Could not invalidate Kite token remotely")
            self._access_token = None
            self._token_date = None
            self._user = None
            try:
                self.token_file.unlink()
            except FileNotFoundError:
                pass

    def ticker_credentials(self) -> tuple[str, str]:
        if not self.has_token_for_today or not self._access_token:
            raise KiteNotAuthenticated("No valid Kite access token for today")
        return self.settings.kite_api_key, self._access_token


kite_sessions = KiteSessionManager()
