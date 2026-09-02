"""Text generation node for creating listing title and description."""
import asyncio
import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from models.state import ListingCreatorState
from services.categories import CATEGORY_LIST as _CATEGORY_LIST, DEFAULT_CATEGORY as _DEFAULT_CATEGORY
from services.openrouter import get_text_llm, get_feedback_llm

logger = logging.getLogger(__name__)

TITLE_SYSTEM_PROMPT = """Du bist ein Experte für Kleinanzeigen-Inserate in Deutschland.
Deine Aufgabe ist es, prägnante, keyword-optimierte Titel für Kleinanzeigen zu erstellen.

Regeln:
- Maximale Länge: 50 Zeichen
- Sprache: Deutsch
- Enthält: Produkttyp, wichtigste Eigenschaft/Marke, Zustand (falls gut/neuwertig)
- Keine Sonderzeichen außer Bindestrichen
- Kein "VHB", "Privatverkauf", Preis etc. im Titel
- Wenn in Bildanalyse/Caption konkrete Maße oder Modellnummern stehen (z.B. 108 cm, 711100),
  müssen diese Suchbegriffe im Titel enthalten sein.
- Begriffe wie "defekt", "kaputt", "nicht funktionsfähig" NUR verwenden, wenn
  der Verkäufer das explizit in der Caption erwähnt

Antworte NUR mit dem Titel, ohne Anführungszeichen."""


def _extract_required_title_tokens(text: str) -> list[str]:
    """Extract high-value search tokens (dimension/model identifiers)."""
    tokens: list[str] = []

    for m in re.finditer(r"\b(\d{2,4}(?:[.,]\d+)?)\s*(cm|mm|m)\b", text, re.IGNORECASE):
        value = m.group(1).replace(",", ".")
        unit = m.group(2).lower()
        tokens.append(f"{value}{unit}")

    for m in re.finditer(
        r"\b(?:größe|groesse|breite|höhe|hoehe|länge|laenge|size|modell(?:nummer)?|model)\s*[:#-]?\s*([A-Z0-9-]{2,})\b",
        text,
        re.IGNORECASE,
    ):
        tokens.append(m.group(1))

    for m in re.finditer(r"\b\d{5,}\b", text):
        tokens.append(m.group(0))

    for m in re.finditer(r"[\"“”']([^\"“”']{4,80})[\"“”']", text):
        phrase = re.sub(r"\s{2,}", " ", m.group(1).strip())
        if len(phrase.split()) >= 2:
            tokens.append(phrase)

    for m in re.finditer(
        r"\b(?:modell|model)\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9 .-]{3,80})",
        text,
        re.IGNORECASE,
    ):
        phrase = re.sub(r"\s{2,}", " ", m.group(1).strip(" .,-"))
        if len(phrase.split()) >= 2:
            tokens.append(phrase)

    normalized: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        token = token.strip(" ,.;:-")
        if not token or "," in token:
            continue
        if token.lower().startswith("nummer "):
            continue
        key = token.lower()
        if key not in seen:
            seen.add(key)
            normalized.append(token)
    return normalized


def _token_priority(token: str) -> tuple[int, int]:
    """Score tokens so model and dimension identifiers are preferred."""
    t = token.strip().lower()
    if re.fullmatch(r"\d{5,}", t):
        return (0, -len(token))  # model number
    if re.fullmatch(r"\d{2,4}(?:[.,]\d+)?(?:cm|mm|m)?", t):
        return (1, -len(token))  # size/dimension
    return (2, -len(token))


def _ensure_title_contains_tokens(title: str, tokens: list[str], limit: int = 50) -> str:
    """Ensure required search tokens are present while respecting title length."""
    if not title:
        title = "Artikel zu verkaufen"

    missing = [
        t
        for t in tokens
        if re.search(rf"\b{re.escape(t)}\b", title, re.IGNORECASE) is None
    ]
    missing.sort(key=_token_priority)
    if not missing:
        return title

    suffix_parts: list[str] = []
    for token in missing:
        candidate_suffix = " ".join(suffix_parts + [token]).strip()
        if len(candidate_suffix) <= max(0, limit - 5):
            suffix_parts.append(token)
        if len(suffix_parts) >= 2:
            break

    if not suffix_parts:
        return title[:limit].rsplit(" ", 1)[0] or title[:limit]

    suffix = " ".join(suffix_parts)
    max_base_len = max(0, limit - len(suffix) - 1)
    base = title[:max_base_len].rstrip()
    if base and " " in base:
        base = base.rsplit(" ", 1)[0]

    combined = f"{base} {suffix}".strip()
    if len(combined) <= limit:
        return combined
    return combined[:limit].rsplit(" ", 1)[0] or combined[:limit]

