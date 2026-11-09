"""Translate a Kleinanzeigen listing into a Vinted draft payload.

Vinted needs structured fields Kleinanzeigen does not have: a leaf ``catalog_id``
from its own category tree, a condition id, a package size, brand and (for
clothing) a size. An LLM picks catalog, brand, condition and size from title and
description; everything else is mapped deterministically here.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Global condition ids (dynamic item_attribute "condition", same in every market).
CONDITION_IDS: dict[str, int] = {
    "new_with_tags": 6,
    "new_without_tags": 1,
    "very_good": 2,
    "good": 3,
    "satisfactory": 4,
}
# Kleinanzeigen package size (bot's shipping_size) -> Vinted package_size_id.
PACKAGE_SIZE_IDS: dict[str, int] = {"S": 1, "M": 2, "L": 3}
KA_OPTION_TO_SIZE: dict[str, str] = {
    "DHL_2": "S", "Hermes_S": "S", "Hermes_Päckchen": "S",
    "DHL_5": "M", "Hermes_M": "M",
    "DHL_10": "L", "Hermes_L": "L", "DHL_20": "L", "DHL_31,5": "L",
}
TITLE_MAX = 100
DESCRIPTION_MAX = 2000
MAX_PHOTOS = 20
MIN_PRICE = 1.0
GROUP_CHOICES_MAX = 120
LEAF_CHOICES_MAX = 150


@dataclass
class VintedFields:
    """What the LLM decided for one listing."""

    catalog_id: Optional[int] = None
    catalog_path: str = ""
    brand: Optional[str] = None
    condition: str = "good"
    size: Optional[str] = None
    notes: list[str] = field(default_factory=list)


def shipping_size_from_listing(
    shipping_type: str, shipping_size: str = "", shipping_options: object = None
) -> str:
    """Return S/M/L, or PICKUP when the listing is not shipped."""
    if shipping_size in PACKAGE_SIZE_IDS:
        return shipping_size
    if (shipping_type or "").upper() != "SHIPPING":
        return "PICKUP"
    options: list[str] = []
    if isinstance(shipping_options, str) and shipping_options:
        try:
            parsed = json.loads(shipping_options)
            options = [str(o) for o in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            options = [o.strip() for o in shipping_options.split(",")]
    elif isinstance(shipping_options, list):
        options = [str(o) for o in shipping_options]
    for option in options:
        if option in KA_OPTION_TO_SIZE:
            return KA_OPTION_TO_SIZE[option]
    return "S"


def skip_reason(price: float, package_size: str) -> Optional[str]:
    """Why a listing cannot go to Vinted at all, or None."""
    if package_size not in PACKAGE_SIZE_IDS:
        return "nur Abholung (Vinted verkauft nur mit Versand)"
    if price < MIN_PRICE:
        return "zu verschenken (Vinted braucht einen Preis)"
    return None


def vinted_price(price: float, factor: float = 1.0) -> float:
    return float(max(MIN_PRICE, round(price * factor)))


# Kleinanzeigen-only wording that is wrong on Vinted (no negotiation flag, no pickup,
# Vinted's buyer protection replaces "Privatverkauf, keine Rücknahme").
_KA_ONLY_LINE_RE = re.compile(
    r"^\s*(?:.*\b(?:abholung|selbstabholung|abzuholen|vb\b|verhandlungsbasis|"
    r"paypal|überweisung|kleinanzeigen)\b.*)$",
    re.IGNORECASE,
)


def adapt_description(description: str) -> str:
    lines = [line for line in (description or "").splitlines() if not _KA_ONLY_LINE_RE.match(line)]
    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > DESCRIPTION_MAX:
        text = text[: DESCRIPTION_MAX - 1].rstrip() + "…"
    return text or (description or "")[:DESCRIPTION_MAX]


def adapt_title(title: str) -> str:
    text = re.sub(r"^\s*reserviert[\s:\-–]*", "", title or "", flags=re.IGNORECASE).strip()
    return text[:TITLE_MAX]


def flatten_catalogs(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Leaves of the Vinted catalog tree as {id, title, path}."""
    leaves: list[dict[str, Any]] = []

    def walk(nodes: list[dict[str, Any]], trail: list[str]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "")
            children = node.get("catalogs") or []
            path = trail + [title]
            if children:
                walk(children, path)
            elif isinstance(node.get("id"), int):
                leaves.append({"id": node["id"], "title": title, "path": " > ".join(path)})

    walk(tree, [])
    return leaves


def group_leaves(leaves: list[dict[str, Any]], depth: int = 2) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for leaf in leaves:
        key = " > ".join(leaf["path"].split(" > ")[:depth])
        groups.setdefault(key, []).append(leaf)
    return groups


def parse_llm_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of an LLM answer (tolerates code fences)."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _listing_text(title: str, description: str, ka_category: str) -> str:
    return (
        f"Titel: {title}\nKleinanzeigen-Kategorie: {ka_category or '-'}\n"
        f"Beschreibung:\n{(description or '')[:1500]}"
    )


