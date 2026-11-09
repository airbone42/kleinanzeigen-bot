"""Mirror Kleinanzeigen listings to Vinted and keep both sides in step.

Rules (docs/vinted.md):
- New listing published on Kleinanzeigen -> same item on Vinted (draft, then publish).
  Pickup-only and give-away listings are skipped: Vinted only sells with shipping.
- Sold on Vinted -> the Kleinanzeigen listing is deleted.
- Gone from Kleinanzeigen (sold/deleted there) for ``vinted_missing_runs_before_delete``
  sync runs -> the Vinted item is deleted. A bot republish (new Kleinanzeigen ID) is
  followed via ``vinted_mapping_repo.rekey`` so it never counts as gone.
- Reserved on Kleinanzeigen -> hidden on Vinted, shown again when un-reserved.
- Deleted by hand on Vinted -> mapping closed, Kleinanzeigen untouched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from config.settings import settings
from db.repository import discogs_mapping_repo, vinted_mapping_repo
from models.listing import Listing
from services.isbn_lookup import find_book_isbn
from services.vinted_client import (
    VintedApiError,
    VintedAuthError,
    VintedClient,
    VintedError,
    parse_cookie_header,
    summarize_photo,
)
from services.vinted_mapper import (
    build_draft,
    choose_vinted_fields,
    flatten_catalogs,
    TRANSIENT_ERROR_FIELDS,
    book_language_id,
    error_fields,
    format_validation_errors,
    needs_isbn,
    normalize_isbn,
    pick_color,
    to_jpeg,
    item_flags,
    item_state,
    pick_brand,
    pick_size,
    shipping_size_from_listing,
    skip_reason,
    vinted_price,
)

logger = logging.getLogger(__name__)

PROFILE_DIR_NAME = "vinted-profile"
CACHE_DIR_NAME = "vinted"
CATALOG_CACHE_FILE = "catalogs.json"
COLOR_CACHE_FILE = "colors.json"
# Vinted processes uploaded photos asynchronously; publishing right away fails with
# "photos: Fehler beim Hochladen".
PHOTO_SETTLE_SECONDS = 6
PHOTO_RETRY_SECONDS = 20
# Present while the last Vinted contact had no valid session: skips the backfill, whose
# Kleinanzeigen downloads would be wasted (about a minute of browser time each).
AUTH_MARKER_FILE = "auth_required"
MAX_MIRROR_ATTEMPTS = 3
KA_ID_RE = re.compile(r"(\d{6,})")


@dataclass
class MirrorInput:
    ka_listing_id: str
    title: str
    description: str
    price: float
    images: list[bytes]
    chat_id: int = 0
    category: str = ""
    package_size: str = "PICKUP"


@dataclass
class MirrorResult:
    status: str  # active | draft | skipped | failed | unchanged
    message: str
    vinted_item_id: Optional[str] = None
    url: Optional[str] = None
    retry: bool = False  # draft: a fresh attempt may succeed (transient error)


@dataclass
class SyncReport:
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    auth_required: bool = False

    def add(self, line: str) -> None:
        logger.info(f"Vinted sync: {line}")
        self.lines.append(line)

    def error(self, line: str) -> None:
        logger.warning(f"Vinted sync: {line}")
        self.errors.append(line)

    def format(self) -> str:
        if not self.lines and not self.errors:
            return ""
        parts = ["🔁 Vinted-Abgleich"]
        parts += [f"• {line}" for line in self.lines]
        parts += [f"⚠️ {line}" for line in self.errors]
        return "\n".join(parts)


def describe_image(image: bytes) -> str:
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image)) as img:
            return f"{img.format} {img.size[0]}x{img.size[1]} {img.mode}, {len(image)} bytes"
    except Exception as e:
        return f"unreadable ({e.__class__.__name__}), {len(image)} bytes, magic {image[:8].hex()}"


def ka_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = KA_ID_RE.search(url.rstrip("/").rsplit("/", 1)[-1]) or KA_ID_RE.search(url)
    return match.group(1) if match else None


def _norm_title(title: str) -> str:
    return re.sub(r"\W+", " ", re.sub(r"^\s*reserviert[\s:\-–]*", "", title, flags=re.I)).strip().lower()


class VintedSyncService:
    def __init__(self) -> None:
        self.state_dir = settings.kleinanzeigen_config_path
        self.profile_dir = self.state_dir / PROFILE_DIR_NAME
        self.cache_dir = self.state_dir / CACHE_DIR_NAME

    def client(self) -> VintedClient:
        return VintedClient(
            base_url=settings.vinted_base_url,
            profile_dir=self.profile_dir,
            locale=settings.vinted_locale,
            chromium_path=settings.chromium_path,
        )

    def _set_auth_required(self, required: bool) -> None:
        marker = self.cache_dir / AUTH_MARKER_FILE
        try:
            if required:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                marker.touch()
            else:
                marker.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Vinted auth marker: {e}")

    def auth_required(self) -> bool:
        return (self.cache_dir / AUTH_MARKER_FILE).exists()

    def item_url(self, item_id: object) -> str:
        return f"{settings.vinted_base_url.rstrip('/')}/items/{item_id}"

    # ----- login ------------------------------------------------------------
    async def login(self, cookie_header: str) -> str:
        cookies = parse_cookie_header(cookie_header)
        async with self.client() as client:
            await client.import_cookies(cookies)
            user_id = await client.ensure_session()
            await client.wardrobe_items()  # proves the session works for the API
        self._set_auth_required(False)
        return user_id

    # ----- catalog ------------------------------------------------------------
    async def _leaves(self, client: VintedClient) -> list[dict[str, Any]]:
        cache = self.cache_dir / CATALOG_CACHE_FILE
        if cache.exists():
            try:
                leaves = json.loads(cache.read_text(encoding="utf-8"))
                if isinstance(leaves, list) and leaves:
                    return leaves
            except (OSError, json.JSONDecodeError):
                pass
        leaves = flatten_catalogs(await client.catalogs())
        if not leaves:
            raise VintedError("Vinted lieferte keinen Kategorienbaum.")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(leaves, ensure_ascii=False), encoding="utf-8")
        return leaves

    async def _colors(self, client: VintedClient) -> list[dict[str, Any]]:
        cache = self.cache_dir / COLOR_CACHE_FILE
        if cache.exists():
            try:
                colors = json.loads(cache.read_text(encoding="utf-8"))
                if isinstance(colors, list) and colors:
                    return colors
            except (OSError, json.JSONDecodeError):
                pass
        try:
            colors = [c for c in await client.colors() if isinstance(c, dict)]
        except VintedApiError as e:
            logger.warning(f"Vinted colors not available: {e}")
            return []
        if colors:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(colors, ensure_ascii=False), encoding="utf-8")
        return colors

    # ----- mirror one listing ------------------------------------------------
    async def mirror(self, inp: MirrorInput, client: Optional[VintedClient] = None) -> MirrorResult:
        """Put one Kleinanzeigen listing on Vinted and record the mapping."""
        existing = await vinted_mapping_repo.get_by_ka(inp.ka_listing_id)
        retry_draft_id: Optional[int] = None
        if existing is not None and existing["status"] in vinted_mapping_repo.LIVE_STATUSES:
            if (existing["status"] == "draft" and existing["vinted_item_id"]
                    and existing["attempts"] < MAX_MIRROR_ATTEMPTS):
                retry_draft_id = int(existing["vinted_item_id"])  # our rejected draft
            else:
                return MirrorResult("unchanged", "schon auf Vinted",
                                    existing["vinted_item_id"], existing["vinted_url"])

        reason = skip_reason(inp.price, inp.package_size)
        if reason is None and not inp.images:
            reason = "keine Fotos"
        if reason:
            await vinted_mapping_repo.save(inp.ka_listing_id, status="skipped", chat_id=inp.chat_id,
                                           title=inp.title, price=inp.price, last_error=reason)
            return MirrorResult("skipped", f"Vinted übersprungen: {reason}")

        try:
            if client is None:
                async with self.client() as own_client:
                    await own_client.ensure_session()
                    result = await self._mirror_with(own_client, inp, retry_draft_id)
            else:
                result = await self._mirror_with(client, inp, retry_draft_id)
        except VintedAuthError:
            raise
        except Exception as e:
            logger.error(f"Vinted mirror of {inp.ka_listing_id} failed: {e}", exc_info=True)
            await vinted_mapping_repo.save(inp.ka_listing_id, status="failed", chat_id=inp.chat_id,
                                           title=inp.title, price=inp.price,
                                           last_error=str(e)[:500], count_attempt=True)
            return MirrorResult("failed", f"Vinted fehlgeschlagen: {str(e)[:300]}")

        await vinted_mapping_repo.save(
            inp.ka_listing_id, status=result.status, chat_id=inp.chat_id,
            vinted_item_id=result.vinted_item_id, vinted_url=result.url, title=inp.title,
            price=inp.price, last_error=None if result.status == "active" else result.message,
            count_attempt=result.status == "draft",
        )
        if result.status == "draft" and not result.retry:
            await vinted_mapping_repo.set_attempts(inp.ka_listing_id, MAX_MIRROR_ATTEMPTS)
        return result

    async def _mirror_with(
        self, client: VintedClient, inp: MirrorInput, draft_id: Optional[int] = None
    ) -> MirrorResult:
        from services.openrouter import get_text_llm

        leaves = await self._leaves(client)
        colors = await self._colors(client)
        fields = await choose_vinted_fields(
            get_text_llm(temperature=0.0), title=inp.title, description=inp.description,
            ka_category=inp.category, leaves=leaves,
            colors=[str(c.get("title")) for c in colors if c.get("title")],
        )
        if fields.catalog_id is None:
            return MirrorResult("skipped", f"Vinted übersprungen: {', '.join(fields.notes)}")

        brand_id, brand = (None, None)
        if fields.brand:
            brand_id, brand = pick_brand(await client.search_brands(fields.brand), fields.brand)
        size_id = None
        if fields.size:
            try:
                size_id = pick_size(await client.size_groups(fields.catalog_id), fields.size)
            except VintedApiError as e:
                logger.info(f"Size lookup failed for catalog {fields.catalog_id}: {e}")

        color_id = pick_color(colors, fields.color)
        notes: list[str] = []
        isbn = fields.isbn
        if isbn is None and needs_isbn(fields.catalog_path):
            from services.openrouter import get_vision_llm

            isbn, source = await find_book_isbn(
                text_llm=get_text_llm(temperature=0.0), vision_llm=get_vision_llm(temperature=0.0),
                title=inp.title, description=inp.description, images=inp.images,
            )
            if isbn and source != "Anzeigentext":
                notes.append(f"ISBN {isbn} aus {source}, bitte prüfen")

        session_id = str(uuid.uuid4())
        for i, image in enumerate(inp.images[:20]):
            logger.info(f"Vinted photo {i}: {describe_image(image)}")
        photo_ids = [
            await client.upload_photo(to_jpeg(image), session_id, filename=f"photo_{i:02d}.jpg")
            for i, image in enumerate(inp.images[:20])
        ]
        draft = build_draft(
            temp_uuid=session_id, title=inp.title, description=inp.description,
            price=vinted_price(inp.price, settings.vinted_price_factor),
            currency=settings.vinted_currency, catalog_id=fields.catalog_id,
            package_size=inp.package_size, photo_ids=photo_ids, condition=fields.condition,
            brand_id=brand_id, brand=brand, size_id=size_id,
            color_ids=[color_id] if color_id else [], isbn=isbn,
            attributes=(
                {"language_book": [book_language_id(fields.book_language)]}
                if needs_isbn(fields.catalog_path) else None
            ),
        )
        if draft_id is not None:
            # Retry: refill our earlier draft (fresh photo ids) instead of a second one.
            await client.update_draft(draft_id, draft, session_id)
            item_id = str(draft_id)
        else:
            created = await client.create_draft(draft, session_id)
            item_id = str(created["id"])
        url = self.item_url(item_id)
        suffix = f" ({'; '.join(notes)})" if notes else ""
        await asyncio.sleep(PHOTO_SETTLE_SECONDS)
        draft = await self._with_stored_photos(client, int(item_id), draft)
        try:
            try:
                await client.complete_draft(int(item_id), draft, session_id)
            except VintedApiError as e:
                if '"photos"' not in e.body:
                    raise
                await asyncio.sleep(PHOTO_RETRY_SECONDS)
                await client.complete_draft(int(item_id), draft, session_id)
        except VintedApiError as e:
            if '"photos"' in e.body:
                try:
                    stored = await client.get_item_edit(int(item_id))
                    logger.info(
                        "Vinted draft photos after rejection: "
                        + json.dumps([summarize_photo(p) for p in stored.get("photos") or []])[:1500]
                    )
                except Exception as diag:
                    logger.info(f"Vinted draft photo diagnosis failed: {diag}")
            # Transient (photo processing): the next sync deletes the draft and tries
            # again. A missing field the bot cannot know (ISBN, size, ...): the draft
            # stays for the owner to finish in the app.
            fields_failed = error_fields(e.body)
            retry = bool(fields_failed) and fields_failed <= TRANSIENT_ERROR_FIELDS
            next_step = (
                f"Der nächste Abgleich versucht es neu (höchstens {MAX_MIRROR_ATTEMPTS}x)."
                if retry else "Bitte in der Vinted-App ergänzen und hochladen."
            )
            return MirrorResult(
                "draft",
                f"Vinted-Entwurf angelegt ({fields.catalog_path}), Veröffentlichen abgelehnt: "
                f"{format_validation_errors(e.body)}{suffix}. {next_step} {url}",
                item_id, url, retry=retry,
            )
        return MirrorResult(
            "active", f"Auf Vinted veröffentlicht ({fields.catalog_path}){suffix}: {url}", item_id, url
        )

    @staticmethod
    async def _with_stored_photos(
        client: VintedClient, item_id: int, draft: dict[str, Any]
    ) -> dict[str, Any]:
        """Swap the upload's temporary photo ids for the draft's stored ones.

        Saving a draft turns each uploaded photo (temporary id) into a stored photo
        with a new id. Publishing with the temporary ids fails with "photos: Fehler
        beim Hochladen" (09.11.2026: uploaded 37861594399, stored 46398133641).
        """
        try:
            stored = await client.get_item_edit(item_id)
        except VintedApiError as e:
            logger.warning(f"Vinted draft {item_id} not readable before publishing: {e}")
            return draft
        photos = [
            {"id": p["id"], "orientation": p.get("orientation") or 0}
            for p in sorted(stored.get("photos") or [], key=lambda p: p.get("image_no") or 0)
            if isinstance(p, dict) and isinstance(p.get("id"), int)
        ]
        return {**draft, "assigned_photos": photos} if photos else draft

    # ----- hooks from the Kleinanzeigen side ---------------------------------
    async def follow_republish(self, old_ka_id: str, new_ka_id: str, title: str = "") -> None:
        if await vinted_mapping_repo.rekey(old_ka_id, new_ka_id, title):
            logger.info(f"Vinted mapping follows republish {old_ka_id} -> {new_ka_id}")

    async def remove_for_ka(self, ka_listing_id: str, reason_status: str = "sold_ka") -> Optional[str]:
        """Delete the Vinted twin right away (bot deleted the Kleinanzeigen listing)."""
        row = await vinted_mapping_repo.get_by_ka(ka_listing_id)
        if row is None or row["status"] not in vinted_mapping_repo.LIVE_STATUSES or not row["vinted_item_id"]:
            return None
        async with self.client() as client:
            await client.ensure_session()
            await client.delete_item(int(row["vinted_item_id"]))
        await vinted_mapping_repo.set_status(ka_listing_id, reason_status)
        return f"Vinted-Artikel {row['vinted_item_id']} gelöscht"

    # ----- periodic sync -----------------------------------------------------
    async def sync(self, ka_listings: Optional[list[Listing]] = None) -> SyncReport:
        """One full reconciliation run (see module docstring)."""
        from services.kleinanzeigen import KleinanzeigenService

        report = SyncReport()
        ka = KleinanzeigenService()
        if ka_listings is None:
            try:
                ka.purge_downloaded_ads()
                ka_listings = await ka.get_published_listings_summary()
            except Exception as e:
                report.error(f"Kleinanzeigen-Übersicht nicht lesbar, Abgleich abgebrochen: {e}")
                return report

        live = await vinted_mapping_repo.get_live()
        ka_by_id = {l.listing_id: l for l in ka_listings}
        if not ka_listings and live:
            # An empty overview is far more likely a fetch problem than "everything sold".
            report.error("Kleinanzeigen meldet 0 Anzeigen, Abgleich zur Sicherheit übersprungen.")
            return report

        if self.auth_required():
            backfill: list[MirrorInput] = []
            logger.info("Vinted sync: no session last time, skipping backfill")
        else:
            backfill = await self._prepare_backfill(ka, ka_listings, report)

        sold_on_vinted: list[Any] = []
        try:
            async with self.client() as client:
                await client.ensure_session()
                self._set_auth_required(False)
                wardrobe = {str(i.get("id")): i for i in await client.wardrobe_items()}
                if wardrobe:
                    first = next(iter(wardrobe.values()))
                    logger.info(f"Vinted wardrobe: {len(wardrobe)} items, flags e.g. {item_flags(first)}")
                for row in live:
                    try:
                        if await self._reconcile_row(client, row, wardrobe, ka_by_id, ka_listings, report):
                            sold_on_vinted.append(row)
                    except VintedAuthError:
                        raise
                    except Exception as e:
                        report.error(f"{row['title'][:40]}: {e}")
                for inp in backfill:
                    result = await self.mirror(inp, client)
                    if result.status not in ("skipped", "unchanged"):
                        report.add(f"{inp.title[:40]}: {result.message}")
        except VintedAuthError as e:
            self._set_auth_required(True)
            report.auth_required = True
            report.error(f"{e} Abgleich nicht möglich.")
            return report
        except VintedError as e:
            report.error(f"Vinted nicht erreichbar: {e}")

        for row in sold_on_vinted:
            ka_id = row["ka_listing_id"]
            if ka_id not in ka_by_id:
                successor = self._find_successor(row, ka_listings)
                if successor is not None:
                    await vinted_mapping_repo.rekey(ka_id, successor.listing_id)
                    ka_id = successor.listing_id
            if ka_id in ka_by_id:
                try:
                    if await ka.delete_listing(ka_id):
                        report.add(f"Auf Vinted verkauft, Kleinanzeigen-Anzeige gelöscht: {row['title'][:40]}")
                    else:
                        report.error(f"Auf Vinted verkauft, Kleinanzeigen-Löschen fehlgeschlagen: "
                                     f"{row['title'][:40]} ({ka_id}), bitte von Hand löschen")
                        continue
                except Exception as e:
                    report.error(f"Kleinanzeigen-Löschen {ka_id} fehlgeschlagen: {e}")
                    continue
            else:
                report.add(f"Auf Vinted verkauft: {row['title'][:40]}")
            await vinted_mapping_repo.set_status(ka_id, "sold_vinted")
        return report

    async def _reconcile_row(
        self,
        client: VintedClient,
        row: Any,
        wardrobe: dict[str, dict[str, Any]],
        ka_by_id: dict[str, Listing],
        ka_listings: list[Listing],
        report: SyncReport,
    ) -> bool:
        """Reconcile one live mapping. Returns True when the item sold on Vinted."""
        ka_id = str(row["ka_listing_id"])
        item_id = row["vinted_item_id"]
        title = str(row["title"] or ka_id)[:40]
        if not item_id:
            return False
        item = wardrobe.get(str(item_id))
        if item is None:
            try:
                item = await client.get_item_edit(int(item_id))
            except VintedApiError as e:
                if e.status in (404, 410):
                    await vinted_mapping_repo.set_status(ka_id, "vinted_removed")
                    report.add(f"Auf Vinted gelöscht (von Hand?), Kleinanzeigen bleibt: {title}")
                    return False
                raise
        state = item_state(item) if item else "active"
        if state == "sold":
            return True
        if row["status"] == "draft" and state in ("active", "hidden", "reserved"):
            await vinted_mapping_repo.set_status(ka_id, "active")
            report.add(f"Vinted-Entwurf wurde veröffentlicht: {title}")

        ka_listing = ka_by_id.get(ka_id)
        if ka_listing is None:
            successor = self._find_successor(row, ka_listings)
            if successor is not None and await vinted_mapping_repo.rekey(ka_id, successor.listing_id):
                report.add(f"Kleinanzeigen-ID {ka_id} -> {successor.listing_id} übernommen: {title}")
                return False
            missing = await vinted_mapping_repo.bump_missing(ka_id)
            if missing < settings.vinted_missing_runs_before_delete:
                return False
            if state == "draft":
                try:
                    await client.delete_draft(int(item_id))
                    report.add(f"Auf Kleinanzeigen weg, Vinted-Entwurf gelöscht: {title}")
                except VintedApiError as e:
                    # A draft is invisible to buyers; nothing can sell twice.
                    report.add(f"Auf Kleinanzeigen weg, Vinted-Entwurf bitte in der App löschen "
                               f"({e.status}): {title}")
                await vinted_mapping_repo.set_status(ka_id, "sold_ka")
                return False
            await client.delete_item(int(item_id))
            await vinted_mapping_repo.set_status(ka_id, "sold_ka")
            report.add(f"Auf Kleinanzeigen weg, Vinted-Artikel gelöscht: {title}")
            return False

        await vinted_mapping_repo.reset_missing(ka_id)
        if ka_listing.status == "reserved" and row["status"] == "active" and state == "active":
            await client.set_hidden(int(item_id), True)
            await vinted_mapping_repo.set_status(ka_id, "hidden")
            report.add(f"Auf Kleinanzeigen reserviert, auf Vinted verborgen: {title}")
        elif ka_listing.status != "reserved" and row["status"] == "hidden":
            await client.set_hidden(int(item_id), False)
            await vinted_mapping_repo.set_status(ka_id, "active")
            report.add(f"Reservierung aufgehoben, auf Vinted wieder sichtbar: {title}")
        return False

    @staticmethod
    def _find_successor(row: Any, ka_listings: list[Listing]) -> Optional[Listing]:
        """A Kleinanzeigen listing with the same title (republished outside the bot hook)."""
        wanted = _norm_title(str(row["title"] or ""))
        if not wanted:
            return None
        matches = [l for l in ka_listings if _norm_title(l.title) == wanted]
        return matches[0] if len(matches) == 1 else None

    async def _prepare_backfill(
        self, ka: Any, ka_listings: list[Listing], report: SyncReport
    ) -> list[MirrorInput]:
        """Pick a few Kleinanzeigen listings that are not on Vinted yet and load their data."""
        limit = settings.vinted_backfill_per_run
        if limit <= 0:
            return []
        known = {str(r["ka_listing_id"]): r for r in await vinted_mapping_repo.get_all()}
        discogs_rows = []
        chat_ids = settings.telegram_allowed_user_ids
        for chat_id in chat_ids:
            discogs_rows += await discogs_mapping_repo.get_active_by_chat(chat_id)
        inputs: list[MirrorInput] = []
        for listing in ka_listings:
            if len(inputs) >= limit:
                break
            if listing.status == "reserved":
                continue
            row = known.get(listing.listing_id)
            # failed, or our rejected draft that the reconcile step below deletes first
            retry = row is not None and row["status"] in ("failed", "draft") and row["attempts"] < MAX_MIRROR_ATTEMPTS
            if row is not None and not retry:
                continue
            # Discogs owns these records: a Vinted sale would not reach the Discogs inventory.
            if any(discogs_mapping_repo.matches_listing(r, listing.listing_id, listing.title) for r in discogs_rows):
                continue
            detailed = await ka.download_single_listing(listing.listing_id)
            if detailed is None:
                report.error(f"Kleinanzeigen-Anzeige {listing.listing_id} nicht ladbar, Vinted später")
                continue
            images = images_from_dir(ka.image_archive_dir / listing.listing_id)
            inputs.append(MirrorInput(
                ka_listing_id=listing.listing_id,
                chat_id=chat_ids[0] if chat_ids else 0,
                title=detailed.title,
                description=detailed.description,
                price=detailed.price or listing.price,
                images=images,
                category=detailed.category,
                package_size=shipping_size_from_listing(
                    detailed.shipping_type, shipping_options=detailed.shipping_options),
            ))
        return inputs

    async def status_text(self) -> str:
        rows = await vinted_mapping_repo.get_all()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        labels = {
            "active": "aktiv", "draft": "Entwurf (manuell fertigstellen)", "hidden": "verborgen",
            "sold_vinted": "auf Vinted verkauft", "sold_ka": "auf Kleinanzeigen weg",
            "vinted_removed": "auf Vinted gelöscht", "skipped": "übersprungen", "failed": "Fehler",
        }
        lines = [f"Vinted-Spiegel: {'an' if settings.vinted_enabled else 'aus'}",
                 f"Abgleich um {', '.join(f'{h}:00' for h in settings.get_vinted_sync_hours())} Uhr"]
        lines += [f"{labels.get(k, k)}: {v}" for k, v in sorted(counts.items())]
        drafts = [r for r in rows if r["status"] == "draft"][:5]
        lines += [f"Entwurf: {r['title'][:40]} {r['vinted_url'] or ''}" for r in drafts]
        failed = [r for r in rows if r["status"] == "failed"][:5]
        lines += [f"Fehler: {r['title'][:40]}: {(r['last_error'] or '')[:120]}" for r in failed]
        return "\n".join(lines)


def images_from_dir(directory: Path) -> list[bytes]:
    from services.kleinanzeigen import IMAGE_SUFFIXES

    if not directory.is_dir():
        return []
    return [p.read_bytes() for p in sorted(directory.rglob("*"))
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
