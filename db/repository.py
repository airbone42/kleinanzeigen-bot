"""CRUD repository for drafts and listings."""
import logging
from typing import Optional

import aiosqlite

from db.database import get_connection

logger = logging.getLogger(__name__)


class DraftRepository:
    """Repository for managing listing drafts."""

    async def create(
        self,
        chat_id: int,
        title: str,
        description: str,
        price: float,
        category: str = "",
        shipping_type: str = "PICKUP",
        shipping_size: str = "PICKUP",
        image_analysis: str = "",
    ) -> int:
        """Create a new draft and return its ID."""
        async for conn in get_connection():
            cursor = await conn.execute(
                """
                INSERT INTO drafts (chat_id, title, description, price, category, shipping_type, shipping_size, image_analysis)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, title, description, price, category, shipping_type, shipping_size, image_analysis),
            )
            await conn.commit()
            draft_id = cursor.lastrowid
            logger.info(f"Created draft {draft_id} for chat {chat_id}")
            return draft_id  # type: ignore[return-value]
        raise RuntimeError("Failed to create draft")

    async def get(self, draft_id: int) -> Optional[aiosqlite.Row]:
        """Get a draft by ID."""
        async for conn in get_connection():
            cursor = await conn.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            )
            return await cursor.fetchone()
        return None

    async def get_by_chat(self, chat_id: int, status: str = "draft") -> list[aiosqlite.Row]:
        """Get all drafts for a chat with given status."""
        async for conn in get_connection():
            cursor = await conn.execute(
                "SELECT * FROM drafts WHERE chat_id = ? AND status = ? ORDER BY created_at DESC",
                (chat_id, status),
            )
            return await cursor.fetchall()
        return []

    async def update(self, draft_id: int, **kwargs: object) -> bool:
        """Update draft fields."""
        if not kwargs:
            return False

        set_clauses = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [draft_id]

        async for conn in get_connection():
            await conn.execute(
                f"UPDATE drafts SET {set_clauses}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                values,
            )
            await conn.commit()
            return True
        return False

    async def update_status(self, draft_id: int, status: str, listing_id: Optional[str] = None, listing_url: Optional[str] = None) -> bool:
        """Update draft status and optionally set listing ID/URL."""
        async for conn in get_connection():
            await conn.execute(
                """
                UPDATE drafts
                SET status = ?, listing_id = ?, listing_url = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, listing_id, listing_url, draft_id),
            )
            await conn.commit()
            return True
        return False

    async def add_image(self, draft_id: int, file_id: str, position: int = 0) -> None:
        """Add an image file_id to a draft."""
        async for conn in get_connection():
            await conn.execute(
                "INSERT INTO draft_images (draft_id, file_id, position) VALUES (?, ?, ?)",
                (draft_id, file_id, position),
            )
            await conn.commit()

    async def get_images(self, draft_id: int) -> list[aiosqlite.Row]:
        """Get all image file_ids for a draft."""
        async for conn in get_connection():
            cursor = await conn.execute(
                "SELECT * FROM draft_images WHERE draft_id = ? ORDER BY position",
                (draft_id,),
            )
            return await cursor.fetchall()
        return []

    async def delete(self, draft_id: int) -> bool:
        """Delete a draft by ID."""
        async for conn in get_connection():
            await conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
            await conn.commit()
            return True
        return False


