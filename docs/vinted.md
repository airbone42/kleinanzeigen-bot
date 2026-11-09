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
- **Draft first.** Items are created as a draft (`/item_upload/drafts`) and then published
  (`/drafts/{id}/completion`). If Vinted rejects the publish step, typically because the
  category demands a size or brand that could not be resolved, the draft stays in the account.
  The bot reports it, and you finish it in the Vinted app. The next sync notices when it is
  live.
- **Limits.** Title up to 100 characters (Kleinanzeigen allows 50, so always fits), description
  trimmed to 2000, at most 20 photos.
- **Session, not password.** Vinted's login has captchas and e-mail codes, so the bot does not
  log in itself. You import the session cookies once with `/vintedlogin`. The profile keeps them,
  and the bot refreshes the access token through `/web/api/auth/refresh`. If the refresh token
  expires, the sync reports it and asks for `/vintedlogin` again.
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

- the sold flags in the wardrobe API (`is_closed`, `item_closing_action`, ...). The sync logs
  the flags of the first wardrobe item, and `item_state()` in `services/vinted_mapper.py`
  accepts all known variants;
- where the web app keeps its CSRF token (meta tag or embedded JSON, `extract_csrf_token()`);
- whether `/size_groups?catalog_ids=` resolves sizes for every clothing category.

Watch the first runs (`/vinted`, logs). A sale that is detected late means a double sale, so if
the sold detection does not work, turn the mirror off until it is fixed.

## Setup

1. `VINTED_ENABLED=true` in `.env` (see `.env.example` for the other `VINTED_*` settings), restart.
2. Log in on vinted.de in your desktop browser, open DevTools → Network, click any request to
   vinted.de and copy the value of the `Cookie` request header.
3. Send `/vintedlogin <value>` to the bot. It deletes the message, stores the cookies in its own
   browser profile (`vinted-profile/` in the state directory) and confirms with your Vinted user
   ID.
4. `/vintedsync` runs the first sync, `/vinted` shows the mapping counts, open drafts and errors.

To stop: `VINTED_ENABLED=false`. Items already on Vinted stay there.
