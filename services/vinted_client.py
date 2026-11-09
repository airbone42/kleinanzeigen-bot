"""Vinted web API, driven from inside a logged-in Chromium page.

There is no public Vinted API for private sellers (the Pro Integrations API is
invite-only) and no maintained library that can write listings (docs/vinted.md).
Vinted sits behind DataDome, which blocks plain HTTP clients by TLS/browser
fingerprint. So, like kleinanzeigen-bot, this module starts a real, non-headless
Chromium via nodriver with its own persistent profile and sends the same
``/api/v2`` requests the Vinted web app sends, as ``fetch()`` inside the page:
same origin, the browser's cookies, its DataDome cookie and its fingerprint.

Endpoints and payload shapes follow a captured real web-app session
(github.com/ziorufus/vinted-lister, data/draft_request.json, June 2026):

    POST /api/v2/photos                                  multipart, photo id
    POST /api/v2/item_upload/drafts                      {"draft": {...}}
    POST /api/v2/item_upload/drafts/{id}/completion      publish the draft
    GET  /api/v2/wardrobe/{user_id}/items                own items
    PUT  /api/v2/items/{id}/is_hidden                    {"is_hidden": bool}
    POST /api/v2/items/{id}/delete
    GET  /api/v2/item_upload/catalogs | /api/v2/brands?keyword=
    POST /web/api/auth/refresh                           new access_token_web

The login is not automated (captcha / e-mail codes). The owner imports the
session cookies once with /vintedlogin; the profile keeps them and the web app
refreshes the access token itself.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# One Vinted browser at a time: the CT has 1.5 GB RAM and the profile can only be
# opened once.
VINTED_LOCK = asyncio.Lock()

PAGE_SETTLE_SECONDS = 4
FETCH_TIMEOUT_MS = 90_000
BROWSER_START_ATTEMPTS = 3
WARDROBE_PER_PAGE = 50
WARDROBE_MAX_PAGES = 40
# Cookies that make up a Vinted web session. Only the two tokens are required.
SESSION_COOKIES = ("access_token_web", "refresh_token_web")
CHROMIUM_FLAGS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]

DYNAMIC_ATTRIBUTE_HEADERS = {
    "X-Enable-Dynamic-Attribute-Condition": "true",
    "X-Enable-Dynamic-Attribute-Size": "true",
    "X-Enable-Dynamic-Attribute-Video-Game-Rating": "true",
}


class VintedError(Exception):
    """Any failure talking to Vinted."""


class VintedAuthError(VintedError):
    """No valid Vinted session: the owner has to run /vintedlogin again."""


class VintedApiError(VintedError):
    """Vinted answered with a non-2xx status."""

    def __init__(self, what: str, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"{what}: HTTP {status} {body[:300]}")


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse a pasted ``Cookie:`` header (or ``name=value`` lines) into a dict."""
    text = raw.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    cookies: dict[str, str] = {}
    for part in re.split(r"[;\n]", text):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip():
            cookies[name.strip()] = value.strip().strip('"')
    return cookies


# Imported besides the tokens. Bot-protection cookies (datadome, cf_clearance) are bound
# to the owner's browser and IP; the bot's own Chromium has to earn its own.
EXTRA_SESSION_COOKIES = ("v_uid", "v_sid", "anon_id", "user-iso-locale", "domain_selected")
SESSION_COOKIE_RE = re.compile(r"^_vinted_[a-z]{2}_session$")


def session_cookies(cookies: dict[str, str]) -> dict[str, str]:
    """Only the cookies that make up the login, nothing for tracking or bot protection."""
    return {
        name: value
        for name, value in cookies.items()
        if value and (name in SESSION_COOKIES or name in EXTRA_SESSION_COOKIES
                      or SESSION_COOKIE_RE.match(name))
    }


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode the payload of a JWT without verifying it (only to read sub/exp)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def token_valid(token: str, margin: int = 120) -> bool:
    exp = jwt_claims(token).get("exp")
    if not isinstance(exp, (int, float)):
        return bool(token)
    return exp - margin > time.time()


