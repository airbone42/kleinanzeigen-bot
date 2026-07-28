"""Pytest configuration.

``config.settings`` instantiates ``Settings()`` at import time (config/settings.py),
and ``telegram_bot_token`` / ``openrouter_api_key`` are required fields. In a clean CI
environment without a ``.env`` file, importing any module that transitively imports
``config.settings`` would raise ``ValidationError`` during test collection.

Setting dummy values here — conftest is imported before test modules are collected —
lets the whole suite import cleanly. ``setdefault`` keeps any real environment intact.

We also point ``DATABASE_PATH`` at a throwaway temp file and create the schema once, so
DB-backed code paths (e.g. ``get_published_listings_summary``) work without a pre-existing
database.
"""
import asyncio
import os
import tempfile

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0000000000:TEST_DUMMY_TOKEN_FOR_PYTEST")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-key-for-pytest")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_IDS", "123456789")

_TEST_DB = os.path.join(tempfile.gettempdir(), "kleinanzeigen_test_bot.db")
os.environ.setdefault("DATABASE_PATH", _TEST_DB)

import pytest


@pytest.fixture(scope="session", autouse=True)
def _init_test_database():
    """Create the SQLite schema in a fresh temp database for the whole test session."""
    try:
        os.remove(_TEST_DB)
    except FileNotFoundError:
        pass
    from db.database import init_database

    asyncio.run(init_database())
    yield
