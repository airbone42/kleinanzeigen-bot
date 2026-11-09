"""Entry point for the Kleinanzeigen Telegram Bot."""
import asyncio
import logging
import os
import shutil
import signal
import socket
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


# --no-sandbox alone does not disable the separately sandboxed network service, which
# made fetch() fail inside Docker (ExceptionDetails: Failed to fetch), hence the last two.
SANDBOX_FLAGS = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-network-service-sandbox",
    "--disable-features=NetworkServiceSandbox",
)
CHROMIUM_CANDIDATES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")


def _find_chromium() -> Optional[str]:
    """Locate a system Chromium binary (CHROMIUM_PATH wins over PATH lookup)."""
    if settings.chromium_path:
        return settings.chromium_path if os.path.exists(settings.chromium_path) else None
    for name in CHROMIUM_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _clear_stale_profile_lock(profile_dir: "os.PathLike[str]") -> None:
    """Remove Chromium's Singleton* lock files if they belong to a dead or foreign process.

    Chromium refuses a profile whose SingletonLock (symlink "<hostname>-<pid>") points to
    another host ("profile in use on another computer") and nodriver then only reports
    "Failed to connect to browser". Happens after moving the profile (Docker VM -> CT 202)
    or after a crash. Runs at bot start, before any browser of this bot is up.
    """
    lock = os.path.join(profile_dir, "SingletonLock")
    try:
        target = os.readlink(lock)
    except OSError:
        return  # no lock
    host, _, pid = target.rpartition("-")
    alive = False
    if host == socket.gethostname() and pid.isdigit():
        try:
            os.kill(int(pid), 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True  # exists, owned by someone else
    if alive:
        return
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            os.unlink(os.path.join(profile_dir, name))
        except FileNotFoundError:
            pass
    logger.warning(f"Removed stale Chromium profile lock ({target}) in {profile_dir}")


def _setup_browser() -> None:
    """Configure kleinanzeigen-bot to use a Chromium binary.

    kleinanzeigen-bot uses nodriver (Chrome DevTools Protocol) and needs a real
    Chrome/Chromium binary: CHROMIUM_PATH or the first one in PATH, in CT 202 and in the
    Docker image the Debian package `chromium` (security updates via apt, visible to
    secwatch). Playwright's bundled Chromium is no longer used: it stays frozen at the
    Playwright release (Vikunja #352). The path goes into kleinanzeigen-config/config.yaml.
    """
    import yaml

    logger.info("Setting up browser for kleinanzeigen-bot...")
    try:
        chromium_path = _find_chromium()
        if not chromium_path:
            logger.error("No Chromium found (CHROMIUM_PATH or PATH), install the package chromium")
            return
        logger.info(f"Using Chromium at: {chromium_path}")

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

        # Browser flags (idempotent – only added if missing).
        # kleinanzeigen-bot explicitly handles --no-sandbox (web_scraping_mixin.py:563).
        args: list[str] = browser_cfg.get("arguments", [])
        # Strip flags that are known-bad or obsolete:
        # - --headless: non-headless is required for the login flow (Cloudflare detection)
        # - --enable-features=NetworkServiceInProcess: removed in Chrome 93+, conflicts with
        #   --disable-network-service-sandbox which is the correct modern fix
        _strip = {"--headless", "--enable-features=NetworkServiceInProcess", "--incognito"}
        args = [a for a in args if a not in _strip]
        # Chrome refuses its sandbox as root, so sandbox flags are only set when running
        # as root (Docker). As an unprivileged service user (CT 202) the sandbox stays on
        # and stale flags are removed. os.geteuid does not exist on Windows.
        if getattr(os, "geteuid", lambda: -1)() == 0:
            logger.warning("Running as root: Chromium sandbox disabled")
            args.extend(flag for flag in SANDBOX_FLAGS if flag not in args)
        else:
            args = [a for a in args if a not in SANDBOX_FLAGS]
        for flag in [
            "--disable-gpu",
            "--disable-dev-shm-usage",
            # Suppress the navigator.webdriver flag that Cloudflare / bot-detection
            # services check; prevents the login-state selector timeout caused by a
            # challenge or GDPR-consent page being served instead of the real page.
            "--disable-blink-features=AutomationControlled",
        ]:
            if flag not in args:
                args.append(flag)
        browser_cfg["arguments"] = args

        # Persist browser profile between restarts (uses setdefault so user can override in config.yaml)
        profile_dir = settings.kleinanzeigen_config_path.resolve() / "browser-profile"
        browser_cfg["user_data_dir"] = str(profile_dir)
        _clear_stale_profile_lock(profile_dir)

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
    from bot.handlers.vinted import (
        vinted_login_handler,
        vinted_retry_handler,
        vinted_status_handler,
        vinted_sync_handler,
    )

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
    app.add_handler(CommandHandler("vinted", vinted_status_handler, filters=user_f))
    app.add_handler(CommandHandler("vintedsync", vinted_sync_handler, filters=user_f))
    app.add_handler(CommandHandler("vintedlogin", vinted_login_handler, filters=user_f))
    app.add_handler(CommandHandler("vintedretry", vinted_retry_handler, filters=user_f))

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
