"""Handler for the daily listing check (manual trigger and display)."""
import asyncio
import html
import logging

try:
    from langsmith import traceable
except Exception:  # pragma: no cover
    def traceable(*args, **kwargs):  # type: ignore[no-redef]
        def _decorator(func):
            return func
        return _decorator

from telegram import Update
from telegram.ext import ContextTypes

from agents.listing_optimizer import get_listing_optimizer
from bot.keyboards import listing_optimize_keyboard, listing_delete_keyboard
from config.settings import settings
from db.repository import discogs_mapping_repo, listing_repo
from models.listing import Listing
from models.state import ListingOptimizerState
from services.kleinanzeigen import KleinanzeigenService
from services.telegram_service import safe_send_message

logger = logging.getLogger(__name__)
_running_checks: set[int] = set()
_running_checks_lock = asyncio.Lock()
_active_tasks: set[asyncio.Task] = set()  # tracked for clean shutdown


def cancel_all_tasks() -> None:
    """Cancel all tracked background tasks (called on shutdown)."""
    for task in list(_active_tasks):
        task.cancel()


async def _is_check_running(chat_id: int) -> bool:
    async with _running_checks_lock:
        return chat_id in _running_checks


async def _try_start_check(chat_id: int) -> bool:
    async with _running_checks_lock:
        if chat_id in _running_checks:
            return False
        _running_checks.add(chat_id)
        return True


async def _finish_check(chat_id: int) -> None:
    async with _running_checks_lock:
        _running_checks.discard(chat_id)


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


def _truncate(text: str, limit: int = 600) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _build_renewal_change_message(
    listing: Listing,
    new_title: str,
    new_description: str,
    new_price: float,
) -> str:
    """Build detailed renewal summary including before/after values."""
    old_title = listing.title
    old_description = listing.description
    old_price = listing.price

    return (
        "✅ Proaktive Erneuerung abgeschlossen\n\n"
        f"Anzeige-ID: {listing.listing_id}\n"
        f"URL: {listing.url or '-'}\n\n"
        "Änderungen:\n"
        f"Titel alt: {old_title}\n"
        f"Titel neu: {new_title}\n"
        f"Preis alt: {old_price:.2f} EUR\n"
        f"Preis neu: {new_price:.2f} EUR\n\n"
        "Beschreibung alt:\n"
        f"{_truncate(old_description)}\n\n"
        "Beschreibung neu:\n"
        f"{_truncate(new_description)}"
    )


def _select_oldest_listing_for_renewal(listings: list[Listing]) -> Listing:
    """Select the oldest suitable listing for proactive renewal.

    Preference:
    1) listings with price >= 5 EUR (avoid deletion-only path)
    2) PICKUP listings over SHIPPING (cleared faster)
    3) oldest by created_at when available
    4) lowest days_remaining as fallback
    5) first listing as last resort
    """
    candidates = [l for l in listings if l.price >= 5.0]
    if not candidates:
        candidates = list(listings)

    pickup_candidates = [l for l in candidates if l.shipping_type == "PICKUP"]
    if pickup_candidates:
        candidates = pickup_candidates

    with_created_at = [l for l in candidates if l.created_at is not None]
    if with_created_at:
        return min(with_created_at, key=lambda l: l.created_at)  # type: ignore[arg-type]

    with_days_remaining = [l for l in candidates if l.days_remaining is not None]
    if with_days_remaining:
        return min(with_days_remaining, key=lambda l: l.days_remaining)  # type: ignore[arg-type]

    return candidates[0]


