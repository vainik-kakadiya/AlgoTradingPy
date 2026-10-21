"""Programmatic Alembic upgrade used on backend start-up."""

from __future__ import annotations

from alembic import command
from alembic.config import Config

from app.core.config import BACKEND_ROOT, get_settings


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url or get_settings().resolved_database_url())
    return cfg


def upgrade_database(url: str | None = None) -> None:
    command.upgrade(alembic_config(url), "head")
