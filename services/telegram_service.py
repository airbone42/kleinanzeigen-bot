"""Telegram-specific helper utilities."""
import logging
from typing import Optional

from telegram import Bot, Message
from telegram.error import BadRequest, TelegramError

logger = logging.getLogger(__name__)


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    parse_mode: str = "Markdown",
    **kwargs: object,
) -> Optional[Message]:
    """Send a Telegram message with error handling.

    Args:
        bot: Telegram bot instance
        chat_id: Target chat ID
        text: Message text
        parse_mode: Message parse mode
        **kwargs: Additional kwargs for send_message

    Returns:
        Sent Message or None on error
    """
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            **kwargs,
        )
    except BadRequest as e:
        # Typically "can't parse entities": retry as plain text so the message
        # (and any reply_markup) is still delivered instead of being lost.
        logger.warning(f"send_message parse error for {chat_id}, retrying as plain text: {e}")
        try:
            return await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=None, **kwargs
            )
        except TelegramError as e2:
            logger.error(f"Failed to send message to {chat_id}: {e2}")
            return None
    except TelegramError as e:
        logger.error(f"Failed to send message to {chat_id}: {e}")
        return None


async def safe_edit_message(
    message: Message,
    text: str,
    parse_mode: str = "Markdown",
    **kwargs: object,
) -> Optional[Message]:
    """Edit a Telegram message with error handling.

    Args:
        message: Original Message to edit
        text: New message text
        parse_mode: Message parse mode
        **kwargs: Additional kwargs for edit_text

    Returns:
        Edited Message or None on error
    """
    try:
        return await message.edit_text(text=text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        logger.warning(f"edit_text parse error, retrying as plain text: {e}")
        try:
            return await message.edit_text(text=text, parse_mode=None, **kwargs)
        except TelegramError as e2:
            logger.error(f"Failed to edit message: {e2}")
            return None
    except TelegramError as e:
        logger.error(f"Failed to edit message: {e}")
        return None


def truncate_for_telegram(text: str, max_length: int = 4096) -> str:
    """Truncate text to fit Telegram's message length limit.

    Args:
        text: Input text
        max_length: Maximum allowed length (default 4096)

    Returns:
        Truncated text with ellipsis if needed
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."
