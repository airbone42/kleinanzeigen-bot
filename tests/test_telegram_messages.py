from bot.handlers.daily_check import _build_renewal_change_message
from bot.handlers.new_listing import _build_publish_message
from models.listing import Listing


def test_build_publish_message_contains_full_listing_information() -> None:
    result = {
        "title": "Thule Dachträger 108 711100",
        "description": "Sehr gut erhaltener Dachträger mit Modellnummer 711100.",
        "price": 79.0,
        "category": "Auto, Rad & Boot > Autozubehör & Teile",
        "shipping_type": "PICKUP",
        "shipping_size": "PICKUP",
    }

    msg = _build_publish_message(
        result=result,
        listing_url="https://www.kleinanzeigen.de/s-anzeige/123",
        active_count=3,
        active_limit=5,
        draft_id=42,
        image_count=6,
    )

    assert "Titel: Thule Dachträger 108 711100" in msg
    assert "Preis: 79.00 EUR" in msg
    assert "Kategorie: Auto, Rad & Boot > Autozubehör & Teile" in msg
    assert "Versand: PICKUP (PICKUP)" in msg
    assert "Bilder: 6" in msg
    assert "Draft-ID: 42" in msg
    assert "Aktive Anzeigen: 3/5" in msg
    assert "URL: https://www.kleinanzeigen.de/s-anzeige/123" in msg
    assert "Beschreibung:" in msg
    assert "Modellnummer 711100" in msg


def test_build_renewal_change_message_contains_old_and_new_values() -> None:
    listing = Listing(
        listing_id="abc123",
        title="Dachträger alt",
        description="Alter Beschreibungstext",
        price=69.0,
        url="https://www.kleinanzeigen.de/s-anzeige/abc123",
    )

    msg = _build_renewal_change_message(
        listing=listing,
        new_title="Dachträger 108 711100",
        new_description="Neuer Beschreibungstext",
        new_price=79.0,
    )

    assert "Anzeige-ID: abc123" in msg
    assert "Titel alt: Dachträger alt" in msg
    assert "Titel neu: Dachträger 108 711100" in msg
    assert "Preis alt: 69.00 EUR" in msg
    assert "Preis neu: 79.00 EUR" in msg
    assert "Beschreibung alt:" in msg
    assert "Alter Beschreibungstext" in msg
    assert "Beschreibung neu:" in msg
    assert "Neuer Beschreibungstext" in msg
