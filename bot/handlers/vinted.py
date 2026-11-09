"""Telegram side of the Vinted mirror: /vinted, /vintedsync, /vintedlogin, schedule."""
import asyncio
import logging
import time
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from config.settings import settings
from services.telegram_service import truncate_for_telegram

logger = logging.getLogger(__name__)

_sync_lock = asyncio.Lock()

# Telegram splits a pasted cookie header (often > 4096 chars) into several messages and
# only the first carries /vintedlogin. Collect the parts per chat and log in once no
# further part arrived for a few seconds.
_login_parts: dict[int, dict[str, Any]] = {}
LOGIN_COLLECT_SECONDS = 300
LOGIN_DEBOUNCE_SECONDS = 4

LOGIN_HELP = (
    "So verbindest du Vinted (einmalig, die Sitzung hält dann von selbst):\n"
    "1. Am PC ein privates Fenster öffnen und dort auf vinted.de einloggen "
    "(eine eigene Sitzung nur für den Bot, sonst beendet Vinted beide).\n"
    "2. F12 → Netzwerk → eine Anfrage an vinted.de anklicken → Anfrage-Header → "
    "den Wert von «Cookie» kopieren.\n"
    "3. Das private Fenster schließen, ohne dich abzumelden.\n"
    "4. Hier senden: /vintedlogin <kopierter Wert>\n"
    "Teilt Telegram den langen Wert in mehrere Nachrichten, sammle ich die Teile ein. "
    "Jede Nachricht lösche ich sofort wieder. Nötig sind access_token_web und refresh_token_web, "
    "es reicht auch nur: access_token_web=...; refresh_token_web=..."
)


def _disabled_text() -> str:
    return "Vinted-Spiegel ist aus (VINTED_ENABLED=true in der Konfiguration setzen)."


async def vinted_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services.vinted_sync import VintedSyncService

    if update.message is None:
        return
    await update.message.reply_text(truncate_for_telegram(await VintedSyncService().status_text()))


async def vinted_login_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    chat_id = message.chat_id
    parts = (message.text or "").split(None, 1)
    raw = parts[1].strip() if len(parts) > 1 else ""
    _login_parts[chat_id] = {"parts": [], "task": None, "started": time.time()}
    if not raw:
        await message.reply_text(LOGIN_HELP + "\n\nSchick den Wert jetzt (gern in mehreren Nachrichten).")
        return
    await _collect_login_part(context, chat_id, message, raw)