async def choose_vinted_fields(
    llm: Any, *, title: str, description: str, ka_category: str, leaves: list[dict[str, Any]]
) -> VintedFields:
    """Two LLM steps: pick a category group, then the leaf plus brand/condition/size."""
    from langchain_core.messages import HumanMessage, SystemMessage

    fields = VintedFields()
    groups = group_leaves(leaves)
    group_names = list(groups)[:GROUP_CHOICES_MAX]
    listing = _listing_text(title, description, ka_category)

    answer = await llm.ainvoke([
        SystemMessage(content=(
            "Du ordnest Kleinanzeigen-Artikel in Vinted-Kategorien ein. Antworte nur mit JSON "
            '{"group": "<exakt ein Eintrag der Liste oder null>"}. null, wenn auf Vinted '
            "keine Gruppe passt (z.B. Möbel, Fahrzeuge, Lebensmittel, Tiere)."
        )),
        HumanMessage(content=listing + "\n\nGruppen:\n" + "\n".join(group_names)),
    ])
    group = parse_llm_json(str(getattr(answer, "content", answer))).get("group")
    if not isinstance(group, str) or group not in groups:
        fields.notes.append("keine passende Vinted-Kategorie")
        return fields

    candidates = groups[group][:LEAF_CHOICES_MAX]
    by_id = {leaf["id"]: leaf for leaf in candidates}
    answer = await llm.ainvoke([
        SystemMessage(content=(
            "Du füllst ein Vinted-Inserat aus. Antworte nur mit JSON: "
            '{"catalog_id": <id aus der Liste>, "brand": "<Marke oder null>", '
            '"condition": "new_with_tags|new_without_tags|very_good|good|satisfactory", '
            '"size": "<Größe wie auf dem Etikett, z.B. M, 42, 38/40, oder null>"}. '
            "Marke nur, wenn sie im Text steht. Zustand aus der Beschreibung, im Zweifel good. "
            "Größe nur für Kleidung und Schuhe."
        )),
        HumanMessage(content=listing + "\n\nKategorien (id: Pfad):\n"
                     + "\n".join(f"{leaf['id']}: {leaf['path']}" for leaf in candidates)),
    ])
    data = parse_llm_json(str(getattr(answer, "content", answer)))
    try:
        catalog_id = int(data.get("catalog_id"))
    except (TypeError, ValueError):
        catalog_id = None
    if catalog_id not in by_id:
        fields.notes.append("Vinted-Kategorie nicht erkannt")
        return fields
    fields.catalog_id = catalog_id
    fields.catalog_path = by_id[catalog_id]["path"]
    brand = data.get("brand")
    fields.brand = brand.strip() if isinstance(brand, str) and brand.strip() and brand.lower() != "null" else None
    condition = data.get("condition")
    fields.condition = condition if condition in CONDITION_IDS else "good"
    size = data.get("size")
    fields.size = size.strip() if isinstance(size, str) and size.strip() and size.lower() != "null" else None
    return fields


def pick_brand(brands: list[dict[str, Any]], wanted: str) -> tuple[Optional[int], Optional[str]]:
    """Exact (case-insensitive) title match from /brands, else no brand id."""
    wanted_norm = wanted.strip().lower()
    for brand in brands:
        title = str(brand.get("title") or "")
        if title.lower() == wanted_norm and isinstance(brand.get("id"), int):
            return brand["id"], title
    return None, wanted.strip() or None


def pick_size(size_groups: list[dict[str, Any]], wanted: str) -> Optional[int]:
    wanted_norm = wanted.strip().lower().replace(" ", "")
    for group in size_groups:
        for size in group.get("sizes") or []:
            title = str(size.get("title") or "").lower().replace(" ", "")
            if title == wanted_norm and isinstance(size.get("id"), int):
                return size["id"]
    return None


def build_draft(
    *,
    temp_uuid: str,
    title: str,
    description: str,
    price: float,
    currency: str,
    catalog_id: int,
    package_size: str,
    photo_ids: list[int],
    condition: str = "good",
    brand_id: Optional[int] = None,
    brand: Optional[str] = None,
    size_id: Optional[int] = None,
) -> dict[str, Any]:
    """The ``draft`` object in the shape of the captured web-app request."""
    item_attributes: list[dict[str, Any]] = [
        {"code": "condition", "ids": [CONDITION_IDS.get(condition, CONDITION_IDS["good"])]}
    ]
    if size_id is not None:
        item_attributes.append({"code": "size", "ids": [size_id]})
    return {
        "id": None,
        "currency": currency,
        "temp_uuid": temp_uuid,
        "title": adapt_title(title),
        "description": adapt_description(description),
        "brand_id": brand_id,
        "brand": brand,
        "catalog_id": catalog_id,
        "isbn": None,
        "is_unisex": False,
        "ai_photo": False,
        "price": price,
        "package_size_id": PACKAGE_SIZE_IDS[package_size],
        "shipment_prices": {"domestic": None, "international": None},
        "color_ids": [],
        "assigned_photos": [{"id": pid, "orientation": 0} for pid in photo_ids[:MAX_PHOTOS]],
        "measurement_length": None,
        "measurement_width": None,
        "item_attributes": item_attributes,
        "manufacturer": None,
        "manufacturer_labelling": None,
    }


def item_state(item: dict[str, Any]) -> str:
    """sold | reserved | hidden | draft | active for an item of the wardrobe API.

    The exact flags were not verified against a live account (docs/vinted.md); the
    check accepts every variant seen in the web app. /vinted prints the raw flags of
    the first item so this can be confirmed.
    """
    closing = str(item.get("item_closing_action") or "").lower()
    status = str(item.get("status") or "").lower()
    if closing == "sold" or item.get("is_sold") or status in ("sold", "verkauft"):
        return "sold"
    if item.get("is_closed") and closing not in ("deleted", "removed"):
        return "sold"
    if item.get("is_reserved"):
        return "reserved"
    if item.get("is_draft"):
        return "draft"
    if item.get("is_hidden"):
        return "hidden"
    return "active"


STATE_FLAG_KEYS = (
    "id", "is_closed", "is_sold", "item_closing_action", "status", "is_reserved",
    "is_hidden", "is_draft", "is_processing", "can_edit",
)


def item_flags(item: dict[str, Any]) -> dict[str, Any]:
    return {k: item.get(k) for k in STATE_FLAG_KEYS if k in item}