async def daily_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /check command – manually trigger the daily listing check."""
    if update.message is None:
        return

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    logger.info(f"Manual daily check triggered by user {chat_id}")

    if await _is_check_running(chat_id):
        await update.message.reply_text("⏳ Für diesen Chat läuft bereits ein Anzeigen-Check.")
        return

    await update.message.reply_text("🔄 *Täglicher Anzeigen-Check wird gestartet...*", parse_mode="Markdown")
    await run_daily_check(context, chat_id, manual=True)


def _count_new_yamls(service: "KleinanzeigenService", since: float) -> int:
    """Count YAML files written to downloaded-ads dir after `since` (unix timestamp)."""
    import glob as _glob
    import os
    pattern = str(service.downloaded_ads_dir / "**" / "*.yaml")
    return sum(1 for f in _glob.glob(pattern, recursive=True) if os.path.getmtime(f) >= since)


async def _get_listings_with_progress(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    service: "KleinanzeigenService",
) -> list[Listing]:
    """Fetch published listing summary (manual mode)."""
    await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Lade Anzeigen-Übersicht...",
    )
    return await service.get_published_listings_summary()


@traceable(name="telegram_check", run_type="chain")
async def run_daily_check(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, manual: bool = False
) -> None:
    """Run the daily listing check and notify the user via Telegram.

    Args:
        context: Telegram bot context
        chat_id: Target chat ID for notifications
        manual: If True, send progress updates during processing
    """
    logger.info(f"Running daily check for chat {chat_id} (manual={manual})")

    started = await _try_start_check(chat_id)
    if not started:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Für diesen Chat läuft bereits ein Anzeigen-Check.",
        )
        return

    try:
        # Fetch current listings summary from Kleinanzeigen
        service = KleinanzeigenService()
        service.purge_downloaded_ads()

        if manual:
            listings = await _get_listings_with_progress(context, chat_id, service)
        else:
            listings = await service.get_published_listings_summary()

        if not listings:
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔄 *Täglicher Anzeigen-Check*\n\nKeine aktiven Anzeigen gefunden.",
                parse_mode="Markdown",
            )
            _run_discogs_followup(context, chat_id)
            return

        # Exclude reserved listings ("Reserviert" = state paused) from renewal
        reserved_count = sum(1 for l in listings if l.status == "reserved")

        # Exclude Discogs-synced listings from renewal — Discogs sync owns their lifecycle
        discogs_mappings = await discogs_mapping_repo.get_active_by_chat(chat_id)
        discogs_listing_ids = {
            str(row["kleinanzeigen_listing_id"])
            for row in discogs_mappings
            if row["kleinanzeigen_listing_id"]
        }

        renewable = [
            l for l in listings
            if l.status != "reserved" and l.listing_id not in discogs_listing_ids
        ]
        discogs_count = sum(1 for l in listings if l.listing_id in discogs_listing_ids)

        def _excluded_suffix() -> str:
            parts = []
            if reserved_count:
                parts.append(f"{reserved_count} reserviert")
            if discogs_count:
                parts.append(f"{discogs_count} Discogs")
            return f", davon {' / '.join(parts)}" if parts else ""

        # Filter listings expiring within type-specific threshold
        expiring = [l for l in renewable if l.is_expiring_soon]
        days_by_id = {l.listing_id: l.days_remaining for l in listings}

        if not expiring:
            if not renewable:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔄 *Täglicher Anzeigen-Check*\n\n"
                        f"📊 {len(listings)} aktive Anzeige(n) gefunden"
                        + _excluded_suffix()
                        + ".\nKeine erneuerbare Anzeige übrig."
                    ),
                    parse_mode="Markdown",
                )
                _run_discogs_followup(context, chat_id)
                return
            proactive = _select_oldest_listing_for_renewal(renewable)
            await safe_send_message(
                context.bot,
                chat_id,
                (
                    f"🔄 <b>Täglicher Anzeigen-Check</b>\n\n"
                    f"📊 {len(listings)} aktive Anzeige(n) gefunden"
                    + _excluded_suffix()
                    + ".\nKeine Anzeigen laufen bald ab.\n"
                    f"↻ Älteste Anzeige wird proaktiv vorgeschlagen: <b>{html.escape(proactive.title)}</b>\n"
                    f"✍️ Optimierungsvorschlag folgt zur Bestätigung."
                ),
                parse_mode="HTML",
            )
            detailed = await service.download_single_listing(proactive.listing_id)
            if detailed:
                detailed.days_remaining = days_by_id.get(proactive.listing_id)
                await listing_repo.upsert(
                    listing_id=detailed.listing_id,
                    chat_id=chat_id,
                    title=detailed.title,
                    description=detailed.description,
                    price=detailed.price,
                    category=detailed.category,
                    days_remaining=detailed.days_remaining,
                    shipping_type=detailed.shipping_type,
                    shipping_costs=detailed.shipping_costs,
                    shipping_options=detailed.shipping_options,
                )
            await _process_expiring_listing(context, chat_id, detailed or proactive)
            _run_discogs_followup(context, chat_id)
            return

        header = (
            f"🔄 *Täglicher Anzeigen-Check*\n\n"
            f"📊 {len(listings)} aktive Anzeige(n)"
            + _excluded_suffix()
            + f"\nDavon {len(expiring)} laufen bald ab:\n\n"
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=header,
            parse_mode="Markdown",
        )

        # Update listing cache in DB
        for listing in listings:
            await listing_repo.upsert(
                listing_id=listing.listing_id,
                chat_id=chat_id,
                title=listing.title,
                description=listing.description,
                price=listing.price,
                category=listing.category,
                days_remaining=listing.days_remaining,
                shipping_type=listing.shipping_type,
                shipping_costs=listing.shipping_costs,
                shipping_options=listing.shipping_options,
            )

        # Process with pagination (5 per page)
        detailed_expiring: list[Listing] = []
        for listing in expiring:
            detailed = await service.download_single_listing(listing.listing_id)
            if detailed:
                detailed.days_remaining = days_by_id.get(listing.listing_id)
                await listing_repo.upsert(
                    listing_id=detailed.listing_id,
                    chat_id=chat_id,
                    title=detailed.title,
                    description=detailed.description,
                    price=detailed.price,
                    category=detailed.category,
                    days_remaining=detailed.days_remaining,
                    shipping_type=detailed.shipping_type,
                    shipping_costs=detailed.shipping_costs,
                    shipping_options=detailed.shipping_options,
                )
            detailed_expiring.append(detailed or listing)
        await _process_expiring_page(context, chat_id, detailed_expiring, manual=manual, total=len(detailed_expiring))

    except Exception as e:
        logger.error(f"Daily check failed for chat {chat_id}: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Fehler beim Anzeigen-Check: {str(e)}",
        )
    finally:
        await _finish_check(chat_id)


PAGE_SIZE = 5


PROGRESS_INTERVAL = 10  # send progress every N listings (manual mode only)


async def _process_expiring_page(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    listings: list[Listing],
    manual: bool = False,
    total: int = 0,
    processed_so_far: int = 0,
) -> None:
    """Process the first PAGE_SIZE listings; queue the rest for /weiter."""
    page, remaining = listings[:PAGE_SIZE], listings[PAGE_SIZE:]

    for i, listing in enumerate(page):
        await _process_expiring_listing(context, chat_id, listing)
        done = processed_so_far + i + 1
        if manual and done % PROGRESS_INTERVAL == 0 and (remaining or i < len(page) - 1):
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏳ Fortschritt: {done}/{total} Anzeigen verarbeitet...",
            )

    if remaining:
        if "expiring_queue" not in context.bot_data:
            context.bot_data["expiring_queue"] = {}
        context.bot_data["expiring_queue"][chat_id] = {
            "listings": remaining,
            "manual": manual,
            "total": total,
            "processed_so_far": processed_so_far + len(page),
        }
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"📄 *{len(remaining)} weitere Anzeige(n)* noch nicht verarbeitet.\n"
                f"Tippe /weiter für die nächsten {min(PAGE_SIZE, len(remaining))}."
            ),
            parse_mode="Markdown",
        )
    else:
        context.bot_data.setdefault("expiring_queue", {}).pop(chat_id, None)
        _run_discogs_followup(context, chat_id)


async def weiter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /weiter – process next page of expiring listings."""
    if update.message is None:
        return
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    queue: dict[int, dict] = context.bot_data.get("expiring_queue", {})
    entry = queue.get(chat_id)

    if not entry:
        await update.message.reply_text("✅ Keine weiteren Anzeigen in der Warteschlange.")
        return

    await _process_expiring_page(
        context,
        chat_id,
        entry["listings"],
        manual=entry.get("manual", False),
        total=entry.get("total", 0),
        processed_so_far=entry.get("processed_so_far", 0),
    )


