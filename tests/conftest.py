"""Test configuration: every test run uses a fresh temporary database and data directory,
no Kite credentials and no automatic feed start."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="algotrading_test_"))
os.environ.update({
    "DATABASE_URL": f"sqlite:///{(_TMP / 'test.db').as_posix()}",
    "DATA_DIR": str(_TMP),
    "AUTO_START_FEED": "false",
    "KITE_API_KEY": "",
    "KITE_API_SECRET": "",
    "KITE_ACCESS_TOKEN": "",
    "CAPTURE_TICKS": "false",
})

import pytest  # noqa: E402

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def database() -> str:
    from app.database.migrate import upgrade_database
    from app.database.session import init_engine

    url = get_settings().resolved_database_url()
    upgrade_database(url)
    init_engine(url)
    return url


@pytest.fixture(scope="session")
def client(database: str):  # noqa: ANN201
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
