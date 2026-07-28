"""Handler for manual and scheduled Discogs inventory sync."""
import asyncio
import logging
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from agents.discogs_sync import get_discogs_sync_graph
from models.state import DiscogsSyncState
from services.telegram_service import truncate_for_telegram

logger = logging.getLogger(__name__)

_running_discogs_sync: set[int] = set()
_running_discogs_sync_lock = asyncio.Lock()


async def _try_start_discogs_sync(chat_id: int) -> bool:
    async with _running_discogs_sync_lock:
        if chat_id in _running_discogs_sync:
            return False
        _running_discogs_sync.add(chat_id)
        return True


async def _finish_discogs_sync(chat_id: int) -> None:
    async with _running_discogs_sync_lock:
        _running_discogs_sync.discard(chat_id)


async def run_discogs_sync(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    announce_start: bool = True,
    verbose_output: bool = True,
    notify_on_error: bool = True,
) -> None:
    """Run Discogs sync graph and send Telegram output based on trigger mode."""
    started = await _try_start_discogs_sync(chat_id)
    if not started:
        if announce_start:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⏳ Discogs-Sync läuft bereits für diesen Chat.",
            )
        return

    try:
        if announce_start:
            await context.bot.send_message(
                chat_id=chat_id,
                text="🎵 Starte Discogs-Sync...",
            )

        initial_state: DiscogsSyncState = {
            "chat_id": chat_id,
            "inventory_items": [],
            "treasure_items": [],
            "published": [],
            "deleted": [],
            "updated_mappings": 0,
            "summary": "",
            "errors": [],
        }
        result = await get_discogs_sync_graph().ainvoke(initial_state)
        if verbose_output:
            text = _build_verbose_sync_message(result)
        else:
            text = _build_changes_only_message(result)
        if text:
            await context.bot.send_message(
                chat_id=chat_id,
                text=truncate_for_telegram(text),
            )
    except Exception as exc:
        logger.error("Discogs-Sync failed for chat %s: %s", chat_id, exc, exc_info=True)
        if notify_on_error:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Discogs-Sync fehlgeschlagen: {exc}",
            )
    finally:
        await _finish_discogs_sync(chat_id)


async def discogs_sync_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /discogssync command."""
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    await run_discogs_sync(
        context,
        chat_id,
        announce_start=True,
        verbose_output=True,
        notify_on_error=True,
    )


def _build_changes_only_message(result: dict[str, Any]) -> str:
    """Automatic mode: only notify on newly published/deleted listings."""
    published = result.get("published", [])
    deleted = result.get("deleted", [])
    if not isinstance(published, list):
        published = []
    if not isinstance(deleted, list):
        deleted = []
    if not published and not deleted:
        return ""

    lines = ["🎵 Discogs-Sync Änderungen"]
    if published:
        lines.append("\nNeu veroeffentlicht:")
        for row in published:
            lines.append(
                f"- {row.get('artist', '')} - {row.get('title', '')} | "
                f"{row.get('price', '')} EUR | {row.get('listing_url', '')}"
            )
    if deleted:
        lines.append("\nGeloescht:")
        for row in deleted:
            lines.append(
                f"- Release {row.get('release_id', '')} | Anzeige {row.get('listing_id', '-')}"
            )
    return "\n".join(lines)


def _build_verbose_sync_message(result: dict[str, Any]) -> str:
    """Manual mode: send full sync output including candidate treasures."""
    inventory_items = result.get("inventory_items", [])
    treasure_items = result.get("treasure_items", [])
    published = result.get("published", [])
    deleted = result.get("deleted", [])
    updated_mappings = int(result.get("updated_mappings", 0))
    errors = result.get("errors", [])

    if not isinstance(inventory_items, list):
        inventory_items = []
    if not isinstance(treasure_items, list):
        treasure_items = []
    if not isinstance(published, list):
        published = []
    if not isinstance(deleted, list):
        deleted = []
    if not isinstance(errors, list):
        errors = []

    lines = [
        "🎵 Discogs-Sync Vollausgabe",
        "",
        f"Inventory-Eintraege: {len(inventory_items)}",
        f"Treffer (Mainstream + Median > 50 EUR): {len(treasure_items)}",
        f"Referenzen aktualisiert: {updated_mappings}",
        f"Neu veroeffentlicht: {len(published)}",
        f"Geloescht (Discogs entfernt): {len(deleted)}",
    ]

    if treasure_items:
        lines.append("\nTreffer:")
        for row in treasure_items:
            lines.append(
                f"- {row.get('artist', '')} - {row.get('title', '')} | "
                f"Label: {row.get('label', '-') or '-'} | "
                f"Jahr: {row.get('year', '-') or '-'} | "
                f"Preis: {float(row.get('price_eur', 0.0)):.2f} | "
                f"Median: {float(row.get('median_price_eur', 0.0)):.2f}"
            )

    if published:
        lines.append("\nNeu veroeffentlicht:")
        for row in published:
            lines.append(
                f"- {row.get('artist', '')} - {row.get('title', '')} | "
                f"{row.get('price', '')} EUR | {row.get('listing_url', '')}"
            )

    if deleted:
        lines.append("\nGeloescht:")
        for row in deleted:
            lines.append(
                f"- Release {row.get('release_id', '')} | Anzeige {row.get('listing_id', '-')}"
            )

    if errors:
        lines.append("\nFehler:")
        for err in errors[:20]:
            lines.append(f"- {err}")

    return "\n".join(lines)
