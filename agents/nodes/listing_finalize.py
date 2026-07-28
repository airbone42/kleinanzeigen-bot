"""Final listing validation and formatting node."""
import logging
import re
from typing import Any

from models.state import ListingCreatorState

logger = logging.getLogger(__name__)

_CONDITION_MAP = {
    "stark gebraucht": "Stark gebrauchter",
    "gebraucht": "Gebrauchter",
    "gut erhalten": "Gut erhaltener",
    "sehr gut": "Sehr gut erhaltener",
    "neuwertig": "Neuwertiger",
}

_DEFECT_HINTS = [
    "defekt",
    "kaputt",
    "beschädigt",
    "funktionsunfähig",
    "nicht funktionsfähig",
    "geht nicht",
    "ohne funktion",
]

_UNCERTAINTY_PATTERNS: list[tuple[str, str]] = [
    (r"\bscheint\s+", ""),
    (r"\bwirkt\s+", ""),
    (r"\bvermutlich\b", ""),
    (r"\bwahrscheinlich\b", ""),
    (r"\banscheinend\b", ""),
    (r"\boffenbar\b", ""),
    (r"\bwohl\b", ""),
]

_IMAGE_OBSERVER_PATTERNS = [
    r"\bauf (?:dem|den) bild(?:ern)?\b",
    r"\bzu sehen\b",
    r"\berkennbar\b",
    r"\bauf der verpackung\b",
    r"\bvermerkt\b",
    r"\bman sieht\b",
    r"\bwie vom verkäufer beschrieben\b",
    r"\blaut verkäufer\b",
]


def _caption_allows_defect(caption: str) -> bool:
    """Return True if seller caption explicitly states a defect."""
    text = caption.lower()
    return any(hint in text for hint in _DEFECT_HINTS)


def _strip_defect_terms(text: str, replacement: str) -> str:
    """Remove defect terminology when seller did not explicitly mark defects."""
    patterns = [
        r"\bdefekt(?:e[nrsm]?)?\b",
        r"\bkaputt(?:e[nrsm]?)?\b",
        r"\bbeschädigt(?:e[nrsm]?)?\b",
        r"\bnicht\s+funktionsfähig\b",
        r"\bfunktionsunfähig\b",
        r"\bohne\s+funktion\b",
        r"\bgeht\s+nicht\b",
    ]
    result = text
    for pattern in patterns:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", result).strip()


def _extract_condition(image_analysis: str) -> str:
    """Extract condition label from image analysis text."""
    text = image_analysis.lower()
    # Check from most specific to least specific
    for key, label in _CONDITION_MAP.items():
        if key in text:
            return label
    return "Gut erhaltener"


def _strip_uncertainty_language(text: str) -> str:
    """Remove hedging language so listing statements stay direct."""
    result = text
    for pattern, replacement in _UNCERTAINTY_PATTERNS:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    result = re.sub(r"\s{2,}", " ", result)
    return re.sub(r"\s+([,.;:!?])", r"\1", result).strip()


def _drop_image_observer_sentences(text: str) -> str:
    """Drop sentences that describe image observations instead of ad facts."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept: list[str] = []
    for sentence in sentences:
        lower = sentence.lower()
        if any(re.search(pattern, lower) for pattern in _IMAGE_OBSERVER_PATTERNS):
            continue
        kept.append(sentence)
    return " ".join(kept).strip()


async def finalize_listing(state: ListingCreatorState) -> dict[str, Any]:
    """Finalize and validate the listing before returning it.

    Ensures all required fields are present and properly formatted.

    Args:
        state: Current workflow state

    Returns:
        Validated state ready for publishing
    """
    title = state.get("title", "").strip()
    description = state.get("description", "").strip()
    price = state.get("price", 0.0)
    caption = state.get("caption", "")

    issues = []

    if not title:
        issues.append("Kein Titel generiert")
        title = "Artikel zu verkaufen"

    if len(title) > 50:
        title = title[:50].rsplit(" ", 1)[0]
        logger.warning(f"Title truncated to 50 chars: '{title}'")

    if not description:
        issues.append("Keine Beschreibung generiert")
        image_analysis = state.get("image_analysis", "")
        condition = _extract_condition(image_analysis)
        description = f"{condition} Artikel abzugeben."

    normalized_description = _strip_uncertainty_language(description)
    if normalized_description != description:
        issues.append("Unsichere Formulierungen entfernt")
        description = normalized_description

    filtered_description = _drop_image_observer_sentences(description)
    if filtered_description != description:
        issues.append("Bildbeobachtungs-Saetze entfernt")
        description = filtered_description or "Gut erhaltener Artikel abzugeben."

    if not _caption_allows_defect(caption):
        clean_title = _strip_defect_terms(title, "gebraucht")
        clean_description = _strip_defect_terms(description, "gebraucht")
        if clean_title != title or clean_description != description:
            issues.append("Defekt-Terminologie ohne Caption-Hinweis entfernt")
            title = clean_title
            description = clean_description
        if not title:
            title = "Artikel zu verkaufen"

    if price <= 0:
        issues.append("Ungültiger Preis")
        price = 1.0

    if issues:
        logger.warning(f"Draft issues found and fixed: {issues}")

    return {
        "title": title,
        "description": description,
        "price": round(price, 2),
        "status": "draft",
    }
