"""Tests for the daily_check handler – startup message, lock, and shutdown."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import bot.handlers.daily_check as dc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(chat_id: int = 12345) -> MagicMock:
    update = MagicMock()
    update.message = AsyncMock()
    update.effective_chat.id = chat_id
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.bot_data = {}
    return context


# ---------------------------------------------------------------------------
# 1. Startup confirmation message is always sent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_handler_sends_start_message() -> None:
    """daily_check_handler must send the confirmation before doing anything."""
    update = _make_update()
    context = _make_context()

    with patch.object(dc, "run_daily_check", new=AsyncMock()) as mock_run:
        await dc.daily_check_handler(update, context)

    update.message.reply_text.assert_awaited_once()
    call_text: str = update.message.reply_text.call_args[0][0]
    assert "gestartet" in call_text.lower() or "check" in call_text.lower()
    mock_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_handler_passes_manual_true() -> None:
    """daily_check_handler must pass manual=True to run_daily_check."""
    update = _make_update()
    context = _make_context()

    with patch.object(dc, "run_daily_check", new=AsyncMock()) as mock_run:
        await dc.daily_check_handler(update, context)

    _, kwargs = mock_run.call_args
    assert kwargs.get("manual") is True


# ---------------------------------------------------------------------------
# 2. Double-check lock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_handler_blocked_when_already_running() -> None:
    """If a check is already running for this chat, send 'already running' and abort."""
    chat_id = 99999
    update = _make_update(chat_id)
    context = _make_context()

    # Simulate a running check
    async with dc._running_checks_lock:
        dc._running_checks.add(chat_id)

    try:
        with patch.object(dc, "run_daily_check", new=AsyncMock()) as mock_run:
            await dc.daily_check_handler(update, context)

        mock_run.assert_not_awaited()
        reply_text: str = update.message.reply_text.call_args[0][0]
        assert "läuft bereits" in reply_text.lower() or "running" in reply_text.lower()
    finally:
        dc._running_checks.discard(chat_id)


# ---------------------------------------------------------------------------
# 3. cancel_all_tasks cleans up
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_all_tasks_cancels_registered_tasks() -> None:
    """cancel_all_tasks() must cancel every task in _active_tasks."""
    async def _long_running() -> None:
        await asyncio.sleep(9999)

    task = asyncio.create_task(_long_running())
    dc._active_tasks.add(task)

    dc.cancel_all_tasks()
    await asyncio.sleep(0)  # let event loop process cancellation

    assert task.cancelled()
    dc._active_tasks.discard(task)


@pytest.mark.asyncio
async def test_cancel_all_tasks_clears_on_done() -> None:
    """Tasks registered with add_done_callback remove themselves from _active_tasks."""
    async def _noop() -> None:
        pass

    task = asyncio.create_task(_noop())
    dc._active_tasks.add(task)
    task.add_done_callback(dc._active_tasks.discard)

    await task  # completes normally
    assert task not in dc._active_tasks
