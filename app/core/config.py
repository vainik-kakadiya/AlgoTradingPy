"""Application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_ROOT / ".env", extra="ignore")

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    frontend_url: str = "http://localhost:5173"

    database_url: str = f"sqlite:///{(BACKEND_ROOT / 'data' / 'algotrading.db').as_posix()}"
    data_dir: Path = BACKEND_ROOT / "data"

    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_access_token: str = ""

    instrument_underlyings: str = "NIFTY,BANKNIFTY,FINNIFTY,SENSEX"
    auto_start_feed: bool = True
    capture_ticks: bool = True
    capture_second_bars: bool = True

    # Market-data quality
    feed_stale_seconds: float = Field(default=10.0, gt=0)
    candle_close_grace_seconds: float = Field(default=1.5, ge=0)

    log_level: str = "INFO"

    # Single-user login (see app/core/auth.py). No default credentials: login is impossible until set.
    auth_enabled: bool = True
    auth_required: bool = False  # true (set by docker-compose): refuse to start without AUTH_EMAIL / AUTH_PASSWORD
    auth_email: str = ""
    auth_password: str = ""
    auth_secret: str = ""  # optional; otherwise a random secret is persisted in data/.auth_secret
    auth_session_hours: float = Field(default=168.0, gt=0)  # 7 days

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def underlying_list(self) -> list[str]:
        return [u.strip().upper() for u in self.instrument_underlyings.split(",") if u.strip()]

    @property
    def kite_configured(self) -> bool:
        return bool(self.kite_api_key and self.kite_api_secret)

    def resolved_database_url(self) -> str:
        """Resolve relative sqlite paths against the backend root, not the CWD."""
        url = self.database_url
        prefix = "sqlite:///"
        if url.startswith(prefix) and not url.startswith("sqlite:////"):
            path = url[len(prefix):]
            if path != ":memory:" and not Path(path).is_absolute():
                return prefix + (BACKEND_ROOT / path).resolve().as_posix()
        return url

    def public_dict(self) -> dict:
        """Settings safe to expose through the API (no secrets)."""
        return {
            "api_host": self.api_host,
            "api_port": self.api_port,
            "frontend_url": self.frontend_url,
            "database": "sqlite" if self.database_url.startswith("sqlite") else "other",
            "kite_api_key_configured": bool(self.kite_api_key),
            "kite_api_secret_configured": bool(self.kite_api_secret),
            "instrument_underlyings": self.underlying_list,
            "auto_start_feed": self.auto_start_feed,
            "capture_ticks": self.capture_ticks,
            "capture_second_bars": self.capture_second_bars,
            "feed_stale_seconds": self.feed_stale_seconds,
            "execution_mode": "PAPER_ONLY",
            "auth_enabled": self.auth_enabled,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
