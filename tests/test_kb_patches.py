"""Tests for the runtime patches applied to the upstream kleinanzeigen-bot."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import kb_patches


def _extractor(shipping_header, *, gateway_options=None):
    """Build a fake AdExtractor exposing what the fallback touches."""
    ex = MagicMock()
    ex._extract_island_props = AsyncMock(
        return_value={"shippingHeader": [0, shipping_header]} if shipping_header is not None else {}
    )
    ex._unwrap_island_value = lambda v: v[1] if isinstance(v, list) and len(v) == 2 else v
    ex.config = SimpleNamespace(
        download=SimpleNamespace(
            include_all_matching_shipping_options=False,
            excluded_shipping_options=[],
        )
    )
    options = gateway_options if gateway_options is not None else [
        {"id": "HERMES_001", "priceInEuroCent": 49, "packageSize": "S"},
        {"id": "HERMES_002", "priceInEuroCent": 49, "packageSize": "S"},
        {"id": "DHL_002", "priceInEuroCent": 550, "packageSize": "M"},
    ]
    ex.web_request = AsyncMock(
        return_value={
            "content": json.dumps({"data": {"shippingOptionsResponse": {"options": options}}})
        }
    )
    return ex


@pytest.mark.asyncio
async def test_pickup_from_island_props() -> None:
    ex = _extractor("Nur Abholung")
    assert await kb_patches.shipping_from_island_props(ex) == ("PICKUP", None, None)


@pytest.mark.asyncio
async def test_shipping_without_price() -> None:
    ex = _extractor("Versand möglich")
    assert await kb_patches.shipping_from_island_props(ex) == ("SHIPPING", None, None)


@pytest.mark.asyncio
async def test_shipping_with_price_maps_option() -> None:
    ex = _extractor("+ Versand ab 0,49 €")
    assert await kb_patches.shipping_from_island_props(ex) == (
        "SHIPPING", 0.49, ["Hermes_Päckchen"],
    )


@pytest.mark.asyncio
async def test_shipping_with_all_matching_options() -> None:
    ex = _extractor("+ Versand ab 0,49 €")
    ex.config.download.include_all_matching_shipping_options = True
    ship_type, costs, options = await kb_patches.shipping_from_island_props(ex)
    assert (ship_type, costs) == ("SHIPPING", 0.49)
    assert options == ["Hermes_Päckchen", "Hermes_S"]


@pytest.mark.asyncio
async def test_excluded_option_is_dropped() -> None:
    ex = _extractor("+ Versand ab 0,49 €")
    ex.config.download.excluded_shipping_options = ["Hermes_Päckchen"]
    assert await kb_patches.shipping_from_island_props(ex) == ("SHIPPING", 0.49, None)


@pytest.mark.asyncio
async def test_gateway_failure_keeps_shipping_type() -> None:
    ex = _extractor("+ Versand ab 1.234,50 €")
    ex.web_request = AsyncMock(side_effect=RuntimeError("Failed to fetch"))
    assert await kb_patches.shipping_from_island_props(ex) == ("SHIPPING", 1234.5, None)


@pytest.mark.asyncio
async def test_no_island_props_returns_none() -> None:
    assert await kb_patches.shipping_from_island_props(_extractor(None)) is None


@pytest.mark.asyncio
async def test_patch_only_kicks_in_for_not_applicable() -> None:
    from kleinanzeigen_bot import extract

    original = extract.AdExtractor._extract_shipping_info_from_ad_page
    try:
        legacy_result = ("SHIPPING", 5.49, ["DHL_5"])

        async def _legacy(self):
            return legacy_result

        extract.AdExtractor._extract_shipping_info_from_ad_page = _legacy
        kb_patches._patched = False
        kb_patches.apply_patches()
        patched = extract.AdExtractor._extract_shipping_info_from_ad_page

        # legacy DOM present -> untouched
        ex = _extractor("Nur Abholung")
        assert await patched(ex) == legacy_result

        # legacy DOM missing -> island fallback
        legacy_result = ("NOT_APPLICABLE", None, None)
        assert await patched(_extractor("+ Versand ab 0,49 €")) == (
            "SHIPPING", 0.49, ["Hermes_Päckchen"],
        )
    finally:
        extract.AdExtractor._extract_shipping_info_from_ad_page = original
        kb_patches._patched = False