def _run_discogs_followup(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    """Schedule Discogs sync as a background task so it doesn't block Telegram callbacks."""
    async def _run() -> None:
        try:
            from bot.handlers.discogs_sync import run_discogs_sync
            await run_discogs_sync(
                context,
                chat_id,
                announce_start=False,
                verbose_output=False,
                notify_on_error=False,
            )
        except Exception as exc:
            logger.error("Discogs follow-up failed for chat %s: %s", chat_id, exc, exc_info=True)

    # Keep a strong reference so the task isn't garbage-collected mid-run, and
    # register it so cancel_all_tasks() can cancel it on shutdown.
    task = asyncio.create_task(_run())
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)


@traceable(name="telegram_check_process_listing", run_type="chain")
async def _process_expiring_listing(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    listing: Listing,
) -> None:
    """Process a single expiring listing – suggest deletion or optimization."""
    try:
        initial_state: ListingOptimizerState = {
            "listing_id": listing.listing_id,
            "current_title": listing.title,
            "current_description": listing.description,
            "current_price": listing.price,
            "days_remaining": listing.days_remaining or 0,
            "suggested_title": "",
            "suggested_description": "",
            "suggested_price": 0.0,
            "changes_summary": "",
            "action": "optimize",
            "feedback": "",
            "status": "pending",
        }

        result = await get_listing_optimizer().ainvoke(
            initial_state,
            config={
                "run_name": "telegram_check_optimizer",
                "tags": ["telegram", "check", "daily_check"],
                "metadata": {
                    "chat_id": chat_id,
                    "listing_id": listing.listing_id,
                },
            },
        )

        if result.get("action") == "delete":
            # Suggest deletion for cheap listings
            msg = (
                f"📦 <b>{html.escape(listing.title)}</b>\n"
                f"Noch {listing.days_remaining} Tage | Preis: {listing.price:.2f} EUR\n\n"
                f"⚠️ {html.escape(str(result.get('changes_summary', 'Löschung empfohlen')))}"
            )
            await safe_send_message(
                context.bot,
                chat_id,
                msg,
                parse_mode="HTML",
                reply_markup=listing_delete_keyboard(listing.listing_id),
            )

        else:
            # Show optimization suggestion
            # Store optimization data for later use
            if "optimizations" not in context.bot_data:
                context.bot_data["optimizations"] = {}
            context.bot_data["optimizations"][listing.listing_id] = {
                "suggested_title": result.get("suggested_title", listing.title),
                "suggested_description": result.get("suggested_description", listing.description),
                "suggested_price": result.get("suggested_price", listing.price),
            }

            _title = html.escape(listing.title)
            _suggested = html.escape(str(result.get("suggested_title", listing.title)))
            _summary = html.escape(str(result.get("changes_summary", "")))
            msg = (
                f"📦 <b>{_title}</b>\n"
                f"Noch {listing.days_remaining} Tage\n\n"
                f"<b>Vorgeschlagene Änderungen:</b>\n"
                f"• Titel: {_title} → {_suggested}\n"
                f"• Preis: {listing.price:.2f} EUR → {result.get('suggested_price', listing.price):.2f} EUR\n\n"
                f"{_summary}"
            )
            await safe_send_message(
                context.bot,
                chat_id,
                msg,
                parse_mode="HTML",
                reply_markup=listing_optimize_keyboard(listing.listing_id),
            )

    except Exception as e:
        logger.error(f"Failed to process listing {listing.listing_id}: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Fehler bei Anzeige '{listing.title}':\n{_describe_error(e)}",
        )


