from unittest.mock import AsyncMock

import pytest

from models.listing import Listing
from services.discogs_sync import (
    DiscogsInventoryItem,
    DiscogsSyncService,
    _extract_artist,
    _map_condition_to_special_attributes,
    build_discogs_description,
)


def _item(
    *,
    release_id: str,
    artist: str,
    median: float,
    is_mainstream: bool = True,
) -> DiscogsInventoryItem:
    return DiscogsInventoryItem(
        inventory_id=f"inv-{release_id}",
        release_id=release_id,
        artist=artist,
        title="Test Release",
        label="Test Label",
        year=2001,
        media_condition="VG+",
        sleeve_condition="VG+",
        price_eur=40.0,
        median_price_eur=median,
        image_url="",
        is_mainstream=is_mainstream,
    )


def test_filter_treasures_only_mainstream_over_50() -> None:
    service = DiscogsSyncService()
    items = [
        _item(release_id="1", artist="Daft Punk", median=60.0, is_mainstream=True),
        _item(release_id="2", artist="Unknown Artist", median=100.0, is_mainstream=False),
        _item(release_id="3", artist="Kraftwerk", median=49.99, is_mainstream=True),
    ]

    treasures = service.filter_treasures(items)

    assert [item.release_id for item in treasures] == ["1"]


def test_discogs_description_excludes_discogs_and_private_sale_phrases() -> None:
    text = build_discogs_description(_item(release_id="999", artist="Aphex Twin", median=88.0))
    lower = text.lower()
    assert "discogs" not in lower
    assert "release-id" not in lower
    assert "inventory-id" not in lower
    assert "privatverkauf" not in lower


def test_extract_artist_supports_discogs_inventory_artist_string() -> None:
    release_data = {"artist": "Die Ärzte"}
    assert _extract_artist(release_data) == "Die Ärzte"


def test_map_condition_to_special_attributes() -> None:
    assert _map_condition_to_special_attributes("Near Mint (NM or M-)") == {
        "condition_s": "like_new"
    }
    assert _map_condition_to_special_attributes("Good Plus (G+)") == {
        "condition_s": "damaged"
    }
    assert _map_condition_to_special_attributes("Very Good Plus (VG+)") == {
        "condition_s": "used"
    }


@pytest.mark.asyncio
async def test_fetch_release_median_price_falls_back_to_price_suggestions(mocker) -> None:
    service = DiscogsSyncService()

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self._payload

    class _Client:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            _Client.calls += 1
            if "price_suggestions" in url:
                return _Resp(
                    {
                        "Near Mint (NM or M-)": {"currency": "EUR", "value": 246.5},
                        "Very Good Plus (VG+)": {"currency": "EUR", "value": 188.5},
                        "Good Plus (G+)": {"currency": "EUR", "value": 72.5},
                        "Good (G)": {"currency": "EUR", "value": 43.5},
                    }
                )
            return _Resp(
                {
                    "num_for_sale": 25,
                    "lowest_price": {"value": 42.95, "currency": "EUR"},
                    "blocked_from_sale": False,
                }
            )

    mocker.patch("services.discogs_sync.httpx.AsyncClient", return_value=_Client())
    median = await service._fetch_release_median_price("1124530")
    assert median == 130.5


@pytest.mark.asyncio
async def test_sync_reconciles_stale_mapping_and_deletes_removed_release(mocker) -> None:
    service = DiscogsSyncService()

    mocker.patch(
        "services.discogs_sync.discogs_mapping_repo.get_active_by_chat",
        new=AsyncMock(
            return_value=[
                {
                    "discogs_release_id": "100",
                    "discogs_inventory_id": "inv-100",
                    "artist": "Daft Punk",
                    "title": "Homework",
                    "label": "Virgin",
                    "year": 1997,
                    "media_condition": "VG+",
                    "discogs_price": 55.0,
                    "median_price": 90.0,
                    "image_url": "",
                    "kleinanzeigen_listing_id": "111",
                    "kleinanzeigen_listing_url": "https://www.kleinanzeigen.de/s-anzeige/id/111",
                }
            ]
        ),
    )
    upsert_mock = mocker.patch(
        "services.discogs_sync.discogs_mapping_repo.upsert_mapping",
        new=AsyncMock(return_value=None),
    )
    mark_deleted_mock = mocker.patch(
        "services.discogs_sync.discogs_mapping_repo.mark_deleted",
        new=AsyncMock(return_value=None),
    )

    service.kleinanzeigen_service.get_active_listings = AsyncMock(
        return_value=[
            Listing(
                listing_id="222",
                title="Daft Punk Homework",
                description="foo bar",
                price=89.0,
                category="Musik",
            )
        ]
    )
    service.kleinanzeigen_service.delete_listing = AsyncMock(return_value=True)

    result = await service.sync(chat_id=1234, inventory_items=[])

    service.kleinanzeigen_service.delete_listing.assert_awaited_once_with("222")
    assert upsert_mock.await_count == 1
    mark_deleted_mock.assert_awaited_once_with(1234, "100")
    assert result["updated_mappings"] == 1
    assert result["deleted"] == [{"release_id": "100", "listing_id": "222"}]
