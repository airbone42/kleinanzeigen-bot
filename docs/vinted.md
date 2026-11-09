# Vinted mirror

The bot can put every new Kleinanzeigen listing on [Vinted](https://www.vinted.de) as well and
keep both platforms in step: an item that sells on one platform disappears from the other.
The feature is **off by default** (`VINTED_ENABLED=false`).

> **Read this first.** Vinted offers no API for private sellers, and its terms of service
> forbid bots, third-party tools and cross-listing. Vinted uses DataDome bot protection and has
> banned accounts for automation. Running this mirror can get your Vinted account
> restricted or closed. You use it at your own risk.

## Why there is no library

We checked the options in November 2026:

| Option | Can it write listings? | Verdict |
|---|---|---|
| Vinted Pro Integrations API (`pro.svc.vinted.com`) | yes (items, orders, "item sold" webhook) | invite-only for professional sellers, not available to private accounts |
| `vinted-scraper`, `vinted-api-wrapper`, `pyVinted`, `vinted-api-kit` | no, search/read only | useless for selling |
| `ziorufus/vinted-lister` | yes (drafts, publish, hide, delete) | young, unlicensed, uses `curl_cffi` with copied tokens. We use its **captured web-app requests** as a reference for endpoints and payloads, not the code |
| `Hpn4/VintedBot`, old gists posting to `/api/v2/items` | partly | broken or vinted.fr only |
| Commercial cross-listers (Vendoo, List Perfectly, Crosslist, ...) | some | no Kleinanzeigen support, Vinted support is being withdrawn |

So the bot talks to Vinted the same way it already talks to Kleinanzeigen: through a **real,
non-headless Chromium** (nodriver, the stack kleinanzeigen-bot uses) with its own persistent
profile. Every Vinted request is a `fetch()` from inside the logged-in Vinted page. That means
same origin, the browser's own cookies and DataDome cookie, and a real browser fingerprint. Plain
HTTP clients get blocked by DataDome.

## How it works

```
new photo upload -> Kleinanzeigen listing (as before)
                 -> Vinted: LLM picks catalog/brand/condition/size
                    -> upload photos -> create draft -> publish draft

sync (VINTED_SYNC_HOURS, default 8:30, 12:30, 16:30, 20:30 Europe/Berlin, or /vintedsync)
  1. read Kleinanzeigen overview (published-ads JSON)
  2. load up to VINTED_BACKFILL_PER_RUN older listings that are not on Vinted yet
  3. Vinted browser: read own wardrobe, then for each mapped item
       sold on Vinted                    -> remember, delete on Kleinanzeigen in step 4
       deleted by hand on Vinted         -> close mapping, Kleinanzeigen stays
       reserved on Kleinanzeigen         -> hide on Vinted (unhide when un-reserved)
       gone from Kleinanzeigen N runs    -> delete on Vinted
       republished on Kleinanzeigen      -> follow the new listing ID
     mirror the backfill listings
  4. delete the Kleinanzeigen listings sold on Vinted
```

The `vinted_mappings` table (SQLite) links a Kleinanzeigen listing ID to a Vinted item ID.
Statuses: `active`, `draft`, `hidden`, `sold_vinted`, `sold_ka`, `vinted_removed`, `skipped`,
`failed`.

### Vinted peculiarities the code has to handle

- **Shipping only.** Vinted has no pickup-only sales, so pickup-only listings (furniture etc.)
  are skipped. Kleinanzeigen package sizes S/M/L map to Vinted `package_size_id` 1/2/3. The
  buyer pays postage and Vinted's buyer-protection fee. The seller gets the full price.
- **No "VB"/negotiable flag.** Buyers send offers instead. Lines mentioning VB, pickup, PayPal or
  Kleinanzeigen are removed from the description. The price is the Kleinanzeigen price times
  `VINTED_PRICE_FACTOR`, at least 1 EUR. Give-away listings are skipped.
- **Structured fields.** Vinted needs a leaf `catalog_id` from its own category tree (fashion,
  home, electronics, entertainment, hobby, sport, no furniture/vehicles), a condition (6 new
  with tags, 1 new without tags, 2 very good, 3 good, 4 satisfactory), optionally a brand ID
  (`/brands?keyword=`) and, for clothing, a size ID. The LLM picks a category group, then the
  leaf, brand, condition and size. The catalog tree is cached in `vinted/catalogs.json` in the
  state directory (delete it to refresh).
- **More required fields than expected** (first real run, 09.11.2026): Vinted demanded a **color**
  even for a bicycle inner tube and an **ISBN** for a book. The LLM picks a color from
  `/api/v2/colors` (cached in `vinted/colors.json`). The ISBN (`services/isbn_lookup.py`) comes
  from the listing text, else from a photo (vision model reads the barcode/ISBN on the back
  cover), else from a catalogue search: the LLM extracts title and author (Kleinanzeigen titles
  are cut at 50 characters), the bot searches the Deutsche Nationalbibliothek (SRU, free, best
  for German books) and Open Library by keywords, and the LLM picks the printed edition among
  the real hits or none. Every ISBN is checksum-verified and must come from a photo, the text or
  a catalogue hit; the bot never invents one. Without one the draft stays for you.
- **Books also need a language** (dynamic attribute `language_book`, global ids, e.g. German
  6437, English 6435). The LLM names the language, German when unknown.
- **Photos.** Kleinanzeigen serves WebP. The bot re-encodes every photo as JPEG (max 1600 px).
  An upload returns a *temporary* photo id; saving the draft turns it into a stored photo with a
  new id. Publishing with the temporary ids fails with `photos: Fehler beim Hochladen`, so the bot
  re-reads the draft (`GET /item_upload/items/{id}`) and publishes with the stored photo ids
  (found 09.11.2026 via the photo diagnostics in the log).
- **Draft first.** Items are created as a draft (`/item_upload/drafts`) and then published
  (`/drafts/{id}/completion`). If Vinted rejects the publish step, typically because the
  category demands a size, ISBN or brand that could not be resolved, the draft stays in the
  account. The bot reports it, and you finish it in the Vinted app. The next sync notices when it
  is live. Transient rejections (photos only) are retried at most 3 times: the next sync uploads
  the photos again and refills the same draft (`PUT /item_upload/drafts/{id}`, fresh photo ids,
  reused ids wipe the photos) before publishing. Drafts cannot be deleted with
  `/items/{id}/delete` (HTTP 400); the bot tries `DELETE /item_upload/drafts/{id}` and otherwise
  asks you to delete the (invisible) draft in the app.
- **Wardrobe flags (confirmed 09.11.2026):** items carry `is_closed`, `item_closing_action`,
  `is_reserved`, `is_hidden`, `is_draft`, `is_processing`. `status` is the condition text
  ("Gut"), not a sale state. Drafts show up in the wardrobe too.
- **Limits.** Title up to 100 characters (Kleinanzeigen allows 50, so always fits), description
  trimmed to 2000, at most 20 photos.
- **Session, not password.** Vinted's login has captchas and e-mail codes, so the bot does not
  log in itself. You import the session cookies once with `/vintedlogin`. The profile keeps them,
  and the bot refreshes the access token through `/web/api/auth/refresh`. If the refresh token
  expires, the sync reports it and asks for `/vintedlogin` again. Observed lifetimes (November 2026):
  `access_token_web` 2 hours, `refresh_token_web` 7 days. The four daily syncs refresh well
  within that. Whether Vinted rotates the refresh token on each refresh is not confirmed yet. If it
  does not, `/vintedlogin` is needed once a week.
- **Anonymous tokens.** Every visit without a session gets anonymous `access_token_web` /
  `refresh_token_web` cookies (JWT without `sub`): access on `.vinted.de`, refresh on
  `.www.vinted.de`. Imported tokens must sit on exactly those domains, and leftover anonymous
  cookies must go first. Otherwise Vinted swaps the session for an anonymous one ("token without
  user ID", 09.11.2026). `anon_id` has to match the token's `anid` claim.
- **Phone verification.** A fresh account carries `mandatory_verification_required=true` until
  the phone number is confirmed. Uploads likely fail until then.
- **The bot needs its own session.** Copying the session of the browser you keep using got it
  revoked on 09.11.2026 after about 45 minutes (same session from two IPs/browsers): Vinted
  logged out both the bot and the owner's browser. Log in in a private window, copy the cookie
  header, close the window **without logging out**, then `/vintedlogin`. Logging out of that
  window ends the bot's session.
- **Only session cookies are imported** (`access_token_web`, `refresh_token_web`,
  `_vinted_<cc>_session`, `v_uid`, `v_sid`, `anon_id`). `datadome` and `cf_clearance` are bound to
  the browser and IP they were issued to, so the bot's own Chromium gets its own.
- **Telegram splits long messages.** A full cookie header is often longer than 4096 characters.
  `/vintedlogin` collects follow-up messages for a few seconds, joins them, deletes every part and
  only then logs in.
- **Safety against false deletes.** A Kleinanzeigen listing must be missing for
  `VINTED_MISSING_RUNS_BEFORE_DELETE` runs (default 2) before the Vinted item is deleted. An
  empty Kleinanzeigen overview aborts the sync. Republishes are followed by ID (bot hook) or by
  identical title. A Kleinanzeigen listing is only deleted when Vinted marks the item sold. An
  item that simply vanished from Vinted never deletes anything on Kleinanzeigen.
- **Discogs-managed records are not mirrored.** A Vinted sale would not reach the Discogs
  inventory.
- **Low request rate.** Four sync runs a day, one browser at a time, a few requests per item.
  This keeps the bot below what a human seller does in the app.

### Not verified against a live account yet

The endpoints follow a captured real web-app session from June 2026. These details still need
confirmation on the first real run:

- which value `item_closing_action` takes for a sale (the flags exist, the first sale will
  show it; `item_state()` treats `is_closed` without a delete action as sold);
- whether `/size_groups?catalog_ids=` resolves sizes for every clothing category.

Watch the first runs (`/vinted`, logs). A sale that is detected late means a double sale, so if
the sold detection does not work, turn the mirror off until it is fixed.

## Setup

1. `VINTED_ENABLED=true` in `.env` (see `.env.example` for the other `VINTED_*` settings), restart.
2. Open a **private window**, log in on vinted.de, open DevTools → Network, click any request to
   vinted.de and copy the value of the `Cookie` request header. Close the private window without
   logging out (the bot needs a session nobody else uses).
3. Send `/vintedlogin <value>` to the bot. It deletes the message, stores the cookies in its own
   browser profile (`vinted-profile/` in the state directory) and confirms with your Vinted user
   ID.
4. `/vintedsync` runs the first sync, `/vinted` shows the mapping counts, open drafts and errors.
   `/vintedretry` retries rejected drafts and failed mirrors right away (useful after an update).

To stop: `VINTED_ENABLED=false`. Items already on Vinted stay there.