async def renew_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /renew <ad_id> – fetch listing, run optimizer, show proposal."""
    if update.message is None:
        return

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    args = context.args or []

    if not args:
        await update.message.reply_text("Verwendung: /renew <Anzeigen-ID>")
        return

    listing_id = args[0].strip()
    await update.message.reply_text(f"🔍 Lade Anzeige {listing_id}...")

    try:
        service = KleinanzeigenService()

        # Purge stale cache to ensure fresh data
        service.purge_listing_cache(listing_id)

        # Always download fresh from kleinanzeigen.de
        listing = await service.download_single_listing(listing_id)
        if listing is not None:
            listing = listing.model_copy(update={"chat_id": chat_id})

        if listing is None:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Anzeige {listing_id} nicht gefunden (auch auf kleinanzeigen.de). Möglicherweise nicht mehr aktiv.",
            )
            return

        await update.message.reply_text(
            f"📦 <b>{html.escape(listing.title)}</b>\n"
            f"Preis: {listing.price:.2f} EUR\n\n"
            "⏳ Optimierungsvorschlag wird erstellt...",
            parse_mode="HTML",
        )

        # Upsert listing into DB so feedback handler can look it up
        await listing_repo.upsert(
            listing_id=listing.listing_id,
            chat_id=chat_id,
            title=listing.title,
            description=listing.description,
            price=listing.price,
            category=listing.category,
            days_remaining=listing.days_remaining,
        )

        await _process_expiring_listing(context, chat_id, listing)

    except Exception as e:
        logger.error(f"/renew failed for {listing_id}: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Fehler: {_describe_error(e)}")


async def scheduled_daily_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback for the APScheduler job – runs the daily check for all configured users."""
    logger.info("Scheduled daily check triggered")
    allowed_user_ids = settings.telegram_allowed_user_ids

    for user_id in allowed_user_ids:
        try:
            await run_daily_check(context, user_id)
        except Exception as e:
            logger.error(f"Scheduled check failed for user {user_id}: {e}", exc_info=True)
