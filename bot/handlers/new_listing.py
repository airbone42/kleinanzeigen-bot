"""Handler for new listing creation via photo uploads."""
import asyncio
import base64
import logging
import time
from collections import defaultdict
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from agents.listing_creator import get_listing_creator
from db.repository import draft_repo
from models.state import ListingCreatorState

logger = logging.getLogger(__name__)

# Track media groups: {media_group_id: [file_ids]}
_media_groups: dict[str, list[str]] = defaultdict(list)
# Track which media groups have been processed
_processed_groups: set[str] = set()
# Track pending jobs: {media_group_id: job}
_pending_jobs: dict[str, Any] = {}

MEDIA_GROUP_WAIT_SECONDS = 3.0
RECENT_UPLOAD_DEDUP_SECONDS = 30 * 60  # 30 minutes
# Track recently processed uploads to prevent duplicate publishes
_recent_upload_fingerprints: dict[str, float] = {}


def _truncate(text: str, limit: int = 900) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _build_publish_message(
    result: dict[str, Any],
    listing_url: str,
    active_count: int,
    active_limit: int,
    draft_id: int,
    image_count: int,
) -> str:
    """Build Telegram summary with full listing data after publish."""
    title = str(result.get("title", "")).strip()
    description = _truncate(str(result.get("description", "")), 1500)
    price = float(result.get("price", 0.0))
    category = str(result.get("category", "")).strip() or "-"
    shipping_type = str(result.get("shipping_type", "PICKUP")).strip()
    shipping_size = str(result.get("shipping_size", "PICKUP")).strip()
    return (
        "✅ Inserat veröffentlicht\n\n"
        f"Titel: {title}\n"
        f"Preis: {price:.2f} EUR\n"
        f"Kategorie: {category}\n"
        f"Versand: {shipping_type} ({shipping_size})\n"
        f"Bilder: {image_count}\n"
        f"Draft-ID: {draft_id}\n"
        f"Aktive Anzeigen: {active_count}/{active_limit}\n"
        f"URL: {listing_url}\n\n"
        "Beschreibung:\n"
        f"{description}"
    )


def _describe_error(e: Exception) -> str:
    """Map common exceptions to user-friendly German hints."""
    msg = str(e).lower()
    if "401" in msg or "unauthorized" in msg or "api key" in msg:
        return "API-Schlüssel ungültig. Bitte OPENROUTER_API_KEY prüfen."
    if "429" in msg or "rate limit" in msg or "too many" in msg:
        return "Rate-Limit erreicht. Kurz warten und erneut versuchen."
    if "timeout" in msg or "timed out" in msg:
        return "Zeitüberschreitung beim KI-Aufruf. Bitte erneut versuchen."
    if "connection" in msg or "network" in msg or "refused" in msg:
        return "Netzwerkfehler. Internetverbindung prüfen."
    if "404" in msg:
        return "Dienst nicht gefunden (404). Konfiguration prüfen."
    return f"Fehler: {e}"


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming photo messages.

    Collects photos from media groups and triggers listing creation.
    """
    if update.message is None or update.message.photo is None:
        return

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    message = update.message

    # Use highest resolution photo
    photo = message.photo[-1]
    file_id = photo.file_id

    media_group_id = message.media_group_id

    if media_group_id:
        # Part of a media group – collect all photos
        _media_groups[media_group_id].append(file_id)

        # Cancel existing delayed job for this group
        if media_group_id in _pending_jobs:
            _pending_jobs[media_group_id].schedule_removal()

        # Schedule processing after waiting for all photos
        job = context.job_queue.run_once(  # type: ignore[union-attr]
            _process_media_group,
            MEDIA_GROUP_WAIT_SECONDS,
            data={"media_group_id": media_group_id, "chat_id": chat_id},
            name=f"media_group_{media_group_id}",
        )
        _pending_jobs[media_group_id] = job

        # Caption is only available on the first photo of the group
        if len(_media_groups[media_group_id]) == 1:
            _media_groups[media_group_id + "__caption"] = [message.caption or ""]
            await message.reply_text("📸 Fotos werden gesammelt...")

    else:
        # Single photo – process immediately
        caption = message.caption or ""
        await message.reply_text("📸 Foto erhalten! Analysiere...")
        await _create_listing(context, chat_id, [file_id], caption=caption)


async def _process_media_group(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback to process a complete media group."""
    if context.job is None or context.job.data is None:
        return

    data = context.job.data
    media_group_id = data["media_group_id"]
    chat_id = data["chat_id"]

    if media_group_id in _processed_groups:
        return

    _processed_groups.add(media_group_id)
    file_ids = _media_groups.pop(media_group_id, [])
    caption_list = _media_groups.pop(media_group_id + "__caption", [""])
    caption = caption_list[0] if caption_list else ""
    _pending_jobs.pop(media_group_id, None)

    if not file_ids:
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📸 {len(file_ids)} Foto(s) erhalten! Analysiere...",
    )
    await _create_listing(context, chat_id, file_ids, caption=caption)