DESCRIPTION_SYSTEM_PROMPT = """Du bist ein Experte für Kleinanzeigen-Beschreibungen in Deutschland.
Erstelle sachliche, seriöse Beschreibungen die Vertrauen aufbauen.

Wichtige Regeln:
- Beginne NIEMALS mit einer Begrüßung ("Hallo", "Hi", "Hey", "Guten Tag", "Liebe Interessenten" o.ä.)
- Starte DIREKT mit der sachlichen Beschreibung des Artikels
- Keine Grußformeln am Ende
- Schreibe aus Verkäufer-Perspektive und niemals Formulierungen wie
  "wie vom Verkäufer beschrieben", "laut Verkäufer", "im Bild zu sehen".
- Fokus auf technische Merkmale, Funktionen, Kompatibilität, Materialien,
  messbare Spezifikationen und Modellbesonderheiten.
- Vermeide reine Farb-/Optikbeschreibungen, wenn technische Infos verfügbar sind.

Struktur der Beschreibung:
1. Was ist der Artikel? (1-2 Sätze, direkt beginnend)
2. Zustand – nutze exakt einen dieser Begriffe aus der Bildanalyse:
   Neuwertig / Sehr gut erhalten / Gut erhalten / Gebraucht / Stark gebraucht
   Beschreibe eventuelle Mängel ehrlich.
   Begriffe wie "defekt", "kaputt", "nicht funktionsfähig" NUR verwenden, wenn
   der Verkäufer das explizit in der Caption erwähnt.
3. Maße/technische Details (falls bekannt)

NIEMALS hinzufügen: Versandhinweise oder Abholhinweise (z.B. "Abholung bevorzugt",
"Versand möglich", "Versand gegen Aufpreis", o.ä.) – diese Infos werden separat angegeben.

Sprache: Deutsch, sachlich und seriös.
Länge: 100-250 Wörter.
Antworte NUR mit der Beschreibung."""

FEEDBACK_SYSTEM_PROMPT = """Du bist ein Experte für Kleinanzeigen-Inserate.
Der Nutzer hat Feedback zu einem Inserat-Entwurf gegeben.
Überarbeite den Entwurf basierend auf dem Feedback.

Aktueller Entwurf:
Titel: {title}
Preis: {price} EUR
Beschreibung: {description}

Bildanalyse: {image_analysis}

Nutzer-Feedback: {feedback}

Antworte im JSON-Format:
{{
  "title": "Neuer Titel (max 50 Zeichen)",
  "description": "Neue Beschreibung",
  "price": 0.00
}}"""


async def generate_title(state: ListingCreatorState) -> dict[str, Any]:
    """Generate an optimized listing title from image analysis.

    Args:
        state: Current workflow state

    Returns:
        Updated state with title field
    """
    analysis = state.get("image_analysis", "")
    if not analysis:
        return {"title": "Artikel zu verkaufen"}

    logger.info("Generating listing title")
    await asyncio.sleep(0.3)  # Rate limiting

    try:
        llm = get_text_llm(temperature=0.6)
        caption = state.get("caption", "").strip()
        caption_hint = f"\n\nHinweis vom Verkäufer: {caption}" if caption else ""
        required_tokens = _extract_required_title_tokens(f"{analysis}\n{caption}")
        messages = [
            SystemMessage(content=TITLE_SYSTEM_PROMPT),
            HumanMessage(
                content=f"Erstelle einen Titel für diesen Artikel:\n\n{analysis}{caption_hint}"
            ),
        ]
        response = await llm.ainvoke(messages)
        title = str(response.content).strip().strip('"').strip("'")
        title = _ensure_title_contains_tokens(title, required_tokens, limit=50)

        # Enforce 50 char limit
        if len(title) > 50:
            title = title[:50].rsplit(" ", 1)[0]
            logger.debug(f"Title truncated to: '{title}'")

        logger.info(f"Generated title: '{title}'")
        return {"title": title}

    except Exception as e:
        logger.error(f"Title generation failed: {e}", exc_info=True)
        return {"title": "Artikel zu verkaufen"}


