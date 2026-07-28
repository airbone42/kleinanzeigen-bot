#!/usr/bin/env bash
# Standalone test script for the kleinanzeigen-bot dependency.
#
# Run from the project root on the host machine:
#   ./scripts/test_kleinanzeigen.sh [command]
#
# Commands:
#   diagnose  (default) – run browser/config diagnostics via bot.main
#   warmup    – run pre-session Playwright warmup only (GDPR + Cloudflare cookies)
#   publish   – run kleinanzeigen-bot publish --ads new directly
#   publish-warmed – run pre-session warmup then publish (tests the full wrapper path)
#   download  – run kleinanzeigen-bot download directly
#   logs      – tail the last 100 lines of kleinanzeigen_bot.log
#   shell     – open an interactive shell in the container
#
# You can also exec into the container manually and run commands directly:
#   docker exec -it kleinanzeigen-bot bash
#   python -m kleinanzeigen_bot --config /app/kleinanzeigen-config/config.yaml --verbose diagnose

set -euo pipefail

CONTAINER="kleinanzeigen-bot"
CMD="${1:-diagnose}"

# Use python -m kleinanzeigen_bot instead of the kleinanzeigen-bot script entry point
# to avoid PATH issues when running as a non-root user via docker exec.
KA_CMD="python -m kleinanzeigen_bot --config /app/kleinanzeigen-config/config.yaml"

_check_container() {
  if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "ERROR: Container '${CONTAINER}' is not running."
    echo "Start it with: docker compose up -d"
    exit 1
  fi
}

case "$CMD" in
  diagnose)
    _check_container
    echo "=== Running bot.main diagnose (includes browser setup + kleinanzeigen-bot diagnose) ==="
    docker exec -it "$CONTAINER" sh -c 'python -m bot.main diagnose'
    ;;
  warmup)
    _check_container
    echo "=== Running pre-session Playwright warmup — dumps page HTML to stdout ==="
    docker exec -it "$CONTAINER" python -c "
import asyncio, logging, os, sys
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s %(message)s')
os.chdir('/app')
sys.path.insert(0, '/app')
import yaml
async def main():
    from playwright.async_api import async_playwright
    with open('/app/kleinanzeigen-config/config.yaml', encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
    chromium = cfg.get('browser', {}).get('binary_location', '')
    profile  = '/app/kleinanzeigen-config/browser-profile'
    print(f'Chromium: {chromium}')
    print(f'Profile:  {profile}')
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile,
            executable_path=chromium,
            headless=True,
            args=['--no-sandbox','--disable-setuid-sandbox','--disable-gpu',
                  '--disable-blink-features=AutomationControlled'],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto('https://www.kleinanzeigen.de', timeout=20000, wait_until='domcontentloaded')
        html = await page.content()
        print(f'\\n=== URL: {page.url} | HTML length: {len(html)} ===')
        print('=== First 3000 chars of HTML ===')
        print(html[:3000])
        print('=== END ===')
        try:
            await page.click('#gdpr-banner-accept', timeout=4000)
            print('GDPR banner accepted')
        except Exception:
            print('No GDPR banner found')
        await ctx.close()
asyncio.run(main())
"
    ;;
  publish)
    _check_container
    echo "=== Running kleinanzeigen-bot publish --ads new directly ==="
    docker exec -it "$CONTAINER" sh -c "$KA_CMD --verbose publish --ads new"
    ;;
  publish-warmed)
    _check_container
    echo "=== Running pre-session warmup then kleinanzeigen-bot publish ==="
    docker exec -it "$CONTAINER" python -c "
import asyncio, os, sys
os.chdir('/app')
sys.path.insert(0, '/app')
from services.kleinanzeigen import KleinanzeigenService
async def main():
    s = KleinanzeigenService()
    await s._pre_session_setup()
    rc, out, err = await s._run_cli('--verbose', 'publish', '--ads', 'new', timeout=300)
    print(out)
    print(err)
    sys.exit(rc)
asyncio.run(main())
"
    ;;
  download)
    _check_container
    echo "=== Running kleinanzeigen-bot download directly ==="
    docker exec -it "$CONTAINER" sh -c "$KA_CMD --verbose download"
    ;;
  logs)
    _check_container
    echo "=== Last 100 lines of kleinanzeigen_bot.log ==="
    docker exec -it "$CONTAINER" sh -c \
      'tail -n 100 /app/kleinanzeigen-config/kleinanzeigen_bot.log'
    ;;
  shell)
    _check_container
    echo "=== Opening shell in container '${CONTAINER}' ==="
    docker exec -it "$CONTAINER" bash
    ;;
  *)
    echo "Usage: $0 [diagnose|warmup|publish|publish-warmed|download|logs|shell]"
    echo ""
    echo "  diagnose        Run browser diagnostics (recommended first step)"
    echo "  warmup          Run Playwright pre-session warmup (GDPR + Cloudflare cookies)"
    echo "  publish         Run kleinanzeigen-bot publish --ads new (direct)"
    echo "  publish-warmed  Run warmup then publish (tests the full wrapper path)"
    echo "  download        Run kleinanzeigen-bot download"
    echo "  logs            Tail the last 100 lines of kleinanzeigen_bot.log"
    echo "  shell           Open an interactive shell in the container"
    exit 1
    ;;
esac
