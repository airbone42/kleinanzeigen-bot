"""Discogs inventory sync service for mainstream high-value releases."""
import asyncio
import logging
import re
import statistics
from dataclasses import dataclass
from typing import Any, Optional

import discogs_client
import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from config.settings import settings
from db.repository import discogs_mapping_repo
from models.draft import Draft
from models.listing import Listing
from services.kleinanzeigen import KleinanzeigenService
from services.openrouter import get_text_llm

logger = logging.getLogger(__name__)

DISCOGS_API_BASE = "https://api.discogs.com"
DISCOGS_REQ_DELAY_SECONDS = 1.1
TREASURE_MEDIAN_MIN_EUR = 50.0
DISCOGS_DEFAULT_CATEGORY = "Musik, Filme & Bücher > Musik & CDs"


@dataclass(slots=True)
class DiscogsInventoryItem:
    """Normalized Discogs inventory item."""

    inventory_id: str
    release_id: str
    artist: str
    title: str
    label: str
    year: Optional[int]
    media_condition: str
    sleeve_condition: str
    price_eur: float
    median_price_eur: float
    image_url: str
    is_mainstream: bool = False


def inventory_item_to_dict(item: DiscogsInventoryItem) -> dict[str, object]:
    """Convert item to state-safe dict."""
    return {
        "inventory_id": item.inventory_id,
        "release_id": item.release_id,
        "artist": item.artist,
        "title": item.title,
        "label": item.label,
        "year": item.year,
        "media_condition": item.media_condition,
        "sleeve_condition": item.sleeve_condition,
        "price_eur": item.price_eur,
        "median_price_eur": item.median_price_eur,
        "image_url": item.image_url,
        "is_mainstream": item.is_mainstream,
    }


def inventory_item_from_dict(data: dict[str, object]) -> DiscogsInventoryItem:
    """Create typed item from state dict."""
    raw_year = data.get("year")
    year = int(raw_year) if isinstance(raw_year, int) else None
    return DiscogsInventoryItem(
        inventory_id=str(data.get("inventory_id", "")),
        release_id=str(data.get("release_id", "")),
        artist=str(data.get("artist", "")),
        title=str(data.get("title", "")),
        label=str(data.get("label", "")),
        year=year,
        media_condition=str(data.get("media_condition", "")),
        sleeve_condition=str(data.get("sleeve_condition", "")),
        price_eur=_parse_price(data.get("price_eur")),
        median_price_eur=_parse_price(data.get("median_price_eur")),
        image_url=str(data.get("image_url", "")),
        is_mainstream=bool(data.get("is_mainstream", False)),
    )