class ListingRepository:
    """Repository for managing active listings."""

    async def upsert(
        self,
        listing_id: str,
        chat_id: int,
        title: str,
        description: str,
        price: float,
        category: str = "",
        days_remaining: Optional[int] = None,
        shipping_type: str = "SHIPPING",
        shipping_costs: Optional[float] = None,
        shipping_options: Optional[str] = None,
    ) -> None:
        """Insert or update a listing."""
        async for conn in get_connection():
            await conn.execute(
                """
                INSERT INTO listings (listing_id, chat_id, title, description, price, category, days_remaining, shipping_type, shipping_costs, shipping_options)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(listing_id) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    price = excluded.price,
                    category = excluded.category,
                    days_remaining = excluded.days_remaining,
                    shipping_type = excluded.shipping_type,
                    shipping_costs = excluded.shipping_costs,
                    shipping_options = excluded.shipping_options,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (listing_id, chat_id, title, description, price, category, days_remaining, shipping_type, shipping_costs, shipping_options),
            )
            await conn.commit()

    async def get(self, listing_id: str) -> Optional[aiosqlite.Row]:
        """Get a listing by its Kleinanzeigen ID."""
        async for conn in get_connection():
            cursor = await conn.execute(
                "SELECT * FROM listings WHERE listing_id = ?", (listing_id,)
            )
            return await cursor.fetchone()
        return None

    async def get_active(self, chat_id: Optional[int] = None) -> list[aiosqlite.Row]:
        """Get all active listings, optionally filtered by chat."""
        async for conn in get_connection():
            if chat_id:
                cursor = await conn.execute(
                    "SELECT * FROM listings WHERE status = 'active' AND chat_id = ? ORDER BY days_remaining",
                    (chat_id,),
                )
            else:
                cursor = await conn.execute(
                    "SELECT * FROM listings WHERE status = 'active' ORDER BY days_remaining"
                )
            return await cursor.fetchall()
        return []

    async def update_status(self, listing_id: str, status: str) -> bool:
        """Update listing status."""
        async for conn in get_connection():
            await conn.execute(
                "UPDATE listings SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE listing_id = ?",
                (status, listing_id),
            )
            await conn.commit()
            return True
        return False

    async def delete(self, listing_id: str) -> bool:
        """Mark a listing as deleted."""
        return await self.update_status(listing_id, "deleted")


class DiscogsMappingRepository:
    """Repository for Discogs release <-> Kleinanzeigen listing mappings."""

    async def upsert_mapping(
        self,
        *,
        chat_id: int,
        discogs_release_id: str,
        discogs_inventory_id: str,
        artist: str,
        title: str,
        label: str,
        year: Optional[int],
        media_condition: str,
        discogs_price: float,
        median_price: float,
        image_url: str,
        kleinanzeigen_listing_id: str,
        kleinanzeigen_listing_url: str,
        status: str = "active",
    ) -> None:
        """Insert or update a Discogs mapping row."""
        async for conn in get_connection():
            await conn.execute(
                """
                INSERT INTO discogs_mappings (
                    chat_id, discogs_release_id, discogs_inventory_id, artist, title, label, year,
                    media_condition, discogs_price, median_price, image_url,
                    kleinanzeigen_listing_id, kleinanzeigen_listing_url, status, last_seen_inventory_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id, discogs_release_id) DO UPDATE SET
                    discogs_inventory_id = excluded.discogs_inventory_id,
                    artist = excluded.artist,
                    title = excluded.title,
                    label = excluded.label,
                    year = excluded.year,
                    media_condition = excluded.media_condition,
                    discogs_price = excluded.discogs_price,
                    median_price = excluded.median_price,
                    image_url = excluded.image_url,
                    kleinanzeigen_listing_id = excluded.kleinanzeigen_listing_id,
                    kleinanzeigen_listing_url = excluded.kleinanzeigen_listing_url,
                    status = excluded.status,
                    last_seen_inventory_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    chat_id,
                    discogs_release_id,
                    discogs_inventory_id,
                    artist,
                    title,
                    label,
                    year,
                    media_condition,
                    discogs_price,
                    median_price,
                    image_url,
                    kleinanzeigen_listing_id,
                    kleinanzeigen_listing_url,
                    status,
                ),
            )
            await conn.commit()

    async def get_active_by_chat(self, chat_id: int) -> list[aiosqlite.Row]:
        """Get active mappings for a chat."""
        async for conn in get_connection():
            cursor = await conn.execute(
                """
                SELECT *
                FROM discogs_mappings
                WHERE chat_id = ? AND status = 'active'
                ORDER BY updated_at DESC
                """,
                (chat_id,),
            )
            return await cursor.fetchall()
        return []

    async def mark_deleted(self, chat_id: int, discogs_release_id: str) -> None:
        """Mark a mapping as deleted."""
        async for conn in get_connection():
            await conn.execute(
                """
                UPDATE discogs_mappings
                SET status = 'deleted', updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ? AND discogs_release_id = ?
                """,
                (chat_id, discogs_release_id),
            )
            await conn.commit()


draft_repo = DraftRepository()
listing_repo = ListingRepository()
discogs_mapping_repo = DiscogsMappingRepository()