async def generate_description(state: ListingCreatorState) -> dict[str, Any]:
    """Generate a listing description from image analysis.

    Args:
        state: Current workflow state

    Returns:
        Updated state with description field
    """
    analysis = state.get("image_analysis", "")
    title = state.get("title", "")

    logger.info("Generating listing description")
    await asyncio.sleep(0.3)  # Rate limiting

    try:
        llm = get_text_llm(temperature=0.7)
        caption = state.get("caption", "").strip()
        caption_section = f"\n\nHinweis vom Verkäufer: {caption}" if caption else ""
        messages = [
            SystemMessage(content=DESCRIPTION_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"Erstelle eine Beschreibung für dieses Inserat:\n\n"
                    f"Titel: {title}\n\n"
                    f"Bildanalyse:\n{analysis}{caption_section}"
                )
            ),
        ]
        response = await llm.ainvoke(messages)
        description = str(response.content).strip()

        logger.info(f"Generated description: {len(description)} chars")
        return {"description": description}

    except Exception as e:
        logger.error(f"Description generation failed: {e}", exc_info=True)
        return {"description": "Gut erhaltener Artikel abzugeben. Besichtigung möglich."}


CATEGORY_SYSTEM_PROMPT = (
    'Du bist ein Experte für Kleinanzeigen-Kategorien.\n'
    'Wähle die passendste Kategorie für diesen Artikel aus der folgenden Liste.\n'
    'Antworte NUR mit dem exakten Kategorienamen aus der Liste, ohne weitere Erklärung.\n\n'
    'Verfügbare Kategorien:\n' + '\n'.join(_CATEGORY_LIST)
)


async def generate_category(state: ListingCreatorState) -> dict[str, Any]:
    """Select the most appropriate Kleinanzeigen category from image analysis."""
    analysis = state.get("image_analysis", "")
    if not analysis:
        return {"category": _DEFAULT_CATEGORY}

    logger.info("Generating listing category")

    try:
        llm = get_text_llm(temperature=0.1)
        messages = [
            SystemMessage(content=CATEGORY_SYSTEM_PROMPT),
            HumanMessage(content='Artikel-Beschreibung:\n\n' + analysis),
        ]
        response = await llm.ainvoke(messages)
        category = str(response.content).strip().strip('"').strip("'")

        if category not in _CATEGORY_LIST:
            logger.warning(f"LLM returned invalid category '{category}', using default")
            category = _DEFAULT_CATEGORY

        logger.info(f"Generated category: '{category}'")
        return {"category": category}

    except Exception as e:
        logger.error(f"Category generation failed: {e}", exc_info=True)
        return {"category": _DEFAULT_CATEGORY}


async def apply_feedback(state: ListingCreatorState) -> dict[str, Any]:
    """Apply user feedback to revise the draft.

    Args:
        state: Current workflow state with feedback

    Returns:
        Updated state with revised title, description, price
    """
    feedback = state.get("feedback", "")
    if not feedback:
        logger.warning("No feedback provided, skipping revision")
        return {}

    logger.info(f"Applying user feedback: {feedback[:100]}...")
    await asyncio.sleep(0.3)  # Rate limiting

    try:
        llm = get_feedback_llm(temperature=0.6)
        prompt = FEEDBACK_SYSTEM_PROMPT.format(
            title=state.get("title", ""),
            price=state.get("price", 0),
            description=state.get("description", ""),
            image_analysis=state.get("image_analysis", ""),
            feedback=feedback,
        )
        messages = [HumanMessage(content=prompt)]
        response = await llm.ainvoke(messages)

        content = str(response.content).strip()

        # Extract JSON from response
        json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            result: dict[str, Any] = {}
            if "title" in data:
                title = str(data["title"])[:50]
                result["title"] = title
            if "description" in data:
                result["description"] = str(data["description"])
            if "price" in data:
                try:
                    result["price"] = float(data["price"])
                except (ValueError, TypeError):
                    pass
            logger.info(f"Feedback applied successfully: {list(result.keys())}")
            return result
        else:
            logger.warning("Could not parse JSON from feedback response")
            return {}

    except Exception as e:
        logger.error(f"Feedback application failed: {e}", exc_info=True)
        return {}
