"""Listing optimization node for the daily check workflow."""
import asyncio
import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from models.state import ListingOptimizerState
from services.openrouter import get_optimization_llm

logger = logging.getLogger(__name__)

OPTIMIZATION_SYSTEM_PROMPT = """Du bist ein Experte für Kleinanzeigen-Optimierung in Deutschland.
Deine Aufgabe: Überarbeite bestehende Anzeigen, damit sie mehr Aufmerksamkeit bekommen.

Optimierungsstrategien:
- Frischere, ansprechendere Formulierungen
- Bessere Keywords im Titel
- Leichte Preisreduzierung wenn angemessen (5-15%)
- Neue Aspekte in der Beschreibung hervorheben

WICHTIG – Was du NICHT tun darfst:
- Versand- oder Abholoptionen in der Beschreibung erwähnen (das wird separat in Kleinanzeigen-Einstellungen gesetzt)
- Informationen erfinden die nicht im Original stehen
- Den Zustand des Artikels beschönigen oder verändern. Mängel, Gebrauchsspuren, Defekte und Einschränkungen MÜSSEN exakt und vollständig aus dem Original übernommen werden. Wenn der Artikel einen platten Reifen hat, schreibe das. Wenn er Kratzer oder Gebrauchsspuren hat, schreibe das. NIEMALS "Top Zustand", "sehr gut erhalten", "einwandfrei" o.ä. verwenden wenn das Original Mängel erwähnt oder den Zustand anders beschreibt.

Die Anzeige läuft bald ab (wenige Tage verbleibend) – eine Optimierung erhöht die Chancen!

Antworte im JSON-Format:
{
  "title": "Neuer Titel (max 50 Zeichen)",
  "description": "Neue Beschreibung",
  "price": 0.00,
  "changes_summary": "Kurze Zusammenfassung der Änderungen auf Deutsch"
}"""


async def optimize_listing(state: ListingOptimizerState) -> dict[str, Any]:
    """Generate optimized version of an existing listing.

    Args:
        state: Current optimizer workflow state

    Returns:
        Updated state with suggested improvements
    """
    listing_id = state.get("listing_id", "unknown")
    logger.info(f"Optimizing listing {listing_id}")
    await asyncio.sleep(0.5)  # Rate limiting

    try:
        llm = get_optimization_llm(temperature=0.3)

        prompt = f"""Optimiere diese Kleinanzeige die in {state.get('days_remaining', '?')} Tagen abläuft:

Aktueller Titel: {state.get('current_title', '')}
Aktueller Preis: {state.get('current_price', 0)} EUR
Aktuelle Beschreibung (FAKTENBASIS – Zustand/Mängel dürfen nicht verändert werden):
{state.get('current_description', '')}"""

        messages = [
            SystemMessage(content=OPTIMIZATION_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        response = await llm.ainvoke(messages)
        content = str(response.content).strip()

        # Parse JSON response
        json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())

            suggested_title = str(data.get("title", state.get("current_title", "")))
            if len(suggested_title) > 50:
                suggested_title = suggested_title[:50].rsplit(" ", 1)[0]

            suggested_price = float(data.get("price", state.get("current_price", 0)))
            suggested_price = max(0.5, suggested_price)

            changes_summary = str(data.get("changes_summary", "Optimierungen vorgenommen"))

            logger.info(f"Optimization generated for listing {listing_id}")
            return {
                "suggested_title": suggested_title,
                "suggested_description": str(data.get("description", state.get("current_description", ""))),
                "suggested_price": round(suggested_price, 2),
                "changes_summary": changes_summary,
            }
        else:
            logger.warning(f"Could not parse optimization JSON for listing {listing_id}")
            return {
                "suggested_title": state.get("current_title", ""),
                "suggested_description": state.get("current_description", ""),
                "suggested_price": state.get("current_price", 0),
                "changes_summary": "Keine Optimierung möglich",
            }

    except Exception as e:
        logger.error(f"Listing optimization failed for {listing_id}: {e}", exc_info=True)
        return {
            "suggested_title": state.get("current_title", ""),
            "suggested_description": state.get("current_description", ""),
            "suggested_price": state.get("current_price", 0),
            "changes_summary": f"Fehler bei Optimierung: {str(e)}",
        }
