"""Image analysis node using vision model via OpenRouter."""
import asyncio
import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage

from models.state import ListingCreatorState
from services.openrouter import get_vision_llm

logger = logging.getLogger(__name__)

IMAGE_ANALYSIS_PROMPT = """Analysiere diese Bilder eines zum Verkauf stehenden Artikels auf Deutsch.

**Wichtigste Aufgabe: Identifiziere das genaue Produkt.**
- Lies zuerst allen sichtbaren Text: Aufdrucke, Typenschilder, Etiketten, Verpackung, Logos, Modellnummern
  (auch auf Unterseiten und Rückseiten, mehrere Bilder zeigen oft dasselbe Gerät)
- Erkenne Marke, Modellname und Modellnummer (z.B. "Wahoo TICKR X", "Garmin Edge 530", "Ubiquiti UniFi Security Gateway USG")
- Nutze Logos, Formfaktoren und Design-Merkmale zur Identifikation
- Erfinde nichts. Ist Marke oder Modell nicht sicher erkennbar, sag das ehrlich über das Feld "sicherheit"

Antworte NUR mit JSON in genau dieser Form:
{
  "produkt": "Marke + Modell, so präzise wie belegbar; leer, wenn nicht erkennbar",
  "zustand": "Neuwertig | Sehr gut erhalten | Gut erhalten | Gebraucht | Stark gebraucht",
  "besonderheiten": "Zubehör, Features, sichtbare Mängel, relevante Aufdrucke (Modellnummer, Spannung, ...)",
  "sicherheit": "hoch | mittel | niedrig",
  "rueckfragen": ["konkrete Frage an den Verkäufer, nur wenn sicherheit nicht hoch"]
}

Regeln für "sicherheit":
- hoch: Marke und Modell sind durch Text oder eindeutiges Design belegt
- mittel: Produkttyp und Marke klar, Modell/Variante geraten
- niedrig: Artikel nur grob erkennbar oder gar nicht (Bild unscharf, zu dunkel, Detail fehlt)

Defektbegriffe nur, wenn klar sichtbar oder in der Caption erwähnt.
Diese Analyse wird für eine Kleinanzeige verwendet."""

_GENERIC_PRODUCTS = {"", "unbekannt", "unbekannter artikel", "artikel", "gegenstand", "produkt", "objekt", "?"}
DEFAULT_QUESTION = "Was genau ist das für ein Artikel (Marke, Modell)?"


def _parse_analysis(raw: str) -> dict[str, Any] | None:
    """Extract the JSON object from the model reply, None if unusable."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _build_result(raw: str) -> dict[str, Any]:
    """Turn the raw model reply into image_analysis text plus optional needs_info."""
    data = _parse_analysis(raw)
    if data is None:
        # Free text without JSON: usable only if there is substantial content.
        if len(raw.strip()) < 20:
            return {"image_analysis": "", "needs_info": DEFAULT_QUESTION}
        return {"image_analysis": raw.strip(), "needs_info": ""}

    produkt = str(data.get("produkt", "")).strip()
    zustand = str(data.get("zustand", "")).strip()
    besonderheiten = str(data.get("besonderheiten", "")).strip()
    sicherheit = str(data.get("sicherheit", "")).strip().lower()
    fragen = [str(q).strip() for q in data.get("rueckfragen", []) if str(q).strip()]

    if produkt.lower() in _GENERIC_PRODUCTS or sicherheit == "niedrig":
        return {
            "image_analysis": "",
            "needs_info": "\n".join(f"• {q}" for q in fragen) or f"• {DEFAULT_QUESTION}",
        }

    analysis = f"**Produkt**: {produkt}\n**Zustand**: {zustand}\n**Besonderheiten**: {besonderheiten}"
    if sicherheit == "mittel":
        analysis += "\n**Hinweis**: Modell/Variante nicht eindeutig belegt, nur belegbare Angaben verwenden."
    return {"image_analysis": analysis, "needs_info": ""}


async def analyze_images(state: ListingCreatorState) -> dict[str, Any]:
    """Analyze uploaded images using vision model.

    Args:
        state: Current workflow state with base64-encoded image strings

    Returns:
        Updated state dict with image_analysis field
    """
    images = state.get("images", [])
    if not images:
        logger.warning("No images provided for analysis")
        return {"image_analysis": "Keine Bilder vorhanden.", "needs_info": "• Bitte Fotos des Artikels schicken."}

    logger.info(f"Analyzing {len(images)} image(s)")

    # Rate limiting between API calls
    await asyncio.sleep(0.5)

    try:
        llm = get_vision_llm(temperature=0.3)

        # Build content with all images
        caption = state.get("caption", "").strip()
        caption_hint = (
            f"\n\nVerkäufer-Caption: {caption}\n"
            "Nutze Defekt-Begriffe nur, wenn die Caption das klar sagt."
            if caption else
            "\n\nEs gibt keine Verkäufer-Caption. Nutze keine Defekt-Begriffe."
        )
        content: list[dict[str, Any]] = [
            {"type": "text", "text": IMAGE_ANALYSIS_PROMPT + caption_hint}
        ]

        for i, b64_image in enumerate(images[:10]):  # Max 10 images, already base64-encoded
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                }
            )
            logger.debug(f"Added image {i + 1}/{len(images)} to analysis request")

        message = HumanMessage(content=content)  # type: ignore[arg-type]
        response = await llm.ainvoke([message])

        result = _build_result(str(response.content))
        logger.info(
            f"Image analysis completed: {len(result['image_analysis'])} chars, "
            f"needs_info={bool(result['needs_info'])}"
        )
        return result

    except Exception as e:
        logger.error(f"Image analysis failed: {e}", exc_info=True)
        # Do not pass error text on as analysis, ask the seller instead of guessing.
        return {"image_analysis": "", "needs_info": DEFAULT_QUESTION}
