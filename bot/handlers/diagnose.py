"""Handler for /diagnose command."""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from services.kleinanzeigen import KleinanzeigenService

logger = logging.getLogger(__name__)

_MAX_CHUNK = 4000  # Telegram message limit is 4096 chars


async def diagnose_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /diagnose command — runs kleinanzeigen-bot diagnose with verbose output."""
    if update.message is None:
        return

    await update.message.reply_text("🔍 Starte Diagnose... (kann ~10 Sekunden dauern)")

    try:
        service = KleinanzeigenService()
        output = await service.diagnose()
        for i in range(0, len(output), _MAX_CHUNK):
            chunk = output[i:i + _MAX_CHUNK]
            await update.message.reply_text(f"```\n{chunk}\n```", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Diagnose failed: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Diagnose fehlgeschlagen: {e}")
