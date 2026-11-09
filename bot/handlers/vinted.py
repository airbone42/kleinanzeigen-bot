"""Telegram side of the Vinted mirror: /vinted, /vintedsync, /vintedlogin, schedule."""
import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from config.settings import settings
from services.telegram_service import truncate_for_telegram

logger = logging.getLogger(__name__)

_sync_lock = asyncio.Lock()

LOGIN_HELP = (
    "So verbindest du Vinted (einmalig, die Sitzung hält dann von selbst):\n"
    "1. Am PC auf vinted.de einloggen.\n"
    "2. F12 → Netzwerk → eine Anfrage an vinted.de anklicken → Anfrage-Header → "
    "den Wert von «Cookie» kopieren.\n"
    "3. Hier senden: /vintedlogin <kopierter Wert>\n"
    "Die Nachricht lösche ich sofort wieder. Nötig sind access_token_web und refresh_token_web."
)


def _disabled_text() -> str:
    return "Vinted-Spiegel ist aus (VINTED_ENABLED=true in der Konfiguration setzen)."


async def vinted_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services.vinted_sync import VintedSyncService

    if update.message is None:
        return
    await update.message.reply_text(truncate_for_telegram(await VintedSyncService().status_text()))


async def vinted_login_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services.vinted_client import VintedError
    from services.vinted_sync import VintedSyncService

    message = update.message
    if message is None:
        return
    raw = " ".join(context.args or []).strip()
    if not raw:
        await message.reply_text(LOGIN_HELP)
        return
    # Session cookies must not stay in the chat history.
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Could not delete /vintedlogin message: {e}")
    chat_id = message.chat_id
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
    await status.edit_text(f"✅ Vinted verbunden (Benutzer {user_id}).")


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