def cookie_domain(base_url: str) -> str:
    host = urlparse(base_url).hostname or "www.vinted.de"
    return "." + (host[4:] if host.startswith("www.") else host)


def extract_csrf_token(page_html: str) -> Optional[str]:
    """Find the CSRF token the web app embeds in its HTML (meta tag or JSON blob)."""
    patterns = (
        r'<meta[^>]+name="csrf-token"[^>]+content="([^"]+)"',
        r'<meta[^>]+content="([^"]+)"[^>]+name="csrf-token"',
        r'\\?"CSRF_TOKEN\\?"\s*:\s*\\?"([0-9a-zA-Z_\-]{20,})',
        r'\\?"csrf_?[tT]oken\\?"\s*:\s*\\?"([0-9a-zA-Z_\-]{20,})',
    )
    for pattern in patterns:
        match = re.search(pattern, page_html)
        if match:
            return match.group(1)
    return None


def summarize_photo(photo: dict[str, Any]) -> dict[str, Any]:
    """Photo object without URLs, for the log."""
    return {
        k: v for k, v in photo.items()
        if not isinstance(v, (dict, list)) and "url" not in k.lower() and k != "dominant_color"
    }


def find_chromium(configured: str = "") -> Optional[str]:
    if configured:
        return configured if os.path.exists(configured) else None
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