async def consume_vinted_login_part(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Take a follow-up part of a split /vintedlogin message. True if consumed."""
    message = update.message
    if message is None:
        return False
    state = _login_parts.get(message.chat_id)
    if state is None:
        return False
    if time.time() - state["started"] > LOGIN_COLLECT_SECONDS:
        _login_parts.pop(message.chat_id, None)
        return False
    await _collect_login_part(context, message.chat_id, message, message.text or "")
    return True


async def _collect_login_part(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message: Any, text: str
) -> None:
    state = _login_parts[chat_id]
    state["parts"].append(text)
    # Session cookies must not stay in the chat history.
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Could not delete /vintedlogin part: {e}")
    task = state.get("task")
    if task is not None:
        task.cancel()
    state["task"] = context.application.create_task(_finish_login_later(context, chat_id))


async def _finish_login_later(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    from services.vinted_client import SESSION_COOKIES, VintedError, parse_cookie_header
    from services.vinted_sync import VintedSyncService

    await asyncio.sleep(LOGIN_DEBOUNCE_SECONDS)
    state = _login_parts.get(chat_id)
    if state is None:
        return
    raw = "".join(state["parts"])
    cookies = parse_cookie_header(raw)
    missing = [name for name in SESSION_COOKIES if not cookies.get(name)]
    if missing:
        state["task"] = None
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Noch nicht dabei: {', '.join(missing)}. Schick den Rest des Cookie-Werts.",
        )
        return
    _login_parts.pop(chat_id, None)
    status = await context.bot.send_message(chat_id=chat_id, text="⏳ Prüfe die Vinted-Sitzung...")
    try:
        user_id = await VintedSyncService().login(raw)
    except VintedError as e:
        await status.edit_text(f"❌ Vinted-Login fehlgeschlagen: {e}")
        return
    except Exception as e:
        logger.error(f"Vinted login failed: {e}", exc_info=True)
        await status.edit_text(f"❌ Vinted-Login fehlgeschlagen: {e}")
        return
    await status.edit_text(f"✅ Vinted verbunden (Benutzer {user_id}). /vintedsync startet den ersten Abgleich.")


async def run_vinted_sync(context: ContextTypes.DEFAULT_TYPE, chat_id: int, *, manual: bool) -> None:
    from bot.handlers.daily_check import _is_check_running
    from services.vinted_sync import VintedSyncService

    if not settings.vinted_enabled:
        if manual:
            await context.bot.send_message(chat_id=chat_id, text=_disabled_text())
        return
    if _sync_lock.locked() or await _is_check_running(chat_id):
        # Both drive the Kleinanzeigen browser profile; running them together kills sessions.
        if manual:
            await context.bot.send_message(
                chat_id=chat_id, text="⏳ Gerade läuft schon ein Abgleich oder Anzeigen-Check."
            )
        return
    async with _sync_lock:
        if manual:
            await context.bot.send_message(chat_id=chat_id, text="🔁 Vinted-Abgleich läuft...")
        try:
            report = await VintedSyncService().sync()
        except Exception as e:
            logger.error(f"Vinted sync failed: {e}", exc_info=True)
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Vinted-Abgleich: {e}")
            return
    text = report.format()
    if report.auth_required:
        text += "\n\n" + LOGIN_HELP
    if text:
        await context.bot.send_message(chat_id=chat_id, text=truncate_for_telegram(text))
    elif manual:
        await context.bot.send_message(chat_id=chat_id, text="✅ Vinted-Abgleich: nichts zu tun.")


async def vinted_retry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Retry rejected drafts / failed mirrors now, e.g. after a bot update."""
    from db.repository import vinted_mapping_repo

    if update.effective_chat is None:
        return
    count = await vinted_mapping_repo.reset_attempts()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"↻ {count} Vinted-Entwürfe/Fehler werden neu versucht.",
    )
    context.application.create_task(run_vinted_sync(context, update.effective_chat.id, manual=True))


async def vinted_sync_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    context.application.create_task(run_vinted_sync(context, chat_id, manual=True))


async def scheduled_vinted_sync(context: ContextTypes.DEFAULT_TYPE) -> None:
    for user_id in settings.telegram_allowed_user_ids[:1]:
        await run_vinted_sync(context, user_id, manual=False)


async def mirror_new_listing(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    listing_url: str,
    title: str,
    description: str,
    price: float,
    category: str,
    shipping_size: str,
    images: list[bytes],
) -> None:
    """Called right after a new Kleinanzeigen listing went live."""
    from services.vinted_client import VintedAuthError
    from services.vinted_sync import MirrorInput, VintedSyncService, ka_id_from_url

    if not settings.vinted_enabled:
        return
    ka_id = ka_id_from_url(listing_url)
    if not ka_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text="ℹ️ Vinted: Kleinanzeigen-ID unbekannt, der nächste Abgleich holt es nach.",
        )
        return
    status = await context.bot.send_message(chat_id=chat_id, text="⏳ Stelle auch auf Vinted ein...")
    try:
        result = await VintedSyncService().mirror(MirrorInput(
            ka_listing_id=ka_id, chat_id=chat_id, title=title, description=description,
            price=price, images=images, category=category, package_size=shipping_size,
        ))
        text = ("✅ " if result.status == "active" else "ℹ️ ") + result.message
    except VintedAuthError as e:
        text = f"⚠️ Vinted: {e}\n\n{LOGIN_HELP}"
    except Exception as e:
        logger.error(f"Vinted mirror failed: {e}", exc_info=True)
        text = f"⚠️ Vinted fehlgeschlagen: {e}"
    await status.edit_text(truncate_for_telegram(text))
