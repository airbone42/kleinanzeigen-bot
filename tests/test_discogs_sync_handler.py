from bot.handlers.discogs_sync import _build_changes_only_message, _build_verbose_sync_message


def test_build_changes_only_message_returns_empty_without_changes() -> None:
    msg = _build_changes_only_message({"published": [], "deleted": []})
    assert msg == ""


def test_build_changes_only_message_contains_published_and_deleted() -> None:
    msg = _build_changes_only_message(
        {
            "published": [
                {
                    "artist": "Die Ärzte",
                    "title": "Jazz Ist Anders",
                    "price": "149.00",
                    "listing_url": "https://www.kleinanzeigen.de/s-anzeige/id/123",
                }
            ],
            "deleted": [{"release_id": "1124530", "listing_id": "123"}],
        }
    )
    assert "Neu veroeffentlicht" in msg
    assert "Geloescht" in msg
    assert "Jazz Ist Anders" in msg


def test_build_verbose_sync_message_lists_treasures() -> None:
    msg = _build_verbose_sync_message(
        {
            "inventory_items": [{}, {}],
            "treasure_items": [
                {
                    "artist": "Various",
                    "title": "Cocoon Compilation E",
                    "label": "Cocoon",
                    "year": 2004,
                    "price_eur": 80.0,
                    "median_price_eur": 70.0,
                }
            ],
            "published": [],
            "deleted": [],
            "updated_mappings": 0,
            "errors": [],
        }
    )
    assert "Vollausgabe" in msg
    assert "Treffer" in msg
    assert "Cocoon Compilation E" in msg
