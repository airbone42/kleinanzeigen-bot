"""Entry point for the Kleinanzeigen Telegram Bot."""
import asyncio
import logging
import os
import signal
import subprocess
import sys
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from config.settings import settings
from db.database import init_database
from bot.handlers.start import start_handler, help_handler

logger = logging.getLogger(__name__)


def _setup_browser() -> None:
    """Install Playwright Chromium and configure kleinanzeigen-bot to use it.

    kleinanzeigen-bot uses nodriver (Chrome DevTools Protocol) and needs a real
    Chrome/Chromium binary. We use Playwright's managed Chromium as the binary source,
    then write its path into kleinanzeigen-config/config.yaml so nodriver finds it.
    """
    import yaml

    logger.info("Setting up browser for kleinanzeigen-bot...")
    try:
        # Step 1: install/update Playwright Chromium binary (idempotent)
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            logger.error(f"playwright install chromium failed: {result.stderr[:500]}")
            return

        # Step 2: get the Chromium executable path via a subprocess (sync_playwright()
        # cannot be called inside an asyncio loop, so we use a child process)
        path_result = subprocess.run(
            [sys.executable, "-c",
             "from playwright.sync_api import sync_playwright\n"
             "with sync_playwright() as pw: print(pw.chromium.executable_path)"],
            capture_output=True, text=True, timeout=30,
        )
        chromium_path = path_result.stdout.strip()

        if not chromium_path or not os.path.exists(chromium_path):
            logger.error(f"Playwright Chromium binary not found at: {chromium_path}")
            return

        logger.info(f"Playwright Chromium ready at: {chromium_path}")

        # Step 3: write binary_location + headless args into kleinanzeigen config.yaml
        config_file = settings.kleinanzeigen_config_path / "config.yaml"
        if not config_file.exists():
            logger.warning(f"kleinanzeigen config.yaml not found at {config_file}, skipping browser config")
            return

        with open(config_file, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        browser_cfg = cfg.setdefault("browser", {})
        browser_cfg["binary_location"] = chromium_path
        # CRITICAL: kleinanzeigen-bot defaults to use_private_window: true, which adds
        # --incognito and prevents cookie persistence → GDPR overlay → login timeout.
        browser_cfg["use_private_window"] = False

        # Docker/root-safe flags (idempotent – only added if missing).
        # kleinanzeigen-bot explicitly handles --no-sandbox (web_scraping_mixin.py:563).
        args: list[str] = browser_cfg.get("arguments", [])
        # Strip flags that are known-bad or obsolete:
        # - --headless: non-headless is required for the login flow (Cloudflare detection)
        # - --enable-features=NetworkServiceInProcess: removed in Chrome 93+, conflicts with
        #   --disable-network-service-sandbox which is the correct modern fix
        _strip = {"--headless", "--enable-features=NetworkServiceInProcess", "--incognito"}
        args = [a for a in args if a not in _strip]
        for flag in [
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            # The network service runs in a separate sandboxed process; --no-sandbox
            # alone does not disable it, causing fetch() to fail inside Docker
            # (ExceptionDetails: Failed to fetch).
            "--disable-network-service-sandbox",
            # Belt-and-suspenders: also disable via feature flag for Chrome 136+
            "--disable-features=NetworkServiceSandbox",
            # Suppress the navigator.webdriver flag that Cloudflare / bot-detection
            # services check; prevents the login-state selector timeout caused by a
            # challenge or GDPR-consent page being served instead of the real page.
            "--disable-blink-features=AutomationControlled",
        ]:
            if flag not in args:
                args.append(flag)
        browser_cfg["arguments"] = args

        # Persist browser profile between restarts (uses setdefault so user can override in config.yaml)
        browser_cfg["user_data_dir"] = str(settings.kleinanzeigen_config_path.resolve() / "browser-profile")

        # Page loads on kleinanzeigen.de take ~40s in this container (headless Chromium,
        # cold cache); the upstream default page_load timeout of 15s makes login fail with
        # "Page did not finish loading within 15.0 seconds." setdefault → user can override.
        timeouts_cfg = cfg.setdefault("timeouts", {})
        timeouts_cfg.setdefault("page_load", 90)
        timeouts_cfg.setdefault("login_detection", 20)

        # Clear stale ad directories from previous sessions. Without this, any ad_*
        # dirs without an id field would be re-published by publish --ads new,
        # causing duplicate or wrong listings.
        import shutil as _shutil
        ads_dir = settings.kleinanzeigen_config_path / "ads"
        if ads_dir.exists():
            for ad_subdir in ads_dir.iterdir():
                if ad_subdir.is_dir() and ad_subdir.name.startswith("ad_"):
                    _shutil.rmtree(ad_subdir, ignore_errors=True)
                    logger.info(f"Cleared stale ad directory: {ad_subdir}")

        with open(config_file, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        logger.info(f"Updated {config_file} with browser.binary_location")

        # Rotate the kleinanzeigen-bot log on startup (keep last 5 sessions)
        log_path = settings.kleinanzeigen_config_path / "kleinanzeigen_bot.log"
        if log_path.exists() and log_path.stat().st_size > 0:
            for i in range(4, 0, -1):
                src = log_path.with_suffix(f".log.{i}")
                dst = log_path.with_suffix(f".log.{i + 1}")
                if src.exists():
                    src.rename(dst)
            log_path.rename(log_path.with_suffix(".log.1"))
            logger.info(f"Rotated {log_path} → {log_path}.1")

    except Exception as e:
        logger.error(f"Browser setup failed: {e}", exc_info=True)


def _setup_langsmith() -> None:
    """Activate LangSmith tracing if configured."""
    api_key = settings.langchain_api_key or settings.langsmith_api_key
    if settings.langchain_tracing_v2.lower() == "true" and api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = api_key
        os.environ["LANGSMITH_API_KEY"] = api_key
        os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project
        os.environ["LANGSMITH_PROJECT"] = settings.langchain_project
        logger.info(f"LangSmith tracing enabled (project: {settings.langchain_project})")
    else:
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        os.environ["LANGSMITH_TRACING"] = "false"


def setup_logging() -> None:
    """Configure logging."""
    logging.basicConfig(
        level=settings.get_log_level(),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Reduce noise from external libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def user_filter(allowed_ids: list[int]) -> filters.BaseFilter:
    """Create a filter that only allows specific user IDs."""
    return filters.User(user_id=allowed_ids)


async def build_application() -> Application:
    """Build and configure the Telegram bot application."""
    # Import handlers here to avoid circular imports
    from bot.handlers.new_listing import photo_handler
    from bot.handlers.feedback import callback_query_handler, text_message_handler
    from bot.handlers.daily_check import daily_check_handler, weiter_handler, renew_handler
    from bot.handlers.discogs_sync import discogs_sync_handler
    from bot.handlers.diagnose import diagnose_handler
    from bot.handlers.logs import logs_handler

    app = Application.builder().token(settings.telegram_bot_token).build()

    allowed = settings.telegram_allowed_user_ids
    if not allowed:
        # Fail closed: an empty allow-list would otherwise expose the bot (and the
        # owner's OpenRouter credits / Kleinanzeigen account) to every Telegram user.
        raise RuntimeError(
            "TELEGRAM_ALLOWED_USER_IDS is empty. Refusing to start an open bot. "
            "Set it to a comma-separated list of allowed Telegram user IDs in .env."
        )

    user_f = user_filter(allowed)

    # Command handlers
    app.add_handler(CommandHandler("start", start_handler, filters=user_f))
    app.add_handler(CommandHandler("help", help_handler, filters=user_f))
    app.add_handler(CommandHandler("check", daily_check_handler, filters=user_f))
    app.add_handler(CommandHandler("weiter", weiter_handler, filters=user_f))
    app.add_handler(CommandHandler("renew", renew_handler, filters=user_f))
    app.add_handler(CommandHandler("discogssync", discogs_sync_handler, filters=user_f))
    app.add_handler(CommandHandler("diagnose", diagnose_handler, filters=user_f))
    app.add_handler(CommandHandler("logs", logs_handler, filters=user_f))

    # Photo handler (for new listings)
    app.add_handler(MessageHandler(filters.PHOTO & user_f, photo_handler))

    # Callback query handler (inline keyboard buttons)
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # Text message handler (for feedback and field editing)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & user_f, text_message_handler)
    )

    return app


async def run_bot() -> None:
    """Run the bot with scheduler."""
    setup_logging()
    _setup_browser()
    _setup_langsmith()
    logger.info("Starting Kleinanzeigen Telegram Bot")

    # Initialize database
    await init_database()

    # Build application
    app = await build_application()

    # Setup scheduler
    from services.scheduler import setup_scheduler
    scheduler = setup_scheduler(app)
    scheduler.start()

    logger.info("Bot started, listening for messages...")

    # Start bot
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        # Keep running until interrupted
        stop_event = asyncio.Event()

        def _signal_handler(sig: int, frame: object) -> None:
            logger.info(f"Received signal {sig}, initiating graceful shutdown...")
            stop_event.set()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        await stop_event.wait()

    finally:
        logger.info("Shutting down...")
        from bot.handlers.daily_check import cancel_all_tasks
        cancel_all_tasks()
        scheduler.shutdown(wait=False)
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("Shutdown complete")


async def _run_cli_diagnose() -> None:
    """Run kleinanzeigen-bot diagnose standalone — no Telegram bot started.

    Usage: python -m bot.main diagnose
    Useful in Docker to verify Chrome config without starting Telegram polling.
    """
    setup_logging()
    _setup_browser()
    from services.kleinanzeigen import KleinanzeigenService
    service = KleinanzeigenService()
    output = await service.diagnose()
    print(output)


def main() -> None:
    """Main entry point."""
    if len(sys.argv) > 1 and sys.argv[1] == "diagnose":
        asyncio.run(_run_cli_diagnose())
    else:
        asyncio.run(run_bot())


if __name__ == "__main__":
    main()
