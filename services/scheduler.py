"""APScheduler setup for the daily listing check."""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application

from config.settings import settings

logger = logging.getLogger(__name__)


def setup_scheduler(app: Application) -> AsyncIOScheduler:
    """Configure and return an AsyncIOScheduler for the daily listing check.

    The scheduler runs the daily check at the configured hour/minute.

    Args:
        app: Telegram bot Application instance

    Returns:
        Configured but not-yet-started scheduler
    """
    scheduler = AsyncIOScheduler(timezone="Europe/Berlin")

    trigger = CronTrigger(
        hour=settings.daily_check_hour,
        minute=settings.daily_check_minute,
        timezone="Europe/Berlin",
    )

    async def _run_daily_check() -> None:
        """Job callback that runs the daily listing check."""
        from bot.handlers.daily_check import scheduled_daily_check
        from telegram.ext import CallbackContext

        context = CallbackContext(app)  # type: ignore[arg-type]
        await scheduled_daily_check(context)

    scheduler.add_job(
        _run_daily_check,
        trigger=trigger,
        id="daily_listing_check",
        name="Daily Kleinanzeigen listing check",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=7200,  # Allow 2 hour grace for missed jobs (covers DST transitions)
    )

    logger.info(
        f"Scheduler configured: daily check at "
        f"{settings.daily_check_hour:02d}:{settings.daily_check_minute:02d} (Europe/Berlin)"
    )

    return scheduler