def _sanitize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _parse_price(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        if "value" in value:
            return _parse_price(value.get("value"))
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return 0.0


def _first_label(labels: object) -> str:
    if isinstance(labels, str):
        return _sanitize(labels)
    if isinstance(labels, list) and labels:
        first = labels[0]
        if isinstance(first, dict):
            return _sanitize(str(first.get("name", "")))
        return _sanitize(str(first))
    return ""


def _extract_artist(release_data: dict[str, object]) -> str:
    direct_artist = release_data.get("artist")
    if isinstance(direct_artist, str) and direct_artist.strip():
        return _sanitize(direct_artist)
    artists = release_data.get("artists")
    if isinstance(artists, list) and artists:
        first = artists[0]
        if isinstance(first, dict):
            return _sanitize(str(first.get("name", "")))
        return _sanitize(str(first))
    return ""


def _extract_image_url(release_data: dict[str, object]) -> str:
    images = release_data.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            uri = str(first.get("uri", "")).strip()
            if uri:
                return uri
    thumb = str(release_data.get("thumb", "")).strip() or str(release_data.get("thumbnail", "")).strip()
    if thumb:
        return thumb
    return ""


def build_discogs_description(item: DiscogsInventoryItem) -> str:
    """Build technical seller-style description for Discogs sync listing."""
    year_line = f"Jahr: {item.year}" if item.year else "Jahr: unbekannt"
    lines = [
        f"Artist: {item.artist}",
        f"Titel: {item.title}",
        f"Label: {item.label or '-'}",
        year_line,
        f"Zustand Vinyl: {item.media_condition or '-'}",
        f"Zustand Cover: {item.sleeve_condition or '-'}",
        "Sorgfältig gelagert.",
    ]
    return "\n".join(lines)


def _map_condition_to_special_attributes(media_condition: str) -> dict[str, str]:
    """Map Discogs media condition to Kleinanzeigen condition_s."""
    text = (media_condition or "").lower()
    if "mint" in text or "near mint" in text or "nm" in text:
        return {"condition_s": "like_new"}
    if "very good" in text or "vg+" in text or "vg" in text:
        return {"condition_s": "used"}
    if "good plus" in text or "good" in text or "fair" in text or "poor" in text:
        return {"condition_s": "damaged"}
    return {"condition_s": "used"}


def build_discogs_title(item: DiscogsInventoryItem) -> str:
    """Build compact Kleinanzeigen title with key tokens."""
    title = _sanitize(f"{item.artist} {item.title}")
    if item.year:
        title = _sanitize(f"{title} {item.year}")
    if item.label:
        title = _sanitize(f"{title} {item.label}")
    return title[:50]


def _extract_listing_id_from_url(url: str) -> str:
    match = re.search(r"/(\d{7,12})(?:$|\D)", url)
    return match.group(1) if match else ""


def _listing_url_for_id(listing_id: str) -> str:
    if not listing_id:
        return ""
    return f"https://www.kleinanzeigen.de/s-anzeige/id/{listing_id}"


class DiscogsSyncService:
    """Synchronizes Discogs inventory treasures to Kleinanzeigen."""

    def __init__(self) -> None:
        self.kleinanzeigen_service = KleinanzeigenService()
        self._mainstream_cache: dict[str, bool] = self._load_mainstream_cache()

    def _cache_path(self) -> "Path":
        from pathlib import Path
        from config.settings import settings
        return settings.kleinanzeigen_config_path / "discogs_mainstream_cache.json"

    def _load_mainstream_cache(self) -> dict[str, bool]:
        import json
        try:
            p = self._cache_path()
            if p.exists():
                return {k: bool(v) for k, v in json.loads(p.read_text()).items()}
        except Exception:
            pass
        return {}

    def _save_mainstream_cache(self) -> None:
        import json
        try:
            self._cache_path().write_text(json.dumps(self._mainstream_cache, ensure_ascii=False, indent=2))
        except Exception as exc:
            logger.warning("Could not save mainstream cache: %s", exc)

    def _build_client(self) -> discogs_client.Client:
        if not settings.discogs_user_token:
            raise ValueError("DISCOGS_USER_TOKEN fehlt")
        return discogs_client.Client(
            "kleinanzeigen-telegram-bot/0.1",
            user_token=settings.discogs_user_token,
        )

    async def get_inventory_items(self) -> list[DiscogsInventoryItem]:
        """Fetch inventory items from Discogs user inventory."""
        username = settings.discogs_username.strip()
        if not username:
            raise ValueError("DISCOGS_USERNAME fehlt")

        logger.info("Discogs inventory fetch started for user '%s'", username)
        client = self._build_client()
        inventory = await asyncio.wait_for(
            asyncio.to_thread(lambda: list(client.user(username).inventory)),
            timeout=300,
        )
        items: list[DiscogsInventoryItem] = []

        for idx, entry in enumerate(inventory, start=1):
            entry_data_raw = getattr(entry, "data", {})
            if not isinstance(entry_data_raw, dict):
                continue
            entry_data: dict[str, Any] = entry_data_raw
            release_data_raw = entry_data.get("release", {})
            release_data = release_data_raw if isinstance(release_data_raw, dict) else {}
            release_id = str(release_data.get("id", "")).strip()
            if not release_id:
                continue

            price_obj = entry_data.get("price", {})
            price_eur = _parse_price(price_obj)
            artist = _extract_artist(release_data)
            title = _sanitize(str(release_data.get("title", "")))
            label = _first_label(release_data.get("labels") or release_data.get("label"))
            is_mainstream = await self._is_mainstream_release(
                artist=artist,
                title=title,
                label=label,
            )
            median_price = 0.0
            if is_mainstream:
                try:
                    # Rate-limit Discogs API requests for candidate releases.
                    median_price = await self._fetch_release_median_price(release_id)
                    await asyncio.sleep(DISCOGS_REQ_DELAY_SECONDS)
                except Exception as exc:
                    logger.warning(
                        "Discogs median lookup failed for release %s: %s",
                        release_id,
                        exc,
                    )
                    median_price = 0.0
                if median_price <= 0:
                    # Fallback when Discogs does not provide median/price suggestions.
                    median_price = price_eur

            item = DiscogsInventoryItem(
                inventory_id=str(entry_data.get("id", "")).strip(),
                release_id=release_id,
                artist=artist,
                title=title,
                label=label,
                year=int(release_data.get("year")) if str(release_data.get("year", "")).isdigit() else None,
                media_condition=_sanitize(str(entry_data.get("condition", ""))),
                sleeve_condition=_sanitize(str(entry_data.get("sleeve_condition", ""))),
                price_eur=price_eur,
                median_price_eur=median_price,
                image_url=_extract_image_url(release_data),
                is_mainstream=is_mainstream,
            )
            items.append(item)
            # Yield to the event loop after each entry so the Telegram dispatcher
            # can process queued updates (e.g. button callbacks) without waiting
            # for the full LLM-heavy inventory loop to complete.
            await asyncio.sleep(0)
            if idx % 10 == 0:
                logger.info(
                    "Discogs inventory progress: %s/%s entries processed",
                    idx,
                    len(inventory),
                )

        logger.info("Discogs inventory fetch finished: %s entries", len(items))
        return items

    async def _is_mainstream_release(self, *, artist: str, title: str, label: str) -> bool:
        """Classify if a release is mainstream enough for resale strategy."""
        artist_norm = (artist or "").strip().lower()
        if artist_norm and artist_norm not in {"various", "various artists", "va"}:
            key = f"artist:{artist_norm}"
        else:
            key = f"release:{artist_norm}|{(title or '').strip().lower()}|{(label or '').strip().lower()}"
        if not key or key == "||":
            return False
        cached = self._mainstream_cache.get(key)
        if cached is not None:
            return cached
        try:
            llm = get_text_llm(temperature=0.0)
            messages = [
                SystemMessage(
                    content=(
                        "Du klassifizierst Musik-Releases als MAINSTREAM oder NICHT_MAINSTREAM "
                        "fuer den Weiterverkauf auf Kleinanzeigen. "
                        "MAINSTREAM umfasst bekannte breite Acts/Compilations "
                        "(z.B. Fettes Brot, Cocoon Compilation, Die Aerzte, Kraftwerk). "
                        "Antworte exakt mit JA oder NEIN."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Artist: {artist or '-'}\n"
                        f"Titel: {title or '-'}\n"
                        f"Label: {label or '-'}"
                    )
                ),
            ]
            resp = await asyncio.wait_for(llm.ainvoke(messages), timeout=12)
            answer = str(resp.content).strip().lower()
            is_mainstream = answer.startswith("ja")
            self._mainstream_cache[key] = is_mainstream
            self._save_mainstream_cache()
            return is_mainstream
        except Exception as exc:
            logger.warning("Mainstream LLM classification failed for '%s': %s", key, exc)
            return False

    async def _fetch_release_median_price(self, release_id: str) -> float:
        """Fetch Discogs marketplace median price for one release."""
        headers = {
            "Authorization": f"Discogs token={settings.discogs_user_token}",
            "User-Agent": "kleinanzeigen-telegram-bot/0.1",
        }
        url = f"{DISCOGS_API_BASE}/marketplace/stats/{release_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, headers=headers)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    logger.warning("Discogs stats rate-limited for release %s", release_id)
                    return 0.0
                raise
            data = response.json()
        median = data.get("median_price") if isinstance(data, dict) else 0
        parsed_median = _parse_price(median.get("value")) if isinstance(median, dict) else _parse_price(median)
        if parsed_median > 0:
            return parsed_median

        # Discogs API often omits median_price in stats; derive robust fallback
        # from marketplace price suggestions.
        suggestions_url = f"{DISCOGS_API_BASE}/marketplace/price_suggestions/{release_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            suggestions_response = await client.get(suggestions_url, headers=headers)
            try:
                suggestions_response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    logger.warning("Discogs price_suggestions rate-limited for release %s", release_id)
                    return 0.0
                raise
            suggestions_data = suggestions_response.json()

        if isinstance(suggestions_data, dict):
            suggestion_values = [
                _parse_price(value.get("value"))
                for value in suggestions_data.values()
                if isinstance(value, dict)
            ]
            suggestion_values = [value for value in suggestion_values if value > 0]
            if suggestion_values:
                return float(statistics.median(suggestion_values))

        lowest = data.get("lowest_price") if isinstance(data, dict) else 0
        if isinstance(lowest, dict):
            return _parse_price(lowest.get("value"))
        return _parse_price(lowest)

    def filter_treasures(self, items: list[DiscogsInventoryItem]) -> list[DiscogsInventoryItem]:
        """Return only mainstream treasures (median > 50 EUR)."""
        return [
            item
            for item in items
            if item.median_price_eur > TREASURE_MEDIAN_MIN_EUR
            and item.is_mainstream
        ]

    async def _download_image_bytes(self, image_url: str) -> list[bytes]:
        if not image_url:
            return []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(image_url)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "image" not in content_type.lower() and not response.content:
                    return []
                return [response.content]
        except Exception as exc:
            logger.warning("Discogs image download failed (%s): %s", image_url, exc)
            return []

    async def _resolve_listing_id_by_metadata(
        self,
        artist: str,
        title: str,
        active_listings: list[Listing],
    ) -> Optional[str]:
        artist_l = artist.lower().strip()
        title_l = title.lower().strip()
        for listing in active_listings:
            hay = f"{listing.title}\n{listing.description}".lower()
            if artist_l and artist_l not in hay:
                continue
            if title_l and title_l not in hay:
                continue
            if artist_l or title_l:
                return listing.listing_id
        return None

    async def sync(
        self,
        *,
        chat_id: int,
        inventory_items: list[DiscogsInventoryItem],
    ) -> dict[str, object]:
        """Sync filtered Discogs inventory with Kleinanzeigen listings."""
        treasures = self.filter_treasures(inventory_items)
        active_mappings = await discogs_mapping_repo.get_active_by_chat(chat_id)
        mapping_by_release = {str(row["discogs_release_id"]): row for row in active_mappings}

        # Sync does not require a remote download; cached YAMLs are sufficient.
        active_listings = await self.kleinanzeigen_service.get_active_listings(refresh=False)
        active_ids = {listing.listing_id for listing in active_listings}

        updated_mappings = 0
        deleted: list[dict[str, str]] = []
        published: list[dict[str, str]] = []
        errors: list[str] = []

        # Reconcile mapping IDs in case renewal produced a different target listing id.
        for release_id, row in mapping_by_release.items():
            current_listing_id = str(row["kleinanzeigen_listing_id"] or "")
            if current_listing_id and current_listing_id in active_ids:
                continue
            resolved = await self._resolve_listing_id_by_metadata(
                str(row["artist"] or ""),
                str(row["title"] or ""),
                active_listings,
            )
            if resolved and resolved != current_listing_id:
                await discogs_mapping_repo.upsert_mapping(
                    chat_id=chat_id,
                    discogs_release_id=release_id,
                    discogs_inventory_id=str(row["discogs_inventory_id"] or ""),
                    artist=str(row["artist"] or ""),
                    title=str(row["title"] or ""),
                    label=str(row["label"] or ""),
                    year=int(row["year"]) if row["year"] is not None else None,
                    media_condition=str(row["media_condition"] or ""),
                    discogs_price=float(row["discogs_price"] or 0.0),
                    median_price=float(row["median_price"] or 0.0),
                    image_url=str(row["image_url"] or ""),
                    kleinanzeigen_listing_id=resolved,
                    kleinanzeigen_listing_url=_listing_url_for_id(resolved),
                    status="active",
                )
                updated_mappings += 1

        inventory_release_ids = {item.release_id for item in inventory_items}

        # Delete listings for releases removed from Discogs inventory.
        for release_id, row in mapping_by_release.items():
            if release_id in inventory_release_ids:
                continue
            listing_id = str(row["kleinanzeigen_listing_id"] or "")
            if not listing_id or listing_id not in active_ids:
                resolved = await self._resolve_listing_id_by_metadata(
                    str(row["artist"] or ""),
                    str(row["title"] or ""),
                    active_listings,
                )
                if resolved:
                    listing_id = resolved
            if listing_id:
                try:
                    await self.kleinanzeigen_service.delete_listing(listing_id)
                except Exception as exc:
                    errors.append(f"Delete fehlgeschlagen ({release_id}): {exc}")
                    continue
            await discogs_mapping_repo.mark_deleted(chat_id, release_id)
            deleted.append({
                "release_id": release_id,
                "listing_id": listing_id,
            })

        # Publish new treasures.
        for item in treasures:
            if item.release_id in mapping_by_release:
                await discogs_mapping_repo.upsert_mapping(
                    chat_id=chat_id,
                    discogs_release_id=item.release_id,
                    discogs_inventory_id=item.inventory_id,
                    artist=item.artist,
                    title=item.title,
                    label=item.label,
                    year=item.year,
                    media_condition=item.media_condition,
                    discogs_price=item.price_eur,
                    median_price=item.median_price_eur,
                    image_url=item.image_url,
                    kleinanzeigen_listing_id=str(mapping_by_release[item.release_id]["kleinanzeigen_listing_id"] or ""),
                    kleinanzeigen_listing_url=str(mapping_by_release[item.release_id]["kleinanzeigen_listing_url"] or ""),
                    status="active",
                )
                continue

            try:
                description = build_discogs_description(item)
                draft = Draft(
                    chat_id=chat_id,
                    title=build_discogs_title(item),
                    description=description,
                    price=max(item.median_price_eur * 0.95, item.price_eur, 1.0),
                    category=DISCOGS_DEFAULT_CATEGORY,
                    shipping_type="SHIPPING",
                    shipping_size="S",
                    special_attributes=_map_condition_to_special_attributes(
                        item.media_condition
                    ),
                    image_file_ids=[],
                )
                image_bytes = await self._download_image_bytes(item.image_url)
                listing_url = await self.kleinanzeigen_service.create_draft(draft, image_bytes)
                listing_id = _extract_listing_id_from_url(listing_url)
                await discogs_mapping_repo.upsert_mapping(
                    chat_id=chat_id,
                    discogs_release_id=item.release_id,
                    discogs_inventory_id=item.inventory_id,
                    artist=item.artist,
                    title=item.title,
                    label=item.label,
                    year=item.year,
                    media_condition=item.media_condition,
                    discogs_price=item.price_eur,
                    median_price=item.median_price_eur,
                    image_url=item.image_url,
                    kleinanzeigen_listing_id=listing_id,
                    kleinanzeigen_listing_url=listing_url,
                    status="active",
                )
                published.append({
                    "release_id": item.release_id,
                    "artist": item.artist,
                    "title": item.title,
                    "listing_id": listing_id,
                    "listing_url": listing_url,
                    "price": f"{max(item.median_price_eur * 0.95, item.price_eur, 1.0):.2f}",
                    "median_price": f"{item.median_price_eur:.2f}",
                })
            except Exception as exc:
                errors.append(f"Publish fehlgeschlagen ({item.artist} - {item.title}): {exc}")

        return {
            "treasures": treasures,
            "published": published,
            "deleted": deleted,
            "updated_mappings": updated_mappings,
            "errors": errors,
        }


def format_discogs_sync_summary(result: dict[str, object]) -> str:
    """Build Telegram message with all discovered and synced data."""
    inventory_count = int(result.get("inventory_count", 0))
    treasure_count = int(result.get("treasure_count", 0))
    updated_mappings = int(result.get("updated_mappings", 0))
    published = result.get("published", [])
    deleted = result.get("deleted", [])
    errors = result.get("errors", [])

    lines = [
        "🎵 Discogs-Sync abgeschlossen",
        "",
        f"Inventory-Eintraege: {inventory_count}",
        f"Treffer (Mainstream + Median > 50 EUR): {treasure_count}",
        f"Referenzen aktualisiert: {updated_mappings}",
        f"Neu veroeffentlicht: {len(published) if isinstance(published, list) else 0}",
        f"Geloescht (Discogs entfernt): {len(deleted) if isinstance(deleted, list) else 0}",
    ]

    if isinstance(published, list) and published:
        lines.append("\nNeu veroeffentlicht:")
        for row in published:
            lines.append(
                f"- {row.get('artist', '')} - {row.get('title', '')} | "
                f"Preis: {row.get('price', '')} EUR | Median: {row.get('median_price', '')} EUR | "
                f"ID: {row.get('listing_id', '-') }"
            )
            lines.append(f"  {row.get('listing_url', '')}")

    if isinstance(deleted, list) and deleted:
        lines.append("\nGeloescht:")
        for row in deleted:
            lines.append(f"- Release {row.get('release_id', '')} | Anzeige {row.get('listing_id', '-')}")

    if isinstance(errors, list) and errors:
        lines.append("\nFehler:")
        for error in errors[:10]:
            lines.append(f"- {error}")

    return "\n".join(lines)
