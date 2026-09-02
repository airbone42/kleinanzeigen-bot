"""Runtime patches for the upstream kleinanzeigen-bot package.

Kleinanzeigen serves two ad-page layouts. The bot extracts shipping info only
from the legacy DOM (``.boxedarticle--details--shipping``). On the redesigned
(Astro) layout that element is absent, so ``shipping_type`` becomes
``NOT_APPLICABLE`` while ``sell_directly`` still comes from the manage-ads API
(``buyNowEligible``). The resulting ad fails validation with
"sell_directly requires shipping_type to be SHIPPING" and the download aborts —
no YAML is written, which in turn breaks republishing.

The redesigned layout carries the same information in the Astro island props
(``shippingHeader``: "+ Versand ab 0,49 €" / "Nur Abholung" / "Versand möglich"),
so the fallback re-reads it from there whenever the legacy extraction comes up
empty. Remove once upstream handles the redesigned layout.
"""
import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

SHIPPING_OPTIONS_URL = (
    "https://gateway.kleinanzeigen.de/postad/api/v1/shipping-options?posterType=PRIVATE"
)
_PRICE_RE = re.compile(r"([\d.,]+)\s*€")

_patched = False


def apply_patches() -> None:
    """Apply all runtime patches (idempotent)."""
    global _patched
    if _patched:
        return
    _patch_shipping_extraction()
    _patched = True


def _patch_shipping_extraction() -> None:
    from kleinanzeigen_bot import extract

    original = extract.AdExtractor._extract_shipping_info_from_ad_page
    if getattr(original, "_kb_patched", False):
        return

    async def _patched_extract_shipping_info(self: Any) -> tuple:
        result = await original(self)
        if result[0] != "NOT_APPLICABLE":
            return result
        fallback = await shipping_from_island_props(self)
        if fallback is None:
            return result
        logger.info(f"Shipping info recovered from Astro island props: {fallback}")
        return fallback

    _patched_extract_shipping_info._kb_patched = True  # type: ignore[attr-defined]
    extract.AdExtractor._extract_shipping_info_from_ad_page = _patched_extract_shipping_info


async def shipping_from_island_props(extractor: Any) -> Optional[tuple]:
    """Derive (shipping_type, shipping_costs, shipping_options) from island props."""
    try:
        props = await extractor._extract_island_props()
    except Exception as e:
        logger.warning(f"Could not read Astro island props for shipping fallback: {e}")
        return None

    raw = props.get("shippingHeader") if isinstance(props, dict) else None
    text = extractor._unwrap_island_value(raw)
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()

    if "Abholung" in text:
        return ("PICKUP", None, None)
    if "€" not in text:
        return ("SHIPPING", None, None) if "Versand" in text else None

    from kleinanzeigen_bot.utils import misc

    match = _PRICE_RE.search(text)
    if not match:
        return ("SHIPPING", None, None)
    try:
        costs = float(misc.parse_decimal(match.group(1)))
    except Exception as e:
        logger.warning(f"Could not parse shipping costs from {text!r}: {e}")
        return ("SHIPPING", None, None)
    options = await _lookup_shipping_options(extractor, costs)
    return ("SHIPPING", costs, options)


async def _lookup_shipping_options(extractor: Any, costs: float) -> Optional[list[str]]:
    """Map shipping costs to kleinanzeigen-bot shipping option names."""
    from kleinanzeigen_bot.model.ad_model import OPTION_NAME_BY_CARRIER_CODE

    try:
        response = await extractor.web_request(SHIPPING_OPTIONS_URL)
        options = json.loads(response["content"])["data"]["shippingOptionsResponse"]["options"]
    except Exception as e:
        logger.warning(f"Could not load shipping options from gateway API: {e}")
        return None

    download_config = extractor.config.download
    excluded = set(download_config.excluded_shipping_options or [])
    price_in_cent = round(costs * 100)
    matching = [o for o in options if o.get("priceInEuroCent") == price_in_cent]
    if not matching:
        return None

    if download_config.include_all_matching_shipping_options:
        package_size = matching[0].get("packageSize")
        names = [
            OPTION_NAME_BY_CARRIER_CODE[o["id"]]
            for o in options
            if o.get("packageSize") == package_size
            and o.get("id") in OPTION_NAME_BY_CARRIER_CODE
            and OPTION_NAME_BY_CARRIER_CODE[o["id"]] not in excluded
        ]
    else:
        name = OPTION_NAME_BY_CARRIER_CODE.get(matching[0].get("id"))
        names = [name] if name and name not in excluded else []

    return names or None
