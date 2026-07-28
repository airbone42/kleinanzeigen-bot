from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot.handlers import new_listing


@pytest.mark.asyncio
async def test_prevent_duplicate_after_post_publish_error() -> None:
    new_listing._recent_upload_fingerprints.clear()

    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=1)
    bot.edit_message_text = AsyncMock()

    file_obj = AsyncMock()
    file_obj.download_as_bytearray.return_value = bytearray(b"img")
    bot.get_file.return_value = file_obj

    context = SimpleNamespace(bot=bot)

    graph = AsyncMock()
    graph.ainvoke.return_value = {
        "title": "Test Titel 12345",
        "description": "Test Beschreibung",
        "price": 19.0,
        "category": "Elektronik > Sonstiges",
        "shipping_type": "PICKUP",
        "shipping_size": "PICKUP",
        "image_analysis": "Analyse",
    }

    service = AsyncMock()
    service.create_draft.return_value = "https://www.kleinanzeigen.de/s-anzeige/id/1234567890"
    service.get_active_slots_usage.return_value = (1, 100)

    with patch("bot.handlers.new_listing.get_listing_creator", return_value=graph), patch(
        "bot.handlers.new_listing.draft_repo.create", new=AsyncMock(return_value=99)
    ), patch(
        "bot.handlers.new_listing.draft_repo.add_image", new=AsyncMock(return_value=None)
    ), patch(
        "bot.handlers.new_listing.draft_repo.update_status",
        new=AsyncMock(side_effect=RuntimeError("db write failed")),
    ), patch(
        "services.kleinanzeigen.KleinanzeigenService", return_value=service
    ):
        await new_listing._create_listing(context, 1234, ["fileA"], caption="Same caption")
        await new_listing._create_listing(context, 1234, ["fileA"], caption="Same caption")

    assert service.create_draft.await_count == 1
    duplicate_msgs = [
        call.kwargs.get("text", "")
        for call in bot.send_message.await_args_list
        if "bereits verarbeitet" in call.kwargs.get("text", "")
    ]
    assert duplicate_msgs, "expected second run to be blocked as duplicate"
