"""Handlers for /start and /help commands."""
import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

HELP_TEXT = """
🤖 *Kleinanzeigen Bot*

Ich helfe dir dabei, Kleinanzeigen-Inserate automatisch zu erstellen und zu verwalten.

*Befehle:*
/start – Bot starten
/help – Diese Hilfe anzeigen
/check – Täglichen Anzeigen-Check manuell starten
/weiter – Nächste Seite ablaufender Anzeigen verarbeiten
/renew – Anzeige per ID erneuern (/renew <ID>)
/discogssync – Discogs-Inventar mit Kleinanzeigen synchronisieren
/diagnose – Browser-Diagnose (Troubleshooting)
/logs – Letzte Zeilen des Kleinanzeigen-Logs anzeigen

*Neues Inserat erstellen:*
Schick mir einfach ein oder mehrere Fotos des Artikels (bis zu 10). Ich analysiere die Bilder und erstelle automatisch einen Entwurf mit Titel, Beschreibung und Preisvorschlag.

*Funktionen:*
• 📸 Automatische Bildanalyse mit KI
• ✍️ Titel und Beschreibungstext auf Knopfdruck
• 💰 Intelligente Preisschätzung
• 🔄 Täglicher Check für ablaufende Anzeigen
• ✅ Alles immer zuerst als Entwurf – du hast das letzte Wort!
"""

START_TEXT = """
👋 Willkommen beim Kleinanzeigen Bot!

Schick mir Fotos deiner Artikel, und ich erstelle automatisch Inserate für dich.

Tippe /help für eine Übersicht aller Funktionen.
"""


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    if update.message is None:
        return
    logger.info(f"User {update.effective_user.id} started the bot")
    await update.message.reply_text(START_TEXT, parse_mode="Markdown")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    if update.message is None:
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")
