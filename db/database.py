"""Async SQLite database connection management."""
import logging
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite

from config.settings import settings

logger = logging.getLogger(__name__)

CREATE_DRAFTS_TABLE = """
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    price REAL NOT NULL,
    category TEXT DEFAULT '',
    shipping_type TEXT DEFAULT 'PICKUP',
    shipping_size TEXT DEFAULT 'PICKUP',
    image_analysis TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    listing_id TEXT DEFAULT NULL,
    listing_url TEXT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_LISTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL UNIQUE,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    price REAL NOT NULL,
    category TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    days_remaining INTEGER DEFAULT NULL,
    shipping_type TEXT DEFAULT 'SHIPPING',
    shipping_costs REAL DEFAULT NULL,
    shipping_options TEXT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_DRAFT_IMAGES_TABLE = """
CREATE TABLE IF NOT EXISTS draft_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
)
"""

CREATE_DISCOGS_MAPPINGS_TABLE = """
CREATE TABLE IF NOT EXISTS discogs_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    discogs_release_id TEXT NOT NULL,
    discogs_inventory_id TEXT DEFAULT '',
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    label TEXT DEFAULT '',
    year INTEGER DEFAULT NULL,
    media_condition TEXT DEFAULT '',
    discogs_price REAL DEFAULT 0,
    median_price REAL DEFAULT 0,
    image_url TEXT DEFAULT '',
    kleinanzeigen_listing_id TEXT DEFAULT '',
    kleinanzeigen_listing_url TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    last_seen_inventory_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chat_id, discogs_release_id)
)
"""


async def get_connection() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Get an async database connection."""
    db_path = settings.database_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn


async def init_database() -> None:
    """Initialize database schema."""
    logger.info("Initializing database schema")
    db_path = settings.database_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(CREATE_DRAFTS_TABLE)
        await conn.execute(CREATE_LISTINGS_TABLE)
        await conn.execute(CREATE_DRAFT_IMAGES_TABLE)
        await conn.execute(CREATE_DISCOGS_MAPPINGS_TABLE)

        # Migration: add shipping_type column to existing databases
        try:
            await conn.execute(
                "ALTER TABLE drafts ADD COLUMN shipping_type TEXT DEFAULT 'PICKUP'"
            )
            logger.info("Migration: added shipping_type column to drafts table")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise  # Only ignore the idempotent "column already exists" case

        # Migration: add shipping_size column to existing databases
        try:
            await conn.execute(
                "ALTER TABLE drafts ADD COLUMN shipping_size TEXT DEFAULT 'PICKUP'"
            )
            logger.info("Migration: added shipping_size column to drafts table")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise  # Only ignore the idempotent "column already exists" case

        # Migration: add shipping fields to listings table
        for col, definition in [
            ("shipping_type", "TEXT DEFAULT 'SHIPPING'"),
            ("shipping_costs", "REAL DEFAULT NULL"),
            ("shipping_options", "TEXT DEFAULT NULL"),
        ]:
            try:
                await conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {definition}")
                logger.info(f"Migration: added {col} column to listings table")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    raise  # Only ignore the idempotent "column already exists" case

        await conn.commit()
    logger.info("Database initialized successfully")
