"""Price estimation and shipping type detection node."""
import asyncio
import logging
import re
import statistics
from urllib.parse import quote_plus
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from models.state import ListingCreatorState
from services.openrouter import get_llm

logger = logging.getLogger(__name__)

PRICE_SYSTEM_PROMPT = """Du bist ein Experte für Gebrauchtwarenpreise in Deutschland (Kleinanzeigen.de).
Schätze einen realistischen, marktgerechten Verkaufspreis für den beschriebenen Artikel.

Berücksichtige:
- Aktueller Marktwert auf Kleinanzeigen.de
- Zustand des Artikels
- Beliebtheit und Nachfrage
- Empfohlen: etwas günstiger als Neupreis, wettbewerbsfähig

Berücksichtige die Objektgröße beim Preisansatz:
- Kleine Artikel (Kleidung, Bücher, Elektronik, Schmuck): Preis eher im oberen Marktbereich ansetzen
- Große Artikel (Möbel, Haushaltsgeräte, Fahrräder): Preis eher im unteren Marktbereich – Lagerkosten für Verkäufer hoch
- In jedem Fall: deutlich unter Neupreis, marktgerecht

Antworte NUR mit einer Zahl (Preis in EUR, ohne Währungszeichen).
Beispiel: 45
Beispiel: 12.50"""

SHIPPING_SYSTEM_PROMPT = """Entscheide ob der Artikel per Paket/Post versendet werden kann und schätze die Paketgröße.

Antworte NUR mit einem der folgenden Werte:
- PICKUP    – Möbel, Haushaltsgroßgeräte, Fahrräder, E-Bikes, große Fernseher (>32 Zoll), Monitore (>27 Zoll), Kinderwagen, Sperrmüll
- SHIPPING_S – Kleine Artikel bis ~2 kg: Handy, Tablet, Bücher, Kleidung, Schuhe, DVDs, CDs, Vinyl, Schmuck, kleine Haushaltswaren
- SHIPPING_M – Mittlere Artikel 2–10 kg: Küchengeräte, mittlere Elektronik, Werkzeug, Spielzeug (groß), kleine Haushaltsgeräte
- SHIPPING_L – Große Artikel bis 31 kg: große Elektronik, Heimkino, Sportgeräte, große Werkzeuge"""

_SHIPPING_OPTIONS: dict[str, list[str]] = {
    "S": ["DHL_2", "Hermes_S"],
    "M": ["DHL_5", "Hermes_M"],
    "L": ["DHL_10", "Hermes_L"],
}

_PREMIUM_HINTS = ("premium", "pro", "streamliner", "bullet", "tri suit", "carbon")


def _parse_shipping(result: str) -> tuple[str, str, list[str]]:
    """Parse LLM result into (shipping_size, shipping_type, shipping_options)."""
    r = result.strip().upper()
    if "SHIPPING_S" in r:
        return "S", "SHIPPING", _SHIPPING_OPTIONS["S"]
    if "SHIPPING_M" in r:
        return "M", "SHIPPING", _SHIPPING_OPTIONS["M"]
    if "SHIPPING_L" in r:
        return "L", "SHIPPING", _SHIPPING_OPTIONS["L"]
    return "PICKUP", "PICKUP", []


async def estimate_price(state: ListingCreatorState) -> dict[str, Any]:
    """Estimate a market-appropriate price and determine shipping type.

    Args:
        state: Current workflow state

    Returns:
        Updated state with price and shipping_type fields
    """
    analysis = state.get("image_analysis", "")
    title = state.get("title", "")

    logger.info("Estimating price and determining shipping type")

    # Run price estimation, web market hint and shipping detection concurrently
    price_task = _estimate_price_llm(title, analysis)
    web_hint_task = _estimate_market_price_from_web(title, analysis)
    shipping_task = _determine_shipping_llm(title, analysis)

    price, web_hint, raw_shipping = await asyncio.gather(price_task, web_hint_task, shipping_task)

    if web_hint is not None:
        premium_factor = 0.95 if _is_premium_item(title, analysis) else 0.85
        floor_price = web_hint * premium_factor
        if price < floor_price:
            logger.info(
                "Raising estimated price from %.2f to %.2f based on market hint %.2f",
                price,
                floor_price,
                web_hint,
            )
            price = floor_price

    shipping_size, shipping_type, _ = _parse_shipping(raw_shipping)
    logger.info(f"Estimated price: {price} EUR, shipping_size: {shipping_size}")
    return {"price": price, "shipping_type": shipping_type, "shipping_size": shipping_size}


def _is_premium_item(title: str, analysis: str) -> bool:
    hay = f"{title} {analysis}".lower()
    return any(h in hay for h in _PREMIUM_HINTS)


async def _estimate_market_price_from_web(title: str, analysis: str) -> float | None:
    """Fetch lightweight market price hints from web snippets (best effort)."""
    query = _build_market_query(title, analysis)
    if not query:
        return None

    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code != 200:
            return None
        prices = _extract_euro_prices(response.text)
        if not prices:
            return None
        trimmed = sorted(prices)[:8]
        return float(statistics.median(trimmed))
    except Exception:
        return None


def _build_market_query(title: str, analysis: str) -> str:
    model_tokens = re.findall(r"\b[A-Z0-9][A-Za-z0-9-]{3,}\b", f"{title} {analysis}")
    if not model_tokens:
        return title.strip()
    unique: list[str] = []
    seen: set[str] = set()
    for token in model_tokens:
        key = token.lower()
        if key not in seen:
            seen.add(key)
            unique.append(token)
    return " ".join(unique[:6]) + " kleinanzeigen preis"


def _extract_euro_prices(text: str) -> list[float]:
    prices: list[float] = []
    for raw in re.findall(r"(\d{1,4}(?:[.,]\d{1,2})?)\s?(?:€|eur|euro)", text, flags=re.IGNORECASE):
        value = raw.replace(",", ".")
        try:
            price = float(value)
            if 5.0 <= price <= 10000.0:
                prices.append(price)
        except ValueError:
            continue
    return prices


async def _estimate_price_llm(title: str, analysis: str) -> float:
    """Call LLM to estimate price."""
    await asyncio.sleep(0.3)  # Rate limiting
    try:
        llm = get_llm("price_estimation", temperature=0.3)
        messages = [
            SystemMessage(content=PRICE_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"Schätze den Verkaufspreis für:\n\n"
                    f"Artikel: {title}\n\n"
                    f"Analyse:\n{analysis}"
                )
            ),
        ]
        response = await llm.ainvoke(messages)
        content = str(response.content).strip()

        price_match = re.search(r'\d+(?:[.,]\d{1,2})?', content.replace(",", "."))
        if price_match:
            price = float(price_match.group().replace(",", "."))
            return max(1.0, min(10000.0, price))
        else:
            logger.warning(f"Could not parse price from: '{content}'")
            return 10.0
    except Exception as e:
        logger.error(f"Price estimation failed: {e}", exc_info=True)
        return 10.0


async def _determine_shipping_llm(title: str, analysis: str) -> str:
    """Call LLM to determine if item can be shipped."""
    await asyncio.sleep(0.3)  # Rate limiting
    try:
        llm = get_llm("price_estimation", temperature=0.1)
        messages = [
            SystemMessage(content=SHIPPING_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"Artikel: {title}\n\n"
                    f"Beschreibung:\n{analysis}"
                )
            ),
        ]
        response = await llm.ainvoke(messages)
        return str(response.content).strip()
    except Exception as e:
        logger.error(f"Shipping determination failed: {e}", exc_info=True)
        return "PICKUP"
