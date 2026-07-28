"""Handler for /logs command — tails the kleinanzeigen_bot.log file."""
import html
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from config.settings import settings

logger = logging.getLogger(__name__)

_TAIL_LINES = 60
_MAX_CHUNK = 4000  # Telegram message character limit


async def logs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /logs command — send the last N lines of kleinanzeigen_bot.log."""
    if update.message is None:
        return

    log_path = settings.kleinanzeigen_config_path / "kleinanzeigen_bot.log"

    if not log_path.exists():
        await update.message.reply_text("❌ Log-Datei nicht gefunden: `kleinanzeigen_bot.log`", parse_mode="Markdown")
        return

    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-_TAIL_LINES:])

        if not tail.strip():
            await update.message.reply_text("📋 Log-Datei ist leer.")
            return

        header = f"📋 Letzte {min(_TAIL_LINES, len(lines))} Zeilen von `kleinanzeigen_bot.log`:\n\n"
        full = header + tail

        # HTML <pre> with escaping: a backtick in a log line (or a chunk boundary
        # splitting a Markdown code fence) would otherwise break message parsing.
        for i in range(0, len(full), _MAX_CHUNK):
            chunk = full[i:i + _MAX_CHUNK]
            await update.message.reply_text(
                f"<pre>{html.escape(chunk)}</pre>", parse_mode="HTML"
            )

    except Exception as e:
        logger.error(f"Failed to read log file: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Fehler beim Lesen der Log-Datei: {e}")
