"""Handler for optimization-related inline keyboard callbacks."""
import logging
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from config.settings import settings
from services.kleinanzeigen import KleinanzeigenService

logger = logging.getLogger(__name__)


# User states for multi-step interactions
# {chat_id: {"action": str, "listing_id": str}}
_user_states: dict[int, dict[str, object]] = {}


async def _safe_edit_or_send(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: Optional[int],
    text: str,
) -> None:
    """Edit the callback message if possible, otherwise send a new message."""
    from telegram import CallbackQuery
    q: CallbackQuery = query  # type: ignore[assignment]

    try:
        await q.edit_message_text(text)
        return
    except Exception as e:
        logger.info(f"edit_message_text failed, sending fallback message: {e}")

    if chat_id is None:
        return
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Fallback send_message failed: {e}", exc_info=True)


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


async def callback_query_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle all inline keyboard button presses."""
    query = update.callback_query
    if query is None:
        return

    # CallbackQueryHandler cannot take a user filter, so gate here to match the
    # per-user restriction applied to every command/message handler.
    user = update.effective_user
    allowed = settings.telegram_allowed_user_ids
    if allowed and (user is None or user.id not in allowed):
        logger.warning("Rejected callback from unauthorized user %s", user.id if user else None)
        try:
            await query.answer("Nicht autorisiert.", show_alert=True)
        except Exception:
            pass
        return

    try:
        await query.answer()
    except Exception:
        pass  # Query may have expired (>10 min); continue processing anyway

    data = query.data or ""
    chat_id = update.effective_chat.id  # type: ignore[union-attr]

    logger.info(f"Callback query from {chat_id}: {data}")

    try:
        if data.startswith("opt_accept:"):
            await _handle_opt_accept(query, context, data.split(":")[1])

        elif data.startswith("opt_edit:"):
            await _handle_opt_edit(query, data.split(":")[1])

        elif data.startswith("opt_skip:"):
            await _handle_opt_skip(query, data.split(":")[1])

        elif data.startswith("del_confirm:"):
            await _handle_del_confirm(query, context, data.split(":")[1])

        elif data.startswith("del_skip:"):
            await _handle_del_skip(query, data.split(":")[1])

        else:
            logger.warning(f"Unknown callback data: {data}")

    except Exception as e:
        logger.error(f"Callback handler error for '{data}': {e}", exc_info=True)
        await query.edit_message_text("❌ Fehler bei der Verarbeitung. Bitte versuche es erneut.")


async def _handle_opt_accept(
    query: object, context: ContextTypes.DEFAULT_TYPE, listing_id: str
) -> None:
    """Accept optimization suggestion: renew listing with optimized text."""
    from telegram import CallbackQuery
    q: CallbackQuery = query  # type: ignore[assignment]

    chat_id = q.message.chat_id if q.message else None  # type: ignore[union-attr]
    await _safe_edit_or_send(q, context, chat_id, "⏳ Optimierung wird angewendet...")

    try:
        opt_data = context.bot_data.get("optimizations", {}).get(listing_id, {})

        service = KleinanzeigenService()
        new_url, pickup_warning = await service.republish_with_optimization(
            listing_id,
            title=opt_data.get("suggested_title") or None,
            description=opt_data.get("suggested_description") or None,
            price=opt_data.get("suggested_price") or None,
            chat_id=chat_id,
        )

        from db.repository import listing_repo
        await listing_repo.update_status(listing_id, "replaced")

        url_text = f"\n{new_url}" if new_url else "\nPrüfe deine Anzeigen auf Kleinanzeigen.de"
        if chat_id is not None:
            await context.bot.send_message(chat_id=chat_id, text=f"✅ Neue Anzeige veröffentlicht!{url_text}")
            if pickup_warning:
                await context.bot.send_message(chat_id=chat_id, text=pickup_warning)

    except Exception as e:
        logger.error(f"Optimization accept failed for {listing_id}: {e}", exc_info=True)
        if chat_id is not None:
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ Fehler: {str(e)}")
            except Exception:
                await _safe_edit_or_send(q, context, chat_id, f"❌ Fehler: {str(e)}")


async def _handle_opt_edit(query: object, listing_id: str) -> None:
    """Request manual editing of optimization."""
    from telegram import CallbackQuery
    q: CallbackQuery = query  # type: ignore[assignment]

    chat_id = q.message.chat_id  # type: ignore[union-attr]
    _user_states[chat_id] = {"action": "opt_feedback", "listing_id": listing_id}

    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text(  # type: ignore[union-attr]
        "✏️ Was soll ich noch anpassen? Schreibe dein Feedback:"
    )


async def _handle_opt_skip(query: object, listing_id: str) -> None:
    """Skip optimization for this listing."""
    from telegram import CallbackQuery
    q: CallbackQuery = query  # type: ignore[assignment]

    await q.edit_message_text(f"⏭️ Anzeige {listing_id} übersprungen.")
    logger.info(f"Optimization skipped for listing {listing_id}")


async def _handle_del_confirm(
    query: object, context: ContextTypes.DEFAULT_TYPE, listing_id: str
) -> None:
    """Confirm and execute listing deletion."""
    from telegram import CallbackQuery
    q: CallbackQuery = query  # type: ignore[assignment]

    await q.edit_message_text("⏳ Lösche Anzeige...")

    try:
        service = KleinanzeigenService()
        success = await service.delete_listing(listing_id)

        if success:
            from db.repository import listing_repo
            await listing_repo.update_status(listing_id, "deleted")
            await q.edit_message_text("🗑️ Anzeige wurde gelöscht.")
            logger.info(f"Listing {listing_id} deleted")
        else:
            await q.edit_message_text("❌ Löschen fehlgeschlagen. Bitte prüfe Kleinanzeigen manuell.")

    except Exception as e:
        logger.error(f"Deletion failed for {listing_id}: {e}", exc_info=True)
        await q.edit_message_text(f"❌ Fehler beim Löschen: {str(e)}")


async def _handle_del_skip(query: object, listing_id: str) -> None:
    """Keep the listing, skip deletion."""
    from telegram import CallbackQuery
    q: CallbackQuery = query  # type: ignore[assignment]

    await q.edit_message_text(f"⏭️ Anzeige wird behalten.")
    logger.info(f"Deletion skipped for listing {listing_id}")


async def text_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle text messages for active user interactions (feedback, editing)."""
    if update.message is None:
        return

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    text = update.message.text or ""

    user_state = _user_states.get(chat_id)
    if not user_state:
        return  # No active state – ignore

    action = str(user_state.get("action", ""))
    listing_id = user_state.get("listing_id")

    # Clear state
    del _user_states[chat_id]

    try:
        if action == "opt_feedback" and listing_id:
            await update.message.reply_text(
                "✅ Feedback empfangen. Optimierung mit deinem Feedback wird noch nicht automatisch angewendet.\n"
                "Bitte nutze den täglichen Check erneut."
            )

    except Exception as e:
        logger.error(f"Text handler error for action '{action}': {e}", exc_info=True)
        await update.message.reply_text("❌ Fehler bei der Verarbeitung. Bitte versuche es erneut.")