class VintedClient:
    """Async context manager: one browser session against the Vinted web app."""

    def __init__(
        self,
        *,
        base_url: str,
        profile_dir: Path,
        locale: str = "de-DE",
        chromium_path: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_base = f"{self.base_url}/api/v2"
        self.profile_dir = profile_dir
        self.locale = locale
        self.chromium_path = chromium_path
        self.browser: Any = None
        self.tab: Any = None
        self.user_id: Optional[str] = None
        self._csrf: Optional[str] = None
        self._access_token: str = ""
        self._anon_id: str = ""

    # ----- browser lifecycle ------------------------------------------------
    async def __aenter__(self) -> "VintedClient":
        await VINTED_LOCK.acquire()
        try:
            await self._start_browser()
        except BaseException:
            VINTED_LOCK.release()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self.browser is not None:
                await self._close_gracefully()
                try:
                    self.browser.stop()
                except Exception as e:
                    logger.debug(f"Vinted browser stop failed: {e}")
            self._kill_orphans()
        finally:
            self.browser = None
            self.tab = None
            VINTED_LOCK.release()

    async def _close_gracefully(self) -> None:
        """Browser.close lets Chromium write its cookie store; a plain kill loses the session."""
        from nodriver import cdp

        process = getattr(self.browser, "_process", None)
        try:
            await asyncio.wait_for(self.tab.send(cdp.browser.close()), timeout=5)
        except Exception as e:
            logger.debug(f"Browser.close: {e}")
        for _ in range(20):
            if process is None or process.returncode is not None:
                break
            await asyncio.sleep(0.5)

    def _kill_orphans(self) -> None:
        """Kill Chromium processes still holding the Vinted profile (Linux only)."""
        marker = f"--user-data-dir={self.profile_dir.resolve()}".encode()
        proc = Path("/proc")
        if not proc.is_dir():
            return
        victims = []
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                if marker in (entry / "cmdline").read_bytes().split(b"\0"):
                    victims.append(int(entry.name))
            except OSError:
                continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in victims:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            if sig == signal.SIGTERM and victims:
                time.sleep(2)
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (self.profile_dir / name).unlink()
            except OSError:
                pass

    async def _start_browser(self) -> None:
        import nodriver

        chromium = find_chromium(self.chromium_path)
        if not chromium:
            raise VintedError("Kein Chromium gefunden (CHROMIUM_PATH oder Paket chromium).")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        is_root = getattr(os, "geteuid", lambda: -1)() == 0
        last_error: Optional[Exception] = None
        for attempt in range(1, BROWSER_START_ATTEMPTS + 1):
            self._kill_orphans()
            try:
                self.browser = await nodriver.start(
                    user_data_dir=str(self.profile_dir),
                    browser_executable_path=chromium,
                    headless=False,
                    sandbox=not is_root,
                    browser_args=list(CHROMIUM_FLAGS) + [f"--lang={self.locale}"],
                )
                self.tab = await self.browser.get(self.base_url)
                await asyncio.sleep(PAGE_SETTLE_SECONDS)
                return
            except Exception as e:
                last_error = e
                logger.warning(f"Vinted browser start failed (attempt {attempt}): {e}")
                await asyncio.sleep(5)
        raise VintedError(f"Browser für Vinted startet nicht: {last_error}")

    # ----- cookies / session ------------------------------------------------
    async def _raw_cookies(self) -> list[Any]:
        base = cookie_domain(self.base_url).lstrip(".")
        cookies = await self.browser.cookies.get_all()
        return [c for c in cookies if str(getattr(c, "domain", "") or "").lstrip(".").endswith(base)]

    async def _cookies(self) -> dict[str, str]:
        """Vinted cookies by name. For the tokens the logged-in variant wins.

        Every visit without a session gets anonymous tokens (JWT without ``sub``), and
        Vinted keeps them on two domains (access on .vinted.de, refresh on
        .www.vinted.de), so the profile can hold several cookies of the same name.
        """
        out: dict[str, str] = {}
        best_exp: dict[str, float] = {}
        for c in await self._raw_cookies():
            name, value = str(c.name), str(c.value)
            if name in SESSION_COOKIES:
                claims = jwt_claims(value)
                score = (1e12 if claims.get("sub") else 0) + float(claims.get("exp") or 0)
                if score <= best_exp.get(name, -1):
                    continue
                best_exp[name] = score
            out[name] = value
        return out

    async def import_cookies(self, cookies: dict[str, str]) -> None:
        """Replace the profile's Vinted cookies with the pasted session.

        Domains as Vinted sets them itself (checked 09.11.2026): access_token_web on
        .vinted.de, refresh_token_web on .www.vinted.de, the rest host-only. anon_id
        must match the token's ``anid`` claim. Leftover anonymous tokens made Vinted
        swap the imported session for an anonymous one.
        """
        from nodriver import cdp

        missing = [name for name in SESSION_COOKIES if not cookies.get(name)]
        if missing:
            raise VintedAuthError(f"Es fehlen die Cookies {', '.join(missing)}.")
        claims = jwt_claims(cookies["access_token_web"])
        if not claims.get("sub"):
            raise VintedAuthError(
                "Das access_token_web gehört zu keinem Konto (anonym). Erst auf vinted.de "
                "einloggen, dann den Cookie-Wert kopieren."
            )
        cookies = session_cookies(cookies)
        if claims.get("anid"):
            cookies["anon_id"] = str(claims["anid"])

        for c in await self._raw_cookies():
            await self.tab.send(cdp.network.delete_cookies(
                name=str(c.name), domain=str(c.domain), path=str(c.path or "/")))

        host = urlparse(self.base_url).hostname or "www.vinted.de"
        one_year = time.time() + 365 * 86400
        params = []
        for name, value in cookies.items():
            if name == "access_token_web":
                where: dict[str, Any] = {"domain": cookie_domain(self.base_url)}
            elif name == "refresh_token_web":
                where = {"domain": "." + host}
            else:
                where = {"url": f"https://{host}/"}
            # Without expires Chromium keeps a session cookie and drops it at exit.
            exp = jwt_claims(value).get("exp") if name in SESSION_COOKIES else None
            if name == "access_token_web":
                exp = time.time() + 7 * 86400  # Vinted's own Max-Age; refreshed on use
            params.append(cdp.network.CookieParam(
                name=name, value=value, path="/", secure=True,
                http_only=name in SESSION_COOKIES,
                expires=cdp.network.TimeSinceEpoch(float(exp or one_year)), **where,
            ))
        await self.browser.cookies.set_all(params)
        self.tab = await self.browser.get(self.base_url)
        await asyncio.sleep(PAGE_SETTLE_SECONDS)

    async def ensure_session(self) -> str:
        """Make sure an access token is valid; returns the Vinted user id."""
        cookies = await self._cookies()
        token = cookies.get("access_token_web", "")
        self._anon_id = cookies.get("anon_id", "")
        if not token_valid(token) or not jwt_claims(token).get("sub"):
            if not jwt_claims(cookies.get("refresh_token_web", "")).get("sub"):
                raise VintedAuthError(
                    "Keine angemeldete Vinted-Sitzung im Browser-Profil (nur anonym), /vintedlogin."
                )
            await self._refresh_token()
            cookies = await self._cookies()
            token = cookies.get("access_token_web", "")
            if not token_valid(token) or not jwt_claims(token).get("sub"):
                raise VintedAuthError("Vinted-Sitzung abgelaufen, bitte /vintedlogin erneut.")
        self._access_token = token
        self.user_id = str(jwt_claims(token)["sub"])
        return self.user_id

    async def _refresh_token(self) -> None:
        result = await self._fetch("POST", f"{self.base_url}/web/api/auth/refresh", auth=False)
        if result["status"] != 200:
            raise VintedAuthError(
                f"Token-Refresh abgelehnt (HTTP {result['status']}), bitte /vintedlogin erneut."
            )
        await asyncio.sleep(1)

    async def _ensure_csrf(self) -> str:
        if self._csrf:
            return self._csrf
        await self.tab.get(f"{self.base_url}/items/new")
        await asyncio.sleep(PAGE_SETTLE_SECONDS)
        page_html = await self.tab.evaluate("document.documentElement.outerHTML")
        token = extract_csrf_token(page_html if isinstance(page_html, str) else "")
        if not token:
            logger.warning("Vinted CSRF token not found on /items/new, sending requests without")
        self._csrf = token or ""
        return self._csrf

    # ----- requests -----------------------------------------------------------
    def _headers(self, extra: Optional[dict[str, str]] = None, auth: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Locale": self.locale,
        }
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        if self._anon_id:
            headers["X-Anon-Id"] = self._anon_id
        if auth and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        headers.update(extra or {})
        return headers

    async def _evaluate_json(self, expression: str) -> dict[str, Any]:
        raw = await self.tab.evaluate(expression, await_promise=True, return_by_value=True)
        if isinstance(raw, tuple):  # (RemoteObject, ExceptionDetails) on a JS error
            raise VintedError(f"JS-Fehler im Vinted-Tab: {raw[1] if len(raw) > 1 else raw}")
        if not isinstance(raw, str):
            raise VintedError(f"Unerwartete Antwort aus dem Vinted-Tab: {raw!r:.200}")
        data = json.loads(raw)
        if data.get("error"):
            raise VintedError(f"fetch fehlgeschlagen: {data['error']}")
        return data

    async def _fetch(
        self,
        method: str,
        url: str,
        body: Any = None,
        *,
        headers: Optional[dict[str, str]] = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        """Run fetch() in the page; returns {"status": int, "text": str}."""
        all_headers = self._headers(headers, auth=auth)
        if body is not None:
            all_headers["Content-Type"] = "application/json"
        js = (
            "(async () => { const ctl = new AbortController();"
            f" const t = setTimeout(() => ctl.abort(), {FETCH_TIMEOUT_MS});"
            f" try {{ const r = await fetch({json.dumps(url)}, {{"
            f" method: {json.dumps(method)}, credentials: 'include',"
            f" headers: {json.dumps(all_headers)},"
            f" body: {json.dumps(json.dumps(body)) if body is not None else 'undefined'},"
            " signal: ctl.signal });"
            " return JSON.stringify({status: r.status, text: await r.text()});"
            " } catch (e) { return JSON.stringify({error: String(e)}); }"
            " finally { clearTimeout(t); } })()"
        )
        return await self._evaluate_json(js)

    async def api(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        what: str = "",
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Call ``/api/v2<path>`` and return the parsed JSON (one retry after 401)."""
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        result = await self._fetch(method, url, body, headers=headers)
        if result["status"] == 401:
            await self._refresh_token()
            await self.ensure_session()
            result = await self._fetch(method, url, body, headers=headers)
        status = int(result["status"])
        text = str(result.get("text") or "")
        if status == 401:
            raise VintedAuthError("Vinted lehnt die Sitzung ab (401), bitte /vintedlogin erneut.")
        if status == 403 and "datadome" in text.lower():
            raise VintedError("Vinted blockiert den Browser (DataDome-Captcha).")
        if not 200 <= status < 300:
            raise VintedApiError(what or f"{method} {path}", status, text)
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise VintedApiError(what or path, status, f"kein JSON: {text[:200]}") from e
        return data if isinstance(data, dict) else {"data": data}

    # ----- Vinted operations -----------------------------------------------
    async def upload_photo(self, image: bytes, temp_uuid: str, filename: str = "photo.jpg") -> int:
        await self._ensure_csrf()
        headers = self._headers()
        js = (
            "(async () => { try {"
            f" const b64 = {json.dumps(base64.b64encode(image).decode())};"
            " const bin = atob(b64); const buf = new Uint8Array(bin.length);"
            " for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);"
            " const fd = new FormData(); fd.append('photo[type]', 'item');"
            f" fd.append('photo[file]', new Blob([buf], {{type: 'image/jpeg'}}), {json.dumps(filename)});"
            f" fd.append('photo[temp_uuid]', {json.dumps(temp_uuid)});"
            f" const r = await fetch({json.dumps(self.api_base + '/photos')}, {{method: 'POST',"
            f" credentials: 'include', headers: {json.dumps(headers)}, body: fd}});"
            " return JSON.stringify({status: r.status, text: await r.text()});"
            " } catch (e) { return JSON.stringify({error: String(e)}); } })()"
        )
        result = await self._evaluate_json(js)
        status = int(result["status"])
        text = str(result.get("text") or "")
        if not 200 <= status < 300:
            raise VintedApiError("Foto-Upload", status, text)
        data = json.loads(text)
        photo = data.get("photo") if isinstance(data.get("photo"), dict) else data
        photo_id = photo.get("id")
        logger.info(f"Vinted photo upload: {len(image)} bytes, magic {image[:4].hex()}, "
                    f"response {summarize_photo(photo)}")
        if not isinstance(photo_id, int):
            raise VintedApiError("Foto-Upload", status, f"keine Foto-ID: {text[:200]}")
        return photo_id

    async def create_draft(self, draft: dict[str, Any], upload_session_id: str) -> dict[str, Any]:
        await self._ensure_csrf()
        body = {"draft": draft, "feedback_id": None, "parcel": None,
                "upload_session_id": upload_session_id}
        data = await self.api("POST", "/item_upload/drafts", body, what="Entwurf anlegen",
                              headers=DYNAMIC_ATTRIBUTE_HEADERS)
        item = data.get("draft") or data.get("item") or data
        if not isinstance(item, dict) or not item.get("id"):
            raise VintedError(f"Entwurf ohne ID zurückgegeben: {str(data)[:200]}")
        return item

    async def update_draft(
        self, draft_id: int, draft: dict[str, Any], upload_session_id: str
    ) -> dict[str, Any]:
        """Replace a draft's data. Photos must be freshly uploaded ids (old ones wipe them)."""
        await self._ensure_csrf()
        body = {"draft": {**draft, "id": draft_id}, "feedback_id": None, "parcel": None,
                "upload_session_id": upload_session_id}
        return await self.api("PUT", f"/item_upload/drafts/{draft_id}", body,
                              what="Entwurf ändern", headers=DYNAMIC_ATTRIBUTE_HEADERS)

    async def complete_draft(
        self, draft_id: int, draft: dict[str, Any], upload_session_id: str
    ) -> dict[str, Any]:
        """Publish a draft (the "Hochladen" button)."""
        await self._ensure_csrf()
        body = {"draft": {**draft, "id": draft_id}, "push_up": False, "parcel": None,
                "upload_session_id": upload_session_id}
        data = await self.api("POST", f"/item_upload/drafts/{draft_id}/completion", body,
                              what="Entwurf veröffentlichen", headers=DYNAMIC_ATTRIBUTE_HEADERS)
        item = data.get("item") or data.get("draft") or data
        return item if isinstance(item, dict) else {}

    async def update_item(self, item_id: int, draft: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_csrf()
        return await self.api("PUT", f"/item_upload/items/{item_id}", {"item": {**draft, "id": item_id}},
                              what="Artikel ändern", headers=DYNAMIC_ATTRIBUTE_HEADERS)

    async def get_item_edit(self, item_id: int) -> dict[str, Any]:
        data = await self.api("GET", f"/item_upload/items/{item_id}", what="Artikel lesen")
        item = data.get("item")
        return item if isinstance(item, dict) else {}

    async def wardrobe_items(self) -> list[dict[str, Any]]:
        """All own items (live, reserved, sold ones Vinted still shows)."""
        if not self.user_id:
            await self.ensure_session()
        items: list[dict[str, Any]] = []
        for page in range(1, WARDROBE_MAX_PAGES + 1):
            data = await self.api(
                "GET",
                f"/wardrobe/{self.user_id}/items?page={page}&per_page={WARDROBE_PER_PAGE}&order=newest_first",
                what="Kleiderschrank lesen",
            )
            batch = data.get("items") or []
            items.extend(i for i in batch if isinstance(i, dict))
            pagination = data.get("pagination") or {}
            total_pages = pagination.get("total_pages")
            if not batch or (isinstance(total_pages, int) and page >= total_pages):
                break
            if not isinstance(total_pages, int) and len(batch) < WARDROBE_PER_PAGE:
                break
        return items

    async def set_hidden(self, item_id: int, hidden: bool) -> None:
        await self._ensure_csrf()
        await self.api("PUT", f"/items/{item_id}/is_hidden", {"is_hidden": hidden},
                       what="Artikel verbergen" if hidden else "Artikel zeigen")

    async def delete_item(self, item_id: int) -> None:
        await self._ensure_csrf()
        await self.api("POST", f"/items/{item_id}/delete", {}, what="Artikel löschen")

    async def delete_draft(self, draft_id: int) -> None:
        """Drafts reject /items/{id}/delete (HTTP 400 InvalidRequest, 09.11.2026)."""
        await self._ensure_csrf()
        await self.api("DELETE", f"/item_upload/drafts/{draft_id}", what="Entwurf löschen")

    async def catalogs(self) -> list[dict[str, Any]]:
        data = await self.api("GET", "/item_upload/catalogs", what="Kategorien lesen")
        catalogs = data.get("catalogs")
        return catalogs if isinstance(catalogs, list) else []

    async def search_brands(self, keyword: str) -> list[dict[str, Any]]:
        from urllib.parse import quote

        data = await self.api("GET", f"/brands?keyword={quote(keyword)}", what="Marke suchen")
        brands = data.get("brands")
        return brands if isinstance(brands, list) else []

    async def colors(self) -> list[dict[str, Any]]:
        data = await self.api("GET", "/colors", what="Farben lesen")
        colors = data.get("colors")
        return colors if isinstance(colors, list) else []

    async def size_groups(self, catalog_id: int) -> list[dict[str, Any]]:
        data = await self.api("GET", f"/size_groups?catalog_ids={catalog_id}", what="Größen lesen")
        groups = data.get("size_groups")
        return groups if isinstance(groups, list) else []
