"""Wrapper around the kleinanzeigen-bot CLI tool.

Studied from https://github.com/Second-Hand-Friends/kleinanzeigen-bot

YAML Ad structure (required fields):
    active: bool
    type: OFFER | WANTED
    title: str (min 10 chars, max 50 chars)
    description: str (max 4000 chars)
    category: str  (built-in name or ID)
    price: int     (in EUR, required when price_type=FIXED)
    price_type: FIXED | NEGOTIABLE | GIVE_AWAY | NOT_APPLICABLE
    shipping_type: PICKUP | SHIPPING | NOT_APPLICABLE
    sell_directly: bool
    republication_interval: int  (days)
    auto_price_reduction:
        enabled: bool
    contact:
        name: str
        street: str
        zipcode: str
        phone: str
    images:
        - "*.jpg"  # glob pattern relative to ad file location

CLI commands:
    kleinanzeigen-bot --config <config.yaml> publish --ads new|due|all|changed|<numeric_id>
    kleinanzeigen-bot --config <config.yaml> download
    kleinanzeigen-bot --config <config.yaml> delete --ads <numeric_id>
    kleinanzeigen-bot --config <config.yaml> verify
Note: --ads no longer accepts file paths (changed in 2026+9baba41).
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from langchain_core.messages import HumanMessage, SystemMessage

from agents.nodes.text_generation import _CATEGORY_LIST
from config.settings import settings
from models.draft import Draft
from models.listing import Listing
from services.categories import DEFAULT_CATEGORY as _DEFAULT_CATEGORY, canonical_category
from services.openrouter import get_text_llm

logger = logging.getLogger(__name__)

# Default category if none specified – must be resolvable by the bot's categories.yaml
DEFAULT_CATEGORY = _DEFAULT_CATEGORY
# Default republication interval in days
DEFAULT_REPUBLICATION_INTERVAL = 60
# Ads subdirectory within the config path
ADS_DIR_NAME = "ads"
# Download directory used by `kleinanzeigen-bot download`
DOWNLOADED_ADS_DIR_NAME = "downloaded-ads"
# Config file name
CONFIG_FILE_NAME = "config.yaml"
# Timeout for single-ad download operations (seconds)
DOWNLOAD_SINGLE_TIMEOUT = 300
# Timeout for publish --ads new (seconds)
PUBLISH_NEW_TIMEOUT = 900
# Environment/setup warnings the bot emits regardless of the actual failure.
BOT_NOISE_WARNING_RE = re.compile(r"nodriver CDP re-attach patch not found")

# Retry behaviour for the published-ads API (in-page fetch fails sporadically)
PUBLISHED_ADS_FETCH_ATTEMPTS = 3
PUBLISHED_ADS_RETRY_DELAY = 3
PUBLISHED_ADS_MAX_PAGES = 100


def _coerce_page_number(raw: object) -> Optional[int]:
    """Coerce a paging value from the API into a positive page number."""
    try:
        page = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def _coerce_api_price(raw: object) -> float:
    """Coerce the ``price`` field of the published-ads API into EUR as float.

    The API returns numbers, formatted strings ("1.250 € VB", "15,00 €")
    or nested dicts ({"amount": ..., "currencyCode": "EUR"}). Returns 0.0 when no
    amount can be derived (e.g. "Zu verschenken" / "VB" without a value).
    """
    if raw is None:
        return 0.0
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, dict):
        for key in ("amount", "value", "amountInEuro", "priceAmount", "price"):
            if key in raw:
                return _coerce_api_price(raw[key])
        cents = raw.get("priceInEuroCent") or raw.get("amountInCent")
        if isinstance(cents, (int, float)):
            return float(cents) / 100.0
        return 0.0
    if isinstance(raw, str):
        # "1.250 € VB" -> 1250.0 ; "15,00 €" -> 15.0
        match = re.search(r"\d{1,3}(?:\.\d{3})+|\d+(?:,\d{1,2})?", raw)
        if not match:
            return 0.0
        token = match.group(0)
        if "." in token and "," not in token:
            token = token.replace(".", "")
        else:
            token = token.replace(".", "").replace(",", ".")
        try:
            return float(token)
        except ValueError:
            return 0.0
    return 0.0


class KleinanzeigenError(Exception):
    """Raised when the Kleinanzeigen CLI returns an error."""


class KleinanzeigenService:
    """Wrapper around the kleinanzeigen-bot CLI tool.

    Manages the ad YAML files and delegates operations to the CLI.

    Directory layout:
        kleinanzeigen_config_path/
        ├── config.yaml          # User-configured credentials (pre-existing)
        └── ads/                 # Ad files managed by this service
            ├── ad_<uuid>.yaml
            ├── ad_<uuid>/
            │   ├── image_0.jpg
            │   └── image_1.jpg
            └── ...
    """

    def __init__(self) -> None:
        self.config_path = settings.kleinanzeigen_config_path
        self.config_file = self.config_path / CONFIG_FILE_NAME
        self.ads_dir = self.config_path / ADS_DIR_NAME
        self.downloaded_ads_dir = self.config_path / DOWNLOADED_ADS_DIR_NAME
        self.ads_dir.mkdir(parents=True, exist_ok=True)

    def _get_cli_command(self) -> list[str]:
        """Get the kleinanzeigen-bot CLI invocation as an argv list.

        Runs through services.kb_cli so the runtime patches in services.kb_patches
        (redesigned ad-page shipping extraction) are active for every CLI call.
        Returned as a list (not a space-joined string) so a sys.executable path
        containing spaces is passed as a single argv token instead of being split.
        """
        return [sys.executable, "-m", "services.kb_cli"]

    def _clear_browser_locks(self) -> None:
        """Remove stale LevelDB/Chromium lock files left by crashed browser processes."""
        browser_profile = self.config_path / "browser-profile"
        if not browser_profile.exists():
            return
        for lock_file in browser_profile.rglob("LOCK"):
            try:
                lock_file.unlink()
                logger.debug(f"Removed stale browser lock: {lock_file}")
            except Exception:
                pass

    def _clear_browser_profile(self) -> None:
        """Delete the entire browser profile to force a completely fresh session."""
        browser_profile = self.config_path / "browser-profile"
        if browser_profile.exists():
            shutil.rmtree(browser_profile, ignore_errors=True)
            logger.info("Cleared browser profile at %s to force fresh session", browser_profile)

    async def _pre_session_setup(self) -> None:
        """Clear stale browser lock files before each CLI run.

        When a browser process is killed abruptly it leaves LevelDB LOCK files
        that prevent the next Chromium instance from starting. We clear them
        proactively so the CLI can always start a fresh session.
        """
        self._clear_browser_locks()

    async def _run_cli(
        self,
        *args: str,
        timeout: Optional[int] = 120,
    ) -> tuple[int, str, str]:
        """Run kleinanzeigen-bot CLI with given arguments.

        Args:
            *args: CLI arguments
            timeout: Timeout in seconds. Set to None to disable timeout.

        Returns:
            Tuple of (returncode, stdout, stderr)
        """
        cli = self._get_cli_command()
        config_args = ["--config", CONFIG_FILE_NAME] if self.config_file.exists() else []

        # --config must come AFTER the module/executable name.
        # e.g. [python, -m, kleinanzeigen_bot, --config, config.yaml, <command>, ...]
        cmd_parts = cli + config_args + list(args)

        logger.info(f"Running CLI: {' '.join(cmd_parts)}")

        try:
            # The CLI runs with cwd=config_path, so the app root must be on
            # PYTHONPATH for `python -m services.kb_cli` to be importable.
            env = dict(os.environ)
            app_root = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = (
                app_root + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else app_root
            )
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.config_path),
                env=env,
            )
            if timeout is None:
                stdout_b, stderr_b = await proc.communicate()
            else:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            returncode = proc.returncode or 0

            if stdout:
                logger.debug(f"CLI stdout: {stdout[:500]}")
            if stderr:
                logger.debug(f"CLI stderr: {stderr[:500]}")

            return returncode, stdout, stderr

        except asyncio.TimeoutError:
            try:
                proc.kill()
                try:
                    partial_out, partial_err = await asyncio.wait_for(proc.communicate(), timeout=5)
                    partial = (partial_err.decode("utf-8", errors="replace") + "\n" +
                               partial_out.decode("utf-8", errors="replace")).strip()
                    if partial:
                        logger.error(f"Partial output before timeout:\n{partial[-1000:]}")
                except Exception:
                    pass
            except Exception:
                pass
            logger.error(f"CLI command timed out after {timeout}s")
            raise KleinanzeigenError(f"CLI command timed out after {timeout} seconds")
        except FileNotFoundError as e:
            logger.error(f"CLI executable not found: {e}")
            raise KleinanzeigenError(
                "kleinanzeigen-bot nicht gefunden. Bitte installiere es mit: pip install kleinanzeigen-bot"
            )

    async def diagnose(self) -> str:
        """Run 'kleinanzeigen-bot --verbose diagnose' and return combined output."""
        try:
            returncode, stdout, stderr = await self._run_cli("--verbose", "diagnose", timeout=60)
        except KleinanzeigenError as e:
            return f"Diagnose-Fehler: {e}"
        return (stderr + "\n" + stdout).strip() or "(keine Ausgabe)"

    def _build_ad_yaml(self, draft: Draft, image_dir: Optional[Path] = None) -> dict:
        """Build the ad YAML data structure from a Draft model.

        Args:
            draft: The draft model
            image_dir: Optional directory containing images

        Returns:
            Dict ready for YAML serialization
        """
        # Title must be at least 10 chars
        title = draft.title
        if len(title) < 10:
            title = title + " " + ("– Privatverkauf"[:10 - len(title) - 1])
        title = title[:50]  # Enforce max length

        # Category: use draft.category or default
        category = canonical_category(draft.category)

        # Build image glob pattern
        images: list[str] = []
        if image_dir and image_dir.exists():
            image_files = sorted(image_dir.glob("*.jpg")) + sorted(image_dir.glob("*.jpeg")) + sorted(image_dir.glob("*.png"))
            if image_files:
                images = [str(image_dir.name) + "/*.jpg"]

        # Load contact details from config if available
        contact = self._load_contact_from_config()

        _size_options: dict[str, list[str]] = {
            "S": ["DHL_2", "Hermes_S"],
            "M": ["DHL_5", "Hermes_M"],
            "L": ["DHL_10", "Hermes_L"],
        }
        shipping_size = getattr(draft, "shipping_size", "PICKUP") or "PICKUP"
        shipping_type = "SHIPPING" if shipping_size in ("S", "M", "L") else "PICKUP"

        ad_data = {
            "active": True,
            "type": "OFFER",
            "title": title,
            "description": draft.description[:4000],  # Enforce max length
            "category": category,
            "price": int(round(draft.price)) if draft.price > 0 else None,
            "price_type": "NEGOTIABLE" if draft.price >= 1 else "GIVE_AWAY",
            "auto_price_reduction": {"enabled": False},
            "shipping_type": shipping_type,
            "sell_directly": shipping_type == "SHIPPING",
            "republication_interval": self._load_default_republication_interval(),
            "contact": contact,
        }
        special_attributes = getattr(draft, "special_attributes", None)
        if isinstance(special_attributes, dict) and special_attributes:
            ad_data["special_attributes"] = special_attributes

        if shipping_type == "SHIPPING":
            base_options = _size_options.get(shipping_size, _size_options["S"])
            # Hermes Paeckchen is cheaper and available for small items under 50 EUR
            if shipping_size == "S" and int(round(draft.price)) < 50:
                options = base_options + ["Hermes_Päckchen"]
            else:
                options = base_options
            ad_data["shipping_options"] = options

        if images:
            ad_data["images"] = images

        # Remove None values
        return {k: v for k, v in ad_data.items() if v is not None}

    def _load_contact_from_config(self) -> dict:
        """Load contact details from the kleinanzeigen-bot config file."""
        default_contact = {
            "name": "",
            "phone": "",
            "street": "",
            "zipcode": "",
        }

        if not self.config_file.exists():
            return default_contact

        try:
            with open(self.config_file, encoding="utf-8") as f:
                config = yaml.safe_load(f)
            contact = config.get("ad_defaults", {}).get("contact", {})
            if contact:
                return contact
        except Exception as e:
            logger.warning(f"Could not read contact from config: {e}")

        return default_contact

    def _load_default_republication_interval(self) -> int:
        """Load default republication interval (days) from config ad_defaults."""
        if not self.config_file.exists():
            return DEFAULT_REPUBLICATION_INTERVAL

        try:
            with open(self.config_file, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            raw_interval = config.get("ad_defaults", {}).get("republication_interval")
            if raw_interval in (None, ""):
                return DEFAULT_REPUBLICATION_INTERVAL
            interval = int(raw_interval)
            return interval if interval > 0 else DEFAULT_REPUBLICATION_INTERVAL
        except Exception as e:
            logger.warning(f"Could not read republication interval from config: {e}")
            return DEFAULT_REPUBLICATION_INTERVAL

    def purge_downloaded_ads(self) -> None:
        """Remove all downloaded ads (fresh start for checks)."""
        if self.downloaded_ads_dir.exists():
            shutil.rmtree(self.downloaded_ads_dir, ignore_errors=True)
        self.downloaded_ads_dir.mkdir(parents=True, exist_ok=True)

    def purge_listing_cache(self, listing_id: str) -> None:
        """Remove cached YAML files and directories for a specific listing."""
        yaml_path = self._find_yaml_by_listing_id(listing_id)
        if yaml_path:
            ad_dir = yaml_path.parent
            shutil.rmtree(ad_dir, ignore_errors=True)
            logger.info(f"Purged cached ad dir: {ad_dir}")
        ad_dir = self._find_ad_dir_by_listing_id(listing_id)
        if ad_dir and ad_dir.exists():
            shutil.rmtree(ad_dir, ignore_errors=True)
            logger.info(f"Purged cached ad dir: {ad_dir}")

    async def _fetch_published_ads_raw(self) -> list[dict]:
        """Fetch published ads via kleinanzeigen-bot API (no overview pagination)."""
        from kleinanzeigen_bot import runtime_config as _runtime_config
        from kleinanzeigen_bot.app import KleinanzeigenBot

        bot = KleinanzeigenBot()
        bot.command = "download"
        bot.ads_selector = "all"
        bot._config_arg = str(self.config_file)
        bot.config_file_path = str(self.config_file)
        bot.workspace = _runtime_config.resolve_workspace(
            command=bot.command,
            config_file_path=bot.config_file_path,
            config_arg=bot._config_arg,
            logfile_arg=None,
            workspace_mode=None,
            logfile_explicitly_provided=False,
            log_basename=bot._log_basename,
        )
        if bot.workspace is not None:
            bot.config_file_path = str(bot.workspace.config_file)
            bot.log_file_path = str(bot.workspace.log_file) if bot.workspace.log_file else None
        bot._bootstrap_runtime()
        try:
            await bot.create_browser_session()
            await bot.login()
            return await self._fetch_published_ads_resilient(bot)
        finally:
            await bot.close_browser_session()

    async def _fetch_published_ads_resilient(self, bot) -> list[dict]:
        """Fetch published ads with retries and a navigation-based fallback.

        The upstream fetch runs `fetch()` inside the logged-in page. That call
        fails sporadically with "TypeError: Failed to fetch" (request aborted by a
        concurrent navigation, or a cross-origin redirect that CORS blocks), which
        aborted the whole check. Retry, then fall back to opening the JSON endpoint
        as a normal navigation, which is immune to both causes.
        """
        from kleinanzeigen_bot import published_ads as _published_ads

        last_error: Optional[Exception] = None
        for attempt in range(1, PUBLISHED_ADS_FETCH_ATTEMPTS + 1):
            try:
                ads = await _published_ads.fetch_published_ads(bot, bot.root_url)
                if ads:
                    return ads
                logger.warning(f"Published-ads fetch returned no ads (attempt {attempt})")
            except Exception as e:
                last_error = e
                page_url = getattr(getattr(bot, "page", None), "url", "?")
                logger.warning(
                    f"Published-ads fetch failed (attempt {attempt}, page={page_url}): {e}"
                )
            if attempt < PUBLISHED_ADS_FETCH_ATTEMPTS:
                await asyncio.sleep(PUBLISHED_ADS_RETRY_DELAY * attempt)
                try:
                    await bot.web_open(bot.root_url)
                except Exception as e:
                    logger.warning(f"Could not reopen {bot.root_url} before retry: {e}")

        logger.info("Falling back to navigation-based published-ads fetch")
        try:
            return await self._fetch_published_ads_via_navigation(bot)
        except Exception as e:
            if last_error is not None:
                raise KleinanzeigenError(
                    f"Anzeigen-Übersicht konnte nicht geladen werden: {last_error}"
                ) from e
            raise

    async def _fetch_published_ads_via_navigation(self, bot) -> list[dict]:
        """Read the manage-ads JSON API by navigating to it instead of fetch()."""
        import json

        ads: list[dict] = []
        page = 1
        while page <= PUBLISHED_ADS_MAX_PAGES:
            url = f"{bot.root_url}/m-meine-anzeigen-verwalten.json?sort=DEFAULT&pageNum={page}"
            await bot.web_open(url)
            raw = await bot.web_execute(
                "document.body ? (document.body.innerText || document.body.textContent) : ''"
            )
            if not isinstance(raw, str) or not raw.strip():
                logger.warning(f"Navigation fallback: empty body on page {page}")
                break
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"Navigation fallback: non-JSON body on page {page}: {raw[:200]!r}")
                break
            if not isinstance(data, dict) or not isinstance(data.get("ads"), list):
                logger.warning(f"Navigation fallback: unexpected payload on page {page}")
                break
            ads.extend(a for a in data["ads"] if isinstance(a, dict) and "id" in a and "state" in a)

            paging = data.get("paging")
            if not isinstance(paging, dict):
                break
            next_page = _coerce_page_number(paging.get("next"))
            last_page = _coerce_page_number(paging.get("last"))
            current = _coerce_page_number(paging.get("pageNum")) or page
            if next_page is None or (last_page is not None and current >= last_page):
                break
            page = next_page

        logger.info(f"Navigation fallback fetched {len(ads)} published ads")
        return ads

    async def get_published_listings_summary(self) -> list[Listing]:
        """Return Listing summaries derived from the published ads API."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ads = await self._fetch_published_ads_raw()
        tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(tz).date()
        listings: list[Listing] = []
        for ad in ads:
            ad_id = ad.get("id")
            if ad_id is None:
                continue
            end_date = ad.get("endDate")
            days_remaining = None
            if isinstance(end_date, str) and end_date:
                try:
                    end_dt = datetime.strptime(end_date, "%d.%m.%Y").date()
                    days_remaining = (end_dt - today).days
                except ValueError:
                    pass
            created_at = None
            creation_date = ad.get("creationDate")
            if isinstance(creation_date, str) and creation_date:
                try:
                    created_at = datetime.strptime(creation_date, "%d.%m.%Y").replace(tzinfo=tz)
                except ValueError:
                    created_at = None
            price_val = _coerce_api_price(ad.get("price"))
            if price_val == 0.0 and ad.get("price"):
                logger.debug(f"Unparsable API price for ad {ad_id}: {ad.get('price')!r}")
            # state=="paused" means reserved via Kleinanzeigen UI;
            # title prefix "Reserviert" covers manual title-based reservations
            ad_state = str(ad.get("state") or "active")
            ad_title = str(ad.get("title") or "")
            listing_status = "reserved" if (
                ad_state == "paused" or ad_title.lower().startswith("reserviert")
            ) else "active"
            listings.append(
                Listing(
                    listing_id=str(ad_id),
                    title=str(ad.get("title") or ""),
                    description="",
                    price=price_val,
                    category=str(ad.get("category") or ""),
                    status=listing_status,
                    days_remaining=days_remaining,
                    created_at=created_at,
                )
            )

        # Enrich shipping_type: YAML files are authoritative, DB cache as fallback
        yaml_shipping = self._scan_local_yaml_shipping_types()
        from db.repository import listing_repo
        for listing in listings:
            if listing.listing_id in yaml_shipping:
                listing.shipping_type = yaml_shipping[listing.listing_id]
            else:
                row = await listing_repo.get(listing.listing_id)
                if row and row["shipping_type"] and row["shipping_type"] != "SHIPPING":
                    listing.shipping_type = row["shipping_type"]

        return listings

    async def create_draft(self, draft: Draft, images: list[bytes]) -> str:
        """Create an ad YAML file and publish it via the CLI.

        Creates the YAML file + images locally, then runs 'kleinanzeigen-bot publish'.
        Returns the listing URL or ID after publishing.

        Args:
            draft: Draft model with listing data
            images: List of image bytes

        Returns:
            Listing URL or ID string
        """
        ad_id = str(uuid.uuid4())[:8]
        ad_dir = self.ads_dir / f"ad_{ad_id}"
        ad_dir.mkdir(parents=True, exist_ok=True)

        # Save images
        if images:
            img_subdir = ad_dir / "images"
            img_subdir.mkdir(exist_ok=True)
            for i, img_bytes in enumerate(images):
                img_path = img_subdir / f"image_{i:02d}.jpg"
                img_path.write_bytes(img_bytes)
                logger.debug(f"Saved image {i} to {img_path}")

        # Build and write YAML
        image_subdir = ad_dir / "images" if images else None
        ad_data = self._build_ad_yaml(draft, image_subdir)
        yaml_path = ad_dir / "ad.yaml"

        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(ad_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        logger.info(f"Created ad YAML at {yaml_path}")

        # Remove any other ad directories so 'publish --ads new' only picks up this one.
        for existing in self.ads_dir.iterdir():
            if existing.is_dir() and existing.name.startswith("ad_") and existing != ad_dir:
                shutil.rmtree(existing, ignore_errors=True)
                logger.debug(f"Removed stale ad dir before publish: {existing}")

        # Publish: --ads new picks up all YAMLs without an id field (i.e. never published)
        try:
            await self._pre_session_setup()
            returncode, stdout, stderr = await self._run_cli(
                "--verbose", "publish", "--ads", "new", timeout=PUBLISH_NEW_TIMEOUT
            )

            if returncode != 0:
                error_msg = (stderr + "\n" + stdout).strip()
                logger.error(f"CLI publish failed (rc={returncode}): {error_msg}")
                raise KleinanzeigenError(
                    f"Fehler beim Hochladen: {error_msg[-600:]}"
                )

            # Extract listing URL from CLI output
            listing_url = self._extract_listing_url(stdout + stderr)

            # Fallback: read the id field written back into YAML by kleinanzeigen-bot
            if not listing_url:
                try:
                    with open(yaml_path, encoding="utf-8") as f:
                        updated_data = yaml.safe_load(f)
                    ad_listing_id = str(updated_data.get("id", "")).strip()
                    if ad_listing_id and ad_listing_id != "None":
                        listing_url = f"https://www.kleinanzeigen.de/s-anzeige/id/{ad_listing_id}"
                except Exception as url_err:
                    logger.debug(f"Could not read id from YAML: {url_err}")

            # Clean up local ad files – the listing is now live on Kleinanzeigen.
            shutil.rmtree(ad_dir, ignore_errors=True)
            logger.debug(f"Cleaned up ad dir after publish: {ad_dir}")

            logger.info(f"Ad published, URL: {listing_url}")
            return listing_url or f"https://www.kleinanzeigen.de/m-meine-anzeigen.html"

        except KleinanzeigenError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error during publish: {e}", exc_info=True)
            raise KleinanzeigenError(f"Unerwarteter Fehler: {str(e)}")

    _ALLOWED_AD_FIELDS: frozenset = frozenset({
        "active", "type", "title", "description", "category",
        "price", "price_type", "auto_price_reduction",
        "shipping_type", "sell_directly", "shipping_options",
        "republication_interval", "contact", "special_attributes", "images",
    })

    @staticmethod
    def _sanitize_ad_yaml(data: dict) -> dict:
        """Drop unsupported keys and canonicalize the category.

        Downloaded ads carry the leaf category name only ("Bücher & Zeitschriften"),
        which the bot cannot resolve: it then opens the category page with the raw
        name, nothing gets selected and the shipping section never renders.
        """
        clean = {k: v for k, v in data.items() if k in KleinanzeigenService._ALLOWED_AD_FIELDS}
        if "category" in clean:
            clean["category"] = canonical_category(clean["category"])
        return clean

    @staticmethod
    def _normalize_shipping(existing_options: list[str], price: float) -> list[str]:
        """Map existing shipping_options to canonical size-based set (same logic as _build_ad_yaml)."""
        _size_options: dict[str, list[str]] = {
            "S": ["DHL_2", "Hermes_S"],
            "M": ["DHL_5", "Hermes_M"],
            "L": ["DHL_10", "Hermes_L"],
        }
        _option_to_size = {
            "DHL_2": "S", "Hermes_S": "S", "Hermes_Päckchen": "S",
            "DHL_5": "M", "Hermes_M": "M",
            "DHL_10": "L", "Hermes_L": "L", "DHL_20": "L", "DHL_31,5": "L",
        }
        size = next((_option_to_size[o] for o in existing_options if o in _option_to_size), "S")
        options = _size_options[size]
        if size == "S" and int(round(price)) < 50:
            options = options + ["Hermes_Päckchen"]
        return options

    def _extract_bot_error(self, output: str) -> str:
        """Extract the most relevant error line from bot output for user-visible messages.

        ERROR lines win over WARNING lines: the bot emits environment warnings
        (e.g. the nodriver patch hint) long before the actual failure, and showing
        those instead of the real cause makes the message useless.
        """
        lines = output.splitlines()
        errors: list[str] = []
        warnings: list[str] = []
        for idx, line in enumerate(lines):
            if re.search(r"\[ERROR\]|All \d+ attempts failed|CaptchaEncountered", line):
                errors.append(self._with_error_details(lines, idx))
            elif "[WARNING]" in line and not BOT_NOISE_WARNING_RE.search(line):
                warnings.append(line.strip())
        if errors:
            return errors[-1]
        if warnings:
            return warnings[-1]
        return "Unbekannter Fehler (Details: /logs)"

    @staticmethod
    def _with_error_details(lines: list[str], idx: int) -> str:
        """Append bullet detail lines (e.g. pydantic validation errors) to a log line."""
        parts = [lines[idx].strip()]
        for follow in lines[idx + 1:idx + 4]:
            stripped = follow.strip()
            if not stripped.startswith("- "):
                break
            parts.append(stripped)
        return " ".join(parts)

    def _extract_listing_url(self, output: str) -> Optional[str]:
        """Extract Kleinanzeigen listing URL from CLI output."""
        # Common URL patterns in output
        url_patterns = [
            r'https://www\.kleinanzeigen\.de/s-anzeige/[\w-]+/\d+',
            r'https://www\.kleinanzeigen\.de/[\w/-]+/\d+',
        ]
        for pattern in url_patterns:
            match = re.search(pattern, output)
            if match:
                return match.group(0)
        return None

    def _extract_listing_id(self, output: str) -> Optional[str]:
        """Extract Kleinanzeigen listing ID from CLI output."""
        match = re.search(r'/(\d{7,12})', output)
        if match:
            return match.group(1)
        return None

    def _extract_listing_id_from_yaml(self, yaml_path: Path) -> Optional[str]:
        """Extract Kleinanzeigen listing ID from a YAML file."""
        try:
            with open(yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            listing_id = str(data.get("id", "")).strip()
            return listing_id if listing_id and listing_id != "None" else None
        except Exception as e:
            logger.debug(f"Could not read id from YAML {yaml_path}: {e}")
            return None

    async def get_active_listings(
        self, *, refresh: bool = True, allow_cache_fallback: bool = True
    ) -> list[Listing]:
        """Download and return all active listings from Kleinanzeigen.

        Optionally runs 'kleinanzeigen-bot download' and parses local YAML files.

        Returns:
            List of Listing models
        """
        if refresh:
            logger.info("Downloading active listings from Kleinanzeigen")
        else:
            logger.info("Reading cached active listings from local YAML files")

        if refresh:
            if self.downloaded_ads_dir.exists():
                shutil.rmtree(self.downloaded_ads_dir, ignore_errors=True)
            self.downloaded_ads_dir.mkdir(parents=True, exist_ok=True)
            try:
                await self._pre_session_setup()
                returncode, stdout, stderr = await self._run_cli(
                    "--verbose", "download",
                    timeout=None,
                )

                if returncode != 0:
                    error_msg = (stderr + "\n" + stdout).strip()
                    if "successfully" in error_msg.lower():
                        # Partial success: some ads downloaded despite non-zero exit code
                        logger.warning(f"CLI download returned rc={returncode} but reported successes – continuing: {error_msg[-300:]}")
                        if not list(self.downloaded_ads_dir.rglob("*.yaml")):
                            logger.error("CLI reported success but no YAMLs were downloaded")
                            raise KleinanzeigenError("Download fehlgeschlagen: keine YAMLs geschrieben")
                    else:
                        logger.error(f"CLI download failed (rc={returncode}): {error_msg}")
                        raise KleinanzeigenError(f"Download fehlgeschlagen: {error_msg[-600:]}")
            except KleinanzeigenError:
                raise
            except Exception as e:
                if allow_cache_fallback:
                    logger.warning(f"Download command failed: {e}, reading cached YAML files")
                else:
                    logger.error(f"Download command failed: {e}")
                    raise
            if not allow_cache_fallback and not list(self.downloaded_ads_dir.rglob("*.yaml")):
                logger.error("No YAMLs downloaded and cache fallback disabled")
                raise KleinanzeigenError("Download fehlgeschlagen: keine YAMLs geschrieben")

        # Parse listing YAML files from download output first, then fallback/source dirs.
        # Deduplicate by listing_id because the same ad can exist in multiple locations.
        listings = []
        seen_ids: set[str] = set()
        yaml_files = self._get_listing_yaml_files()
        logger.info(f"Parsing {len(yaml_files)} YAML files from listing directories")

        for yaml_path in yaml_files:
            listing = self._parse_ad_yaml(yaml_path)
            if listing and listing.listing_id not in seen_ids:
                seen_ids.add(listing.listing_id)
                listings.append(listing)

        logger.info(f"Found {len(listings)} active listings")
        return listings

    def _get_listing_yaml_files(self) -> list[Path]:
        """Collect candidate listing YAML files from known directories.

        `kleinanzeigen-bot download` writes into `downloaded-ads/`, while local drafts
        and manually managed ads live in `ads/`. We check both to support current and
        older layouts.
        """
        candidate_dirs = [self.downloaded_ads_dir, self.ads_dir]
        files: list[Path] = []
        seen_paths: set[Path] = set()
        for directory in candidate_dirs:
            if not directory.exists():
                continue
            for yaml_path in directory.rglob("*.yaml"):
                resolved = yaml_path.resolve()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                files.append(yaml_path)
        return files

    def get_cached_active_listing_ids(self) -> set[str]:
        """Return active listing IDs from local cached YAML files."""
        listing_ids: set[str] = set()
        for yaml_path in self._get_listing_yaml_files():
            listing = self._parse_ad_yaml(yaml_path)
            if listing:
                listing_ids.add(listing.listing_id)
        return listing_ids

    def get_active_slots_usage(self, new_listing_url: Optional[str] = None, limit: int = 100) -> tuple[int, int]:
        """Return estimated active listing usage as (active_count, limit).

        Uses locally cached listing YAMLs for speed and optionally accounts for a
        freshly created listing URL that may not yet be present in cache.
        """
        listing_ids = self.get_cached_active_listing_ids()
        new_id = self._extract_listing_id(new_listing_url or "")
        if new_id and new_id not in listing_ids:
            listing_ids.add(new_id)
        return len(listing_ids), limit

    def _scan_local_yaml_shipping_types(self) -> dict[str, str]:
        """Scan ads/ and downloaded-ads/ for listing_id → shipping_type from YAML files."""
        result: dict[str, str] = {}
        for base_dir in (self.ads_dir, self.downloaded_ads_dir):
            if not base_dir.exists():
                continue
            for yaml_path in base_dir.rglob("*.yaml"):
                try:
                    with open(yaml_path, encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    if not isinstance(data, dict):
                        continue
                    listing_id = data.get("id")
                    shipping_type = data.get("shipping_type")
                    if listing_id and shipping_type in ("PICKUP", "SHIPPING"):
                        result[str(listing_id)] = shipping_type
                except Exception:
                    continue
        return result

    def _parse_ad_yaml(self, yaml_path: Path) -> Optional[Listing]:
        """Parse an ad YAML file into a Listing model.

        Args:
            yaml_path: Path to the YAML file

        Returns:
            Listing model or None if invalid/inactive
        """
        try:
            with open(yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)

            if not data or not isinstance(data, dict):
                return None

            # Skip inactive ads
            if not data.get("active", True):
                return None

            # Skip ads without an ID (not yet published or local-only)
            listing_id = data.get("id")
            if not listing_id:
                return None

            # Estimate days remaining from created_on and republication_interval
            days_remaining = None
            created_dt: Optional[datetime] = None
            created_on = data.get("created_on")
            raw_interval = data.get("republication_interval")
            try:
                if raw_interval in (None, ""):
                    republication_interval = self._load_default_republication_interval()
                else:
                    republication_interval = int(raw_interval)
                if republication_interval <= 0:
                    republication_interval = self._load_default_republication_interval()
            except (TypeError, ValueError):
                republication_interval = self._load_default_republication_interval()
            if created_on:
                try:
                    if isinstance(created_on, str):
                        created_dt = datetime.fromisoformat(created_on.replace("Z", "+00:00"))
                    elif isinstance(created_on, datetime):
                        created_dt = created_on
                    else:
                        created_dt = None

                    if created_dt:
                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        now = datetime.now(timezone.utc)
                        elapsed = (now - created_dt).days
                        if elapsed <= republication_interval:
                            # Within first publication cycle – remaining time is known
                            days_remaining = max(0, republication_interval - elapsed)
                        else:
                            # Approximate the current cycle based on the initial created_on.
                            remainder = elapsed % republication_interval
                            days_remaining = 0 if remainder == 0 else max(0, republication_interval - remainder)
                except Exception as e:
                    logger.debug(f"Could not parse created_on date: {e}")

            price = data.get("price", 0) or 0
            price_type = data.get("price_type", "")
            if price_type == "GIVE_AWAY":
                price = 0.0

            shipping_type = data.get("shipping_type", "SHIPPING") or "SHIPPING"
            shipping_options_raw = data.get("shipping_options")
            shipping_options_str: Optional[str] = None
            if isinstance(shipping_options_raw, list):
                import json as _json
                shipping_options_str = _json.dumps(shipping_options_raw)
            elif isinstance(shipping_options_raw, str):
                shipping_options_str = shipping_options_raw

            return Listing(
                listing_id=str(listing_id),
                title=data.get("title", ""),
                description=data.get("description", ""),
                price=float(price),
                category=data.get("category", ""),
                status="active" if data.get("active", True) else "inactive",
                days_remaining=days_remaining,
                shipping_type=shipping_type,
                shipping_options=shipping_options_str,
                created_at=created_dt,
            )

        except Exception as e:
            logger.warning(f"Failed to parse YAML file {yaml_path}: {e}")
            return None

    async def delete_listing(self, listing_id: str) -> bool:
        """Delete a listing from Kleinanzeigen.

        The CLI's delete command finds ads by loading local YAML files whose 'id' matches.
        We create a minimal temporary YAML so the CLI can locate the listing by its numeric ID.

        Args:
            listing_id: The Kleinanzeigen listing ID

        Returns:
            True if deletion was successful
        """
        logger.info(f"Deleting listing {listing_id}")

        # Create a minimal temp YAML so the CLI can find the listing by its numeric ID.
        # The CLI's load_ads() scans ads/ for YAML files and matches ad_cfg.id to the selector.
        temp_dir = self.ads_dir / f"_del_{listing_id}"
        temp_yaml = temp_dir / "ad.yaml"
        temp_dir.mkdir(parents=True, exist_ok=True)
        contact = self._load_contact_from_config()
        minimal_ad: dict = {
            "active": True,
            "type": "OFFER",
            "id": int(listing_id),
            "title": f"Zu loeschen {listing_id}",  # min_length=10 required
            "description": ".",
            "category": DEFAULT_CATEGORY,
            "price": 0,
            # Must be explicit: config.yaml's ad_defaults combine shipping_type
            # PICKUP with sell_directly true, which trips the bot's
            # "sell_directly requires shipping_type to be SHIPPING" validation
            # and aborts the delete run before the browser even starts.
            "shipping_type": "PICKUP",
            "sell_directly": False,
            "contact": contact,
        }
        with open(temp_yaml, "w", encoding="utf-8") as f:
            yaml.safe_dump(minimal_ad, f, allow_unicode=True, sort_keys=False)

        try:
            await self._pre_session_setup()
            returncode, stdout, stderr = await self._run_cli(
                "delete",
                "--ads", listing_id,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if returncode == 0:
            logger.info(f"Listing {listing_id} deleted via CLI")
            removed_yaml: list[str] = []
            removed_dirs: list[str] = []
            for yaml_path in self._get_listing_yaml_files():
                try:
                    with open(yaml_path, encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    if not data or str(data.get("id", "")).strip() != str(listing_id):
                        continue
                    yaml_path.unlink(missing_ok=True)
                    removed_yaml.append(str(yaml_path))
                    parent = yaml_path.parent
                    if parent.name.startswith("ad_") and (
                        parent.is_relative_to(self.ads_dir)
                        or parent.is_relative_to(self.downloaded_ads_dir)
                    ):
                        shutil.rmtree(parent, ignore_errors=True)
                        removed_dirs.append(str(parent))
                except Exception as e:
                    logger.warning(
                        f"Failed to inspect cached YAML {yaml_path} during deletion cleanup: {e}"
                    )
            for base_dir in (self.ads_dir, self.downloaded_ads_dir):
                for entry in base_dir.iterdir():
                    if not entry.is_dir():
                        continue
                    if not entry.name.startswith(f"ad_{listing_id}"):
                        continue
                    if str(entry) in removed_dirs:
                        continue
                    shutil.rmtree(entry, ignore_errors=True)
                    removed_dirs.append(str(entry))
            if removed_yaml or removed_dirs:
                logger.info(
                    "Deleted cached YAMLs/dirs for listing %s: yamls=%d dirs=%d",
                    listing_id,
                    len(removed_yaml),
                    len(removed_dirs),
                )
            else:
                logger.info("No cached YAMLs/dirs found for deleted listing %s", listing_id)
            return True
        else:
            error_msg = (stderr + "\n" + stdout).strip()
            logger.error(f"CLI delete failed (rc={returncode}): {error_msg}")
            raise KleinanzeigenError(
                f"Löschen fehlgeschlagen: {self._extract_bot_error(error_msg)}"
            )

    async def publish_draft(self, listing_id: str) -> bool:
        """Publish a draft listing.

        Args:
            listing_id: Local draft ID or Kleinanzeigen listing ID

        Returns:
            True if successful
        """
        yaml_path = self._find_yaml_by_listing_id(listing_id)
        if not yaml_path:
            logger.warning(f"No YAML found for listing {listing_id}")
            return False

        # Read the numeric Kleinanzeigen ID written back into YAML after initial publish
        try:
            with open(yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            numeric_id = str(data.get("id", "")).strip()
        except Exception as e:
            logger.error(f"Could not read id from YAML {yaml_path}: {e}")
            raise KleinanzeigenError("Kleinanzeigen-ID nicht in YAML gefunden")

        if not numeric_id or numeric_id == "None":
            raise KleinanzeigenError("Keine Kleinanzeigen-ID in YAML – Anzeige wurde noch nicht veröffentlicht")

        await self._pre_session_setup()
        returncode, stdout, stderr = await self._run_cli(
            "publish",
            "--ads", numeric_id,
        )

        if returncode == 0:
            logger.info(f"Draft {listing_id} (Kleinanzeigen ID {numeric_id}) published")
            return True
        else:
            logger.error(f"Publish failed (rc={returncode}): {stderr}")
            raise KleinanzeigenError(f"Veröffentlichung fehlgeschlagen: {stderr[:200]}")

    async def renew_listing(
        self,
        listing_id: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        price: Optional[float] = None,
    ) -> bool:
        """Renew an already published listing by Kleinanzeigen numeric ID."""
        self._ensure_listing_config_for_publish(
            listing_id,
            title=title,
            description=description,
            price=price,
        )
        await self._pre_session_setup()
        returncode, stdout, stderr = await self._run_cli(
            "publish",
            "--ads", str(listing_id),
            timeout=300,
        )
        combined_output = (stdout + "\n" + stderr).lower()
        if returncode == 0 and "no new/outdated ads found" not in combined_output:
            logger.info(f"Listing {listing_id} renewed via CLI")
            return True

        error_msg = (stderr + "\n" + stdout).strip()
        logger.error(f"Renew failed for listing {listing_id} (rc={returncode}): {error_msg}")
        raise KleinanzeigenError(f"Erneuerung fehlgeschlagen: {error_msg[-600:]}")

    async def _suggest_alternative_categories(
        self, title: str, description: str, exclude_category: str, n: int = 3
    ) -> list[str]:
        """Use LLM to suggest alternative categories, excluding the given one."""
        category_list_str = "\n".join(_CATEGORY_LIST)
        llm = get_text_llm(temperature=0.3)
        prompt = (
            f"Wähle die {n} besten Kategorien für diesen Artikel aus der folgenden Liste.\n"
            f"Schließe die Kategorie '{exclude_category}' aus.\n"
            f"Antworte NUR mit den Kategorienamen, einen pro Zeile, ohne Nummerierung oder Erklärung.\n\n"
            f"Verfügbare Kategorien:\n{category_list_str}\n\n"
            f"Artikel-Titel: {title}\nArtikel-Beschreibung: {description}"
        )
        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            lines = str(response.content).strip().splitlines()
            valid = [
                line.strip()
                for line in lines
                if line.strip() in _CATEGORY_LIST and line.strip() != exclude_category
            ]
            return valid[:n]
        except Exception as e:
            logger.warning("Category suggestion LLM call failed: %s", e)
            return []

    async def _publish_new_ad(
        self, new_ad_dir: Path, new_yaml: Path, listing_id: str
    ) -> tuple[str, Optional[str]]:
        """Run 'publish --ads new', retry with alternative categories on dialog failure.

        Returns (url, warning_message). warning_message is set when PICKUP fallback was used.
        """
        await self._pre_session_setup()
        returncode, stdout, stderr = await self._run_cli(
            "--verbose", "publish", "--ads", "new", timeout=PUBLISH_NEW_TIMEOUT
        )
        combined_output = stdout + stderr

        def _shipping_dialog_failed(output: str) -> bool:
            """True when publishing died in the shipping step of the ad form.

            Besides the 'Andere Versandmethoden' route the shipping section can
            fail to render entirely (category-specific), which surfaces as a
            timeout on the 'ad-shipping-options' element.
            """
            return "Andere Versandmethoden" in output or "ad-shipping-options" in output

        pickup_warning: Optional[str] = None

        # If shipping dialog failed, try 3 alternative categories, then fall back to PICKUP
        if (returncode != 0 or re.search(r"published 0 ads", combined_output, re.IGNORECASE)) and _shipping_dialog_failed(combined_output):
            with open(new_yaml, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            original_cat = data.get("category", "")
            ad_title = data.get("title", "")
            ad_desc = data.get("description", "")

            logger.warning(
                "Shipping dialog 'Andere Versandmethoden' failed for listing %s (category '%s'); "
                "fetching LLM alternative categories.",
                listing_id, original_cat,
            )

            alt_categories = await self._suggest_alternative_categories(
                ad_title, ad_desc, original_cat, n=3
            )

            published = False
            for i, alt_cat in enumerate(alt_categories):
                logger.warning(
                    "Shipping dialog retry %d/3 for listing %s with category '%s'",
                    i + 1, listing_id, alt_cat,
                )
                data["category"] = canonical_category(alt_cat)
                data.pop("special_attributes", None)
                with open(new_yaml, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
                await self._pre_session_setup()
                returncode, stdout, stderr = await self._run_cli(
                    "--verbose", "publish", "--ads", "new", timeout=PUBLISH_NEW_TIMEOUT
                )
                combined_output = stdout + stderr
                if returncode == 0 and not re.search(r"published 0 ads", combined_output, re.IGNORECASE):
                    published = True
                    break

            if not published:
                logger.warning(
                    "All category retries failed for listing %s; falling back to PICKUP", listing_id
                )
                data["category"] = original_cat
                data["shipping_type"] = "PICKUP"
                data["sell_directly"] = False
                data.pop("shipping_options", None)
                data.pop("special_attributes", None)
                with open(new_yaml, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
                await self._pre_session_setup()
                returncode, stdout, stderr = await self._run_cli(
                    "--verbose", "publish", "--ads", "new", timeout=PUBLISH_NEW_TIMEOUT
                )
                combined_output = stdout + stderr
                pickup_warning = (
                    f"⚠️ Anzeige '{ad_title}' wurde mit 'Nur Abholung' veröffentlicht, "
                    f"da der Versand-Dialog für Kategorie '{original_cat}' nicht funktioniert. "
                    f"Bitte manuell Versandoptionen aktualisieren."
                )

        # Special-attribute retry: if a category-specific attribute field can't be found, strip it and retry
        if (returncode != 0 or re.search(r"published 0 ads", combined_output, re.IGNORECASE)) \
                and re.search(r"Failed to set attribute '(.+?)'", combined_output):
            attr_match = re.search(r"Failed to set attribute '(.+?)'", combined_output)
            logger.warning(
                "Special attribute '%s' not found for listing %s; stripping special_attributes and retrying.",
                attr_match.group(1) if attr_match else "unknown", listing_id,
            )
            with open(new_yaml, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            data.pop("special_attributes", None)
            with open(new_yaml, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            await self._pre_session_setup()
            returncode, stdout, stderr = await self._run_cli(
                "--verbose", "publish", "--ads", "new", timeout=PUBLISH_NEW_TIMEOUT
            )
            combined_output = stdout + stderr

        # Stale page retry: if pstad-descrptn specifically never rendered, retry once with fresh lock cleanup
        # NOTE: Do NOT clear the full browser profile here — that destroys auth cookies and triggers 2FA.
        if (returncode != 0 or re.search(r"published 0 ads", combined_output, re.IGNORECASE)) \
                and re.search(r"No HTML element found with ID .pstad-descrptn.", combined_output):
            logger.warning(
                "Stale browser page (pstad-descrptn) for listing %s; retrying once.", listing_id,
            )
            await self._pre_session_setup()
            returncode, stdout, stderr = await self._run_cli(
                "--verbose", "publish", "--ads", "new", timeout=PUBLISH_NEW_TIMEOUT
            )
            combined_output = stdout + stderr

        if returncode != 0:
            # If the publish bot internally calls delete_ad and THAT times out, the publish
            # may have succeeded even though the CLI exits non-zero. Try to recover via YAML id.
            if "delete_ad" in combined_output or "meine-anzeigen" in combined_output:
                recovered_id = self._extract_listing_id_from_yaml(new_yaml)
                if recovered_id:
                    logger.warning(
                        "Publish CLI exited non-zero (likely delete_ad timeout) for listing %s, "
                        "but YAML id=%s found — treating as success. Old listing NOT deleted.",
                        listing_id, recovered_id,
                    )
                    shutil.rmtree(new_ad_dir, ignore_errors=True)
                    recovered_url = f"https://www.kleinanzeigen.de/s-anzeige/id/{recovered_id}"
                    return (
                        recovered_url,
                        f"⚠️ Alte Anzeige {listing_id} konnte nicht automatisch gelöscht werden "
                        "(Timeout beim Laden von 'Meine Anzeigen'). Bitte manuell entfernen.",
                    )
            shutil.rmtree(new_ad_dir, ignore_errors=True)
            raise KleinanzeigenError(f"Veröffentlichung fehlgeschlagen: {combined_output[-600:]}")

        # Submit-boundary: bot clicked submit but couldn't confirm success (stale page / renamed DOM).
        # The bot writes the new ID back into the YAML on success — check that first.
        if re.search(r"submission may have succeeded|reached submit boundary", combined_output, re.IGNORECASE):
            new_id = self._extract_listing_id_from_yaml(new_yaml)
            if new_id:
                # Bot confirmed success via YAML — treat as normal publish
                logger.info(
                    "Submit boundary for listing %s — confirmed via YAML id=%s. Deleting old.",
                    listing_id, new_id,
                )
                new_url = f"https://www.kleinanzeigen.de/s-anzeige/id/{new_id}"
                shutil.rmtree(new_ad_dir, ignore_errors=True)
                await self.delete_listing(listing_id)
                return new_url, pickup_warning
            # YAML has no id — try auto-recovery: fetch live listings, match by title.
            logger.info(
                "Submit boundary for listing %s — no YAML id. Attempting auto-recovery via download.",
                listing_id,
            )
            try:
                with open(new_yaml, encoding="utf-8") as _f:
                    _yaml_data = yaml.safe_load(_f) or {}
                _ad_title = _yaml_data.get("title", "")
                await asyncio.sleep(8)  # wait for the new ad to appear on the server
                live_listings = await self.get_active_listings(refresh=True)
                matched = [
                    lv for lv in live_listings
                    if lv.title == _ad_title and lv.listing_id != listing_id
                ]
                if matched:
                    recovered_id = matched[0].listing_id
                    recovered_url = f"https://www.kleinanzeigen.de/s-anzeige/id/{recovered_id}"
                    logger.info(
                        "Submit boundary auto-recovery succeeded for listing %s — new id=%s.",
                        listing_id, recovered_id,
                    )
                    shutil.rmtree(new_ad_dir, ignore_errors=True)
                    await self.delete_listing(listing_id)
                    return recovered_url, pickup_warning
                logger.warning(
                    "Submit boundary auto-recovery: no matching live ad found for title '%s' (listing %s).",
                    _ad_title, listing_id,
                )
            except Exception as recovery_err:
                logger.warning(
                    "Submit boundary auto-recovery failed for listing %s: %s",
                    listing_id, recovery_err,
                )
            shutil.rmtree(new_ad_dir, ignore_errors=True)
            return (
                "Anzeige wurde wahrscheinlich veröffentlicht, konnte aber nicht bestätigt werden. "
                "Bitte unter 'Meine Anzeigen' prüfen.",
                f"⚠️ Alte Anzeige {listing_id} wurde NICHT gelöscht. Bitte manuell entfernen falls die neue Anzeige live ist.",
            )

        if re.search(r"published 0 ads", combined_output, re.IGNORECASE):
            shutil.rmtree(new_ad_dir, ignore_errors=True)
            bot_error = self._extract_bot_error(combined_output)
            logger.error(
                "Publish failed (0 ads published) for %s. Bot error: %s\nFull stderr tail: %s",
                listing_id, bot_error, combined_output[-2000:],
            )
            raise KleinanzeigenError(
                f"Veröffentlichung fehlgeschlagen: {bot_error}\n(Details: /logs)"
            )

        new_url = self._extract_listing_url(combined_output)
        new_id = None
        if not new_url:
            new_id = self._extract_listing_id(combined_output)
            if new_id:
                new_url = f"https://www.kleinanzeigen.de/s-anzeige/id/{new_id}"
        if not new_url:
            new_id = self._extract_listing_id_from_yaml(new_yaml)
            if new_id:
                new_url = f"https://www.kleinanzeigen.de/s-anzeige/id/{new_id}"

        if not new_url and not new_id:
            logger.error(
                "Publish succeeded but no new listing id/url found; keeping old listing %s. "
                "combined_output=%s",
                listing_id,
                combined_output[-800:],
            )
            raise KleinanzeigenError(
                "Neue Anzeige konnte nicht bestätigt werden. Alte Anzeige wurde nicht gelöscht."
            )

        shutil.rmtree(new_ad_dir, ignore_errors=True)
        await self.delete_listing(listing_id)
        return new_url, pickup_warning

    async def republish_with_optimization(
        self,
        listing_id: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        price: Optional[float] = None,
        chat_id: int = 0,
    ) -> tuple[str, Optional[str]]:
        """Republish an existing listing as new, preserving all original settings.

        Copies the original ad directory (images, shipping config, category, etc.),
        strips the 'id' field so kleinanzeigen-bot treats it as a new ad, applies
        text overrides, publishes, then deletes the old listing.

        Returns (new_url, warning_message). warning_message is set when PICKUP fallback was used.
        """
        source_yaml = self._find_yaml_by_listing_id(listing_id)
        if source_yaml is None:
            # No local YAML — build one from DB data + available image directory
            from db.repository import listing_repo
            import json as _json
            row = await listing_repo.get(listing_id)
            if row is None:
                raise KleinanzeigenError(
                    f"Keine lokale Anzeigen-Konfiguration und kein DB-Eintrag für ID {listing_id}."
                )

            ad_id = str(uuid.uuid4())[:8]
            new_ad_dir = self.ads_dir / f"ad_{ad_id}"
            new_ad_dir.mkdir(parents=True, exist_ok=True)

            # Copy images from the source directory if found
            source_img_dir = self._find_ad_dir_by_listing_id(listing_id)
            if source_img_dir:
                for img_file in source_img_dir.iterdir():
                    if img_file.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                        shutil.copy2(img_file, new_ad_dir / img_file.name)

            # Collect copied image filenames for YAML (relative to YAML location = new_ad_dir)
            image_files = sorted(
                f for f in new_ad_dir.iterdir()
                if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
            )
            images_list = [img.name for img in image_files]

            # Read shipping config from source YAML to preserve it unchanged
            source_yaml_data: dict = {}
            if source_img_dir:
                for f in source_img_dir.iterdir():
                    if f.suffix == ".yaml":
                        try:
                            with open(f, encoding="utf-8") as _f:
                                source_yaml_data = yaml.safe_load(_f) or {}
                        except Exception:
                            pass
                        break

            shipping_type = str(source_yaml_data.get("shipping_type") or row["shipping_type"] or "SHIPPING")
            ad_price = price if price is not None else float(row["price"])

            contact = self._load_contact_from_config()
            ad_data: dict = {
                "active": True,
                "type": "OFFER",
                "title": (title or str(row["title"]))[:50],
                "description": (description or str(row["description"] or ""))[:4000],
                "category": canonical_category(str(row["category"] or "")),
                "price": int(round(ad_price)),
                "price_type": "NEGOTIABLE",
                "auto_price_reduction": {"enabled": False},
                "shipping_type": shipping_type,
                "sell_directly": shipping_type == "SHIPPING",
                "republication_interval": self._load_default_republication_interval(),
                "contact": contact,
            }
            if shipping_type == "SHIPPING":
                raw_opts = source_yaml_data.get("shipping_options") or []
                ad_data["shipping_options"] = self._normalize_shipping(raw_opts, ad_price)
            if source_yaml_data.get("special_attributes"):
                ad_data["special_attributes"] = source_yaml_data["special_attributes"]
            if images_list:
                ad_data["images"] = images_list

            new_yaml = new_ad_dir / "ad.yaml"
            with open(new_yaml, "w", encoding="utf-8") as f:
                yaml.safe_dump(ad_data, f, allow_unicode=True, sort_keys=False)

            # Remove other ad_ dirs so publish --ads new only picks up ours
            for existing_dir in self.ads_dir.iterdir():
                if existing_dir.is_dir() and existing_dir.name.startswith("ad_") and existing_dir != new_ad_dir:
                    shutil.rmtree(existing_dir, ignore_errors=True)

            return await self._publish_new_ad(new_ad_dir, new_yaml, listing_id)

        source_dir = source_yaml.parent
        ad_id = str(uuid.uuid4())[:8]
        new_ad_dir = self.ads_dir / f"ad_{ad_id}"

        # Copy the entire original directory (YAML + images subdirectory)
        shutil.copytree(source_dir, new_ad_dir)
        new_yaml = new_ad_dir / "ad.yaml"
        if not new_yaml.exists():
            # Handle case where original yaml has a different name
            for f in new_ad_dir.rglob("*.yaml"):
                f.rename(new_yaml)
                break

        # Strip the 'id' field so it is treated as a brand-new listing.
        # Normalize shipping_options using the same size-based logic as new listings,
        # and ensure sell_directly is True when shipping is enabled.
        with open(new_yaml, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data.pop("id", None)
        if data.get("shipping_type") == "SHIPPING":
            raw_opts = data.get("shipping_options") or []
            ad_price = float(data.get("price") or 0)
            data["shipping_options"] = self._normalize_shipping(raw_opts, ad_price)
            data["sell_directly"] = True
        data = self._sanitize_ad_yaml(data)
        with open(new_yaml, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

        # Apply optimizer overrides (title / description / price)
        self._apply_overrides_to_yaml(new_yaml, title=title, description=description, price=price)

        # Remove all other ad_ directories so 'publish --ads new' only picks up ours
        for existing_dir in self.ads_dir.iterdir():
            if existing_dir.is_dir() and existing_dir.name.startswith("ad_") and existing_dir != new_ad_dir:
                shutil.rmtree(existing_dir, ignore_errors=True)

        return await self._publish_new_ad(new_ad_dir, new_yaml, listing_id)

    async def get_listing_details(self, listing_id: str) -> Optional[Listing]:
        """Get details of a specific listing.

        Args:
            listing_id: The Kleinanzeigen listing ID

        Returns:
            Listing model or None if not found
        """
        yaml_path = self._find_yaml_by_listing_id(listing_id)
        if yaml_path:
            return self._parse_ad_yaml(yaml_path)

        # Try to find in all YAML files
        for yaml_file in self.ads_dir.rglob("*.yaml"):
            listing = self._parse_ad_yaml(yaml_file)
            if listing and listing.listing_id == listing_id:
                return listing

        return None

    def _find_yaml_by_listing_id(self, listing_id: str) -> Optional[Path]:
        """Find a YAML file that contains the given listing ID."""
        for yaml_file in self._get_listing_yaml_files():
            try:
                with open(yaml_file, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if data and str(data.get("id", "")) == str(listing_id):
                    return yaml_file
            except Exception:
                continue
        return None

    async def download_single_listing(self, listing_id: str) -> Optional[Listing]:
        """Download a single listing by ID using 'kleinanzeigen-bot download --ads=<id>'.

        Returns the parsed Listing if the download succeeded and created a YAML,
        otherwise None.
        """
        await self._pre_session_setup()
        returncode, stdout, stderr = await self._run_cli(
            "download", "--ads", listing_id, timeout=DOWNLOAD_SINGLE_TIMEOUT
        )
        combined = (stdout + stderr).lower()
        if returncode != 0 and "successfully" not in combined:
            logger.warning(f"Single-listing download failed for {listing_id} (rc={returncode})")
            return None

        yaml_path = self._find_yaml_by_listing_id(listing_id)
        if yaml_path:
            return self._parse_ad_yaml(yaml_path)
        return None

    def _find_ad_dir_by_listing_id(self, listing_id: str) -> Optional[Path]:
        """Find an ad directory matching listing_id even without a YAML file.

        Searches for directories named `ad_<listing_id>*` in both
        `downloaded-ads/` and `ads/`. Returns the first match, or None.
        """
        prefix = f"ad_{listing_id}"
        for base_dir in (self.downloaded_ads_dir, self.ads_dir):
            if not base_dir.exists():
                continue
            for entry in base_dir.iterdir():
                if entry.is_dir() and entry.name.startswith(prefix):
                    return entry
        return None

    def _ensure_listing_config_for_publish(
        self,
        listing_id: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        price: Optional[float] = None,
    ) -> None:
        """Ensure the listing YAML exists under ads/ so publish --ads <id> can target it."""
        for yaml_file in self.ads_dir.rglob("*.yaml"):
            try:
                with open(yaml_file, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if data and str(data.get("id", "")) == str(listing_id):
                    return
            except Exception:
                continue

        source_yaml = self._find_yaml_by_listing_id(listing_id)
        if source_yaml is None:
            raise KleinanzeigenError(f"Keine lokale Anzeigen-Konfiguration für ID {listing_id} gefunden")

        target_dir = self.ads_dir / f"ad_{listing_id}"
        source_dir = source_yaml.parent
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_dir, target_dir)
        target_yaml = target_dir / "ad.yaml"
        if source_yaml.name == "ad.yaml":
            self._apply_overrides_to_yaml(
                target_yaml,
                title=title,
                description=description,
                price=price,
            )
            logger.info(f"Seeded ads config for renewal: {target_yaml}")
            return

        shutil.copy2(source_yaml, target_yaml)
        self._apply_overrides_to_yaml(
            target_yaml,
            title=title,
            description=description,
            price=price,
        )
        logger.info(f"Seeded ads config for renewal: {target_yaml}")

    def _apply_overrides_to_yaml(
        self,
        yaml_path: Path,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        price: Optional[float] = None,
    ) -> None:
        """Apply optional field overrides before publishing."""
        if title is None and description is None and price is None:
            return

        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise KleinanzeigenError(f"Ungültige YAML-Struktur in {yaml_path}")

        if title is not None:
            normalized_title = title.strip()
            if normalized_title:
                data["title"] = normalized_title[:50]
        if description is not None:
            data["description"] = description.strip()
        if price is not None:
            normalized_price = max(0.0, float(price))
            data["price"] = int(round(normalized_price))
            if normalized_price < 1:
                data["price_type"] = "GIVE_AWAY"
            elif data.get("price_type") == "GIVE_AWAY":
                data["price_type"] = "FIXED"

        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    def ensure_config_exists(self) -> bool:
        """Check if the kleinanzeigen-bot config file exists.

        Returns:
            True if config is ready to use
        """
        if not self.config_file.exists():
            logger.warning(
                f"Kleinanzeigen config not found at {self.config_file}. "
                "Create it with: kleinanzeigen-bot create-config"
            )
            return False
        return True
