"""Image analysis node using vision model via OpenRouter."""
import asyncio
import logging
from typing import Any

from langchain_core.messages import HumanMessage

from models.state import ListingCreatorState
from services.openrouter import get_vision_llm

logger = logging.getLogger(__name__)

IMAGE_ANALYSIS_PROMPT = """Analysiere dieses Bild eines zum Verkauf stehenden Artikels auf Deutsch.

**Wichtigste Aufgabe: Identifiziere das genaue Produkt.**
- Erkenne Marke, Modellname und Modellnummer (z.B. "Wahoo TICKR X", "Garmin Edge 530", "Adidas Ultraboost 22")
- Nutze sichtbare Logos, Aufschriften, Formfaktoren und charakteristische Design-Merkmale zur Identifikation
- Falls das exakte Modell nicht eindeutig erkennbar ist, nenne trotzdem die wahrscheinlichste Variante ohne Unsicherheitshinweise

Strukturiere deine Antwort:
- **Produkt**: Exakter Markenname + Modell (so präzise wie möglich)
- **Zustand**: Neuwertig / Sehr gut erhalten / Gut erhalten / Gebraucht / Stark gebraucht
- **Besonderheiten**: Auffällige Features, Zubehör, sichtbare Mängel

Wichtig:
- Defektbegriffe nur wenn klar sichtbar oder in der Caption erwähnt
- Keine reine Farb-/Formbeschreibung wenn das Modell erkennbar ist

Diese Analyse wird für eine Kleinanzeige verwendet."""


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
        return {"image_analysis": "Keine Bilder vorhanden."}

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

        analysis = str(response.content).strip()
        logger.info(f"Image analysis completed: {len(analysis)} chars")
        return {"image_analysis": analysis}

    except Exception as e:
        logger.error(f"Image analysis failed: {e}", exc_info=True)
        return {"image_analysis": f"Bildanalyse fehlgeschlagen: {str(e)}"}
