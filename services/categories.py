"""Kleinanzeigen category resolution.

The bot maps an ad's ``category`` to a numeric path via its bundled
``categories.yaml``. An unknown value is passed through verbatim into
``p-kategorie-aendern.html#?path=<value>``; the site then silently selects
nothing, the shipping section never renders and publishing dies with
"No HTML element found with ID 'ad-shipping-options'".

Downloaded ads carry the leaf name only (e.g. "Bücher & Zeitschriften"), so
every category we write into an ad YAML goes through :func:`resolve_category`.
"""
import logging
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

# Valid "Parent > Child" paths offered to the category LLM. Kept short on
# purpose; validated against categories.yaml by tests.
CATEGORY_LIST: list[str] = [
    "Elektronik > Handy & Telefon",
    "Elektronik > Tablets Reader",
    "Elektronik > Notebooks",
    "Elektronik > PC-Zubehör & Software",
    "Elektronik > TV & Video",
    "Elektronik > Audio & Hifi",
    "Elektronik > Foto",
    "Elektronik > Konsolen",
    "Elektronik > Videospiele",
    "Elektronik > Haushaltsgeräte",
    "Haus & Garten > Wohnzimmer",
    "Haus & Garten > Schlafzimmer",
    "Haus & Garten > Küche & Esszimmer",
    "Haus & Garten > Dekoration",
    "Haus & Garten > Lampen & Licht",
    "Haus & Garten > Heimwerken",
    "Haus & Garten > Gartenzubehör & Pflanzen",
    "Haus & Garten > Büro",
    "Mode & Beauty > Damenbekleidung",
    "Mode & Beauty > Herrenbekleidung",
    "Mode & Beauty > Damenschuhe",
    "Mode & Beauty > Herrenschuhe",
    "Mode & Beauty > Taschen & Accessoires",
    "Mode & Beauty > Beauty & Gesundheit",
    "Familie, Kind & Baby > Spielzeug",
    "Familie, Kind & Baby > Baby- & Kinderkleidung",
    "Familie, Kind & Baby > Baby- & Kinderschuhe",
    "Familie, Kind & Baby > Kinderwagen & Buggys",
    "Musik, Filme & Bücher > Bücher & Zeitschriften",
    "Musik, Filme & Bücher > Fachbücher, Schule & Studium",
    "Musik, Filme & Bücher > Comics",
    "Musik, Filme & Bücher > Musik & CDs",
    "Musik, Filme & Bücher > Film & DVD",
    "Musik, Filme & Bücher > Musikinstrumente",
    "Freizeit, Hobby & Nachbarschaft > Sport & Camping",
    "Freizeit, Hobby & Nachbarschaft > Sammeln",
    "Freizeit, Hobby & Nachbarschaft > Modellbau",
    "Freizeit, Hobby & Nachbarschaft > Handarbeit, Basteln & Kunsthandwerk",
    "Freizeit, Hobby & Nachbarschaft > Trödel",
    "Auto, Rad & Boot > Autoteile & Reifen",
    "Auto, Rad & Boot > Fahrräder & Zubehör",
]

# Catch-all for items that fit nowhere else.
DEFAULT_CATEGORY = "Freizeit, Hobby & Nachbarschaft > Trödel"


@lru_cache(maxsize=1)
def known_categories() -> dict[str, str]:
    """Return the bot's category alias -> numeric path mapping (current + deprecated)."""
    from kleinanzeigen_bot import resources as _resources
    from kleinanzeigen_bot.utils import dicts as _dicts

    categories: dict[str, str] = dict(
        _dicts.load_dict_from_module(_resources, "categories.yaml", "")
    )
    categories.update(_dicts.load_dict_from_module(_resources, "categories_old.yaml", ""))
    return categories


def resolve_category(value: Optional[str]) -> Optional[str]:
    """Map *value* onto a category alias the bot can resolve, or None.

    Accepts what the bot accepts (exact alias, or any path whose parent is a
    known alias) and additionally repairs leaf-only names as written by the
    bot's ad download.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None

    categories = known_categories()
    if candidate in categories:
        return candidate
    # The bot itself falls back to the parent path, so such values are fine.
    if ">" in candidate and candidate.rpartition(">")[0].strip() in categories:
        return candidate

    lowered = candidate.casefold()
    for alias in categories:
        if alias.casefold() == lowered:
            return alias

    # Leaf-only or partial path: match against the tail segments of known aliases.
    suffix = f"> {lowered}"
    matches = [
        alias for alias in categories
        if alias.casefold().endswith(suffix) and alias.count(">") == candidate.count(">") + 1
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def canonical_category(value: Optional[str], *, default: str = DEFAULT_CATEGORY) -> str:
    """Resolve *value*, falling back to *default* with a warning."""
    resolved = resolve_category(value)
    if resolved:
        if value and resolved != value.strip():
            logger.info("Category '%s' resolved to '%s'", value, resolved)
        return resolved
    if value:
        logger.warning("Category '%s' is unknown to the bot; using '%s'", value, default)
    return default
