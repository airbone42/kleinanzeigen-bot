"""Inline keyboards for the Telegram bot."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def listing_optimize_keyboard(listing_id: str) -> InlineKeyboardMarkup:
    """Keyboard for listing optimization approval."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Übernehmen", callback_data=f"opt_accept:{listing_id}"
                ),
            ]
        ]
    )


def listing_delete_keyboard(listing_id: str) -> InlineKeyboardMarkup:
    """Keyboard for listing deletion confirmation."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑️ Löschen", callback_data=f"del_confirm:{listing_id}"
                ),
                InlineKeyboardButton(
                    "⏭️ Behalten", callback_data=f"del_skip:{listing_id}"
                ),
            ]
        ]
    )


def confirm_keyboard(action: str, item_id: str) -> InlineKeyboardMarkup:
    """Generic confirmation keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Ja", callback_data=f"{action}_yes:{item_id}"),
                InlineKeyboardButton("❌ Nein", callback_data=f"{action}_no:{item_id}"),
            ]
        ]
    )
