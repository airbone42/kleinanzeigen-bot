"""Restore lost ad images from the original Telegram photos and republish.

Usage (inside the bot container):
    python -m scripts.restore_images <listing_id>:<draft_id>[:<category>] [...] [--dry-run]

Fetches the draft's Telegram photos into image-archive/<listing_id>/, then
republishes the listing; the republish picks the images up from the archive.
"""
import argparse
import asyncio
import logging

import yaml
from telegram import Bot

from config.settings import settings
from db.database import init_database
from db.repository import draft_repo, listing_repo
from services.kleinanzeigen import KleinanzeigenService

logger = logging.getLogger("restore_images")


async def fetch_draft_images(bot: Bot, service: KleinanzeigenService, listing_id: str, draft_id: int) -> int:
    rows = await draft_repo.get_images(draft_id)
    target = service.image_archive_dir / listing_id
    target.mkdir(parents=True, exist_ok=True)
    for row in rows:
        tg_file = await bot.get_file(str(row["file_id"]))
        await tg_file.download_to_drive(target / f"draft{draft_id}_{int(row['position']):02d}.jpg")
    return len(rows)


def set_category(service: KleinanzeigenService, listing_id: str, category: str) -> None:
    """Override the category; attributes of the old category would not exist in the new one."""
    yaml_path = service._find_yaml_by_listing_id(listing_id)
    if yaml_path is None:
        raise RuntimeError(f"No YAML for listing {listing_id}")
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data["category"] = category
    data.pop("special_attributes", None)
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    logger.info("Listing %s: category set to %s", listing_id, category)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pairs", nargs="+", help="<listing_id>:<draft_id>[:<category>]")
    parser.add_argument("--dry-run", action="store_true", help="only fetch images into the archive")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # httpx logs request URLs, which contain the bot token
    logging.getLogger("httpx").setLevel(logging.WARNING)
    await init_database()
    service = KleinanzeigenService()
    bot = Bot(settings.telegram_bot_token)

    for pair in args.pairs:
        listing_id, draft_id, *category = pair.split(":", 2)
        try:
            count = await fetch_draft_images(bot, service, listing_id, int(draft_id))
            logger.info("Listing %s: fetched %d image(s) from draft %s", listing_id, count, draft_id)
            if args.dry_run or not count:
                continue
            listing = await service.download_single_listing(listing_id)
            if listing is None:
                logger.error("Listing %s: download failed, skipping republish", listing_id)
                continue
            if category:
                set_category(service, listing_id, category[0])
            new_url, warning = await service.republish_with_optimization(listing_id)
            await listing_repo.update_status(listing_id, "replaced")
            logger.info("Listing %s republished: %s %s", listing_id, new_url, warning or "")
        except Exception as e:
            logger.error("Listing %s failed: %s", listing_id, e, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