async def _create_listing(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, file_ids: list[str], caption: str = ""
) -> None:
    """Download images, run the listing creator workflow, and publish directly."""
    # Deduplicate repeated trigger events for the same photo batch/caption.
    now = time.time()
    for key, ts in list(_recent_upload_fingerprints.items()):
        if now - ts > RECENT_UPLOAD_DEDUP_SECONDS:
            _recent_upload_fingerprints.pop(key, None)
    fingerprint = f"{chat_id}|{'|'.join(sorted(file_ids))}|{caption.strip().lower()}"
    if fingerprint in _recent_upload_fingerprints:
        logger.warning(
            "Skipping duplicate listing creation for chat %s (same image batch).",
            chat_id,
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text="ℹ️ Dieser Bild-Upload wurde bereits verarbeitet. Keine zweite Veröffentlichung.",
        )
        return
    # Reserve fingerprint before long-running operations so concurrent duplicates are blocked.
    _recent_upload_fingerprints[fingerprint] = now

    # Download images from Telegram and encode as base64 strings
    images: list[str] = []
    for file_id in file_ids[:10]:  # Max 10 images
        try:
            file = await context.bot.get_file(file_id)
            image_bytes = await file.download_as_bytearray()
            images.append(base64.b64encode(bytes(image_bytes)).decode("utf-8"))
            logger.debug(f"Downloaded and encoded image {file_id}: {len(image_bytes)} bytes")
        except Exception as e:
            logger.error(f"Failed to download image {file_id}: {e}")

    if not images:
        _recent_upload_fingerprints.pop(fingerprint, None)
        await context.bot.send_message(
            chat_id=chat_id,
            text="\u274c Bilder konnten nicht geladen werden. Bitte versuche es erneut.",
        )
        return

    # Show progress
    progress_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="\U0001f50d Analysiere Bilder...",
    )

    published_listing_url: str | None = None
    try:
        initial_state: ListingCreatorState = {
            "images": images,
            "image_file_ids": file_ids,
            "image_analysis": "",
            "title": "",
            "description": "",
            "price": 0.0,
            "category": "",
            "caption": caption,
            "shipping_type": "PICKUP",
            "shipping_size": "PICKUP",
            "feedback": "",
            "draft_id": None,
            "status": "draft",
        }

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text="\u270d\ufe0f Erstelle Entwurf...",
        )

        result = await get_listing_creator().ainvoke(initial_state)

        # Save draft to database
        draft_id = await draft_repo.create(
            chat_id=chat_id,
            title=result["title"],
            description=result["description"],
            price=result["price"],
            category=result.get("category", ""),
            shipping_type=result.get("shipping_type", "PICKUP"),
            shipping_size=result.get("shipping_size", "PICKUP"),
            image_analysis=result.get("image_analysis", ""),
        )

        # Save image file IDs
        for i, file_id in enumerate(file_ids):
            await draft_repo.add_image(draft_id, file_id, position=i)

        # Download images as bytes for Kleinanzeigen upload
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text="\U0001f4e4 Wird auf Kleinanzeigen hochgeladen...",
        )

        image_bytes_list: list[bytes] = []
        for fid in file_ids:
            try:
                f = await context.bot.get_file(fid)
                image_bytes_list.append(bytes(await f.download_as_bytearray()))
            except Exception as img_err:
                logger.error(f"Failed to download image {fid}: {img_err}")

        # Publish directly
        from models.draft import Draft as DraftModel
        from services.kleinanzeigen import KleinanzeigenService
        draft_model = DraftModel(
            id=draft_id,
            chat_id=chat_id,
            title=result["title"],
            description=result["description"],
            price=result["price"],
            category=result.get("category", ""),
            shipping_type=result.get("shipping_type", "PICKUP"),
            shipping_size=result.get("shipping_size", "PICKUP"),
            image_file_ids=file_ids,
        )
        service = KleinanzeigenService()
        listing_url = await service.create_draft(draft_model, image_bytes_list)
        published_listing_url = listing_url
        await draft_repo.update_status(draft_id, "published", listing_url=listing_url)
        active_count, active_limit = service.get_active_slots_usage(listing_url)

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text=_build_publish_message(
                result=result,
                listing_url=listing_url,
                active_count=active_count,
                active_limit=active_limit,
                draft_id=draft_id,
                image_count=len(file_ids),
            ),
        )
        logger.info(f"Draft {draft_id} published to Kleinanzeigen, url={listing_url}")

    except Exception as e:
        # Keep dedup lock if publish likely already succeeded to avoid duplicate listings.
        if published_listing_url is None:
            _recent_upload_fingerprints.pop(fingerprint, None)
        logger.error(f"Listing creation failed for chat {chat_id}: {e}", exc_info=True)
        if published_listing_url is not None:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=(
                    "⚠️ Anzeige wurde bereits veröffentlicht, aber ein Folgefehler ist aufgetreten.\n"
                    f"URL: {published_listing_url}\n"
                    "Der Upload bleibt gesperrt, um Doppelanzeigen zu verhindern.\n"
                    f"{_describe_error(e)}"
                ),
            )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=f"\u274c Fehler bei der Erstellung des Inserats.\n{_describe_error(e)}",
            )
