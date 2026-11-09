"""Vinted mirror: field mapping, client helpers and the two-way sold sync."""
import base64
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from config.settings import settings
from db.repository import vinted_mapping_repo
from models.listing import Listing
from services import vinted_mapper as vm
from services.vinted_client import (
    VintedApiError,
    cookie_domain,
    extract_csrf_token,
    jwt_claims,
    parse_cookie_header,
    token_valid,
)
from services.vinted_sync import MirrorInput, VintedSyncService, ka_id_from_url


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"


# ----- client helpers -------------------------------------------------------

def test_parse_cookie_header_accepts_devtools_value() -> None:
    cookies = parse_cookie_header('Cookie: anon_id=abc; access_token_web=AAA.BBB.CCC; refresh_token_web="r1"')
    assert cookies == {"anon_id": "abc", "access_token_web": "AAA.BBB.CCC", "refresh_token_web": "r1"}


def test_jwt_and_token_validity() -> None:
    fresh = _jwt({"sub": 4711, "exp": time.time() + 3600})
    stale = _jwt({"sub": 4711, "exp": time.time() - 10})
    assert jwt_claims(fresh)["sub"] == 4711
    assert token_valid(fresh)
    assert not token_valid(stale)
    assert not token_valid("")


def test_cookie_domain_and_csrf() -> None:
    assert cookie_domain("https://www.vinted.de") == ".vinted.de"
    assert extract_csrf_token('<meta name="csrf-token" content="tok123">') == "tok123"
    assert extract_csrf_token('..."CSRF_TOKEN":"75f6c9fa-dc8e-4e52-a000-e09dd4084b3e"...') == (
        "75f6c9fa-dc8e-4e52-a000-e09dd4084b3e"
    )
    assert extract_csrf_token("<html></html>") is None


# ----- mapper -------------------------------------------------------------------

def test_shipping_size_and_skip_reason() -> None:
    assert vm.shipping_size_from_listing("SHIPPING", shipping_options='["DHL_5", "Hermes_M"]') == "M"
    assert vm.shipping_size_from_listing("PICKUP", shipping_options=None) == "PICKUP"
    assert vm.shipping_size_from_listing("SHIPPING", shipping_size="L") == "L"
    assert vm.skip_reason(20, "PICKUP") is not None
    assert vm.skip_reason(0, "S") is not None
    assert vm.skip_reason(20, "S") is None


def test_adapt_description_drops_kleinanzeigen_only_lines() -> None:
    text = "Gut erhaltene Jacke.\nAbholung in Mainz oder Versand.\nPreis VB\nKeine Flecken."
    assert vm.adapt_description(text) == "Gut erhaltene Jacke.\nKeine Flecken."
    assert vm.adapt_title("Reserviert: Lego Technic 42100") == "Lego Technic 42100"


TREE = [
    {"id": 1, "title": "Damen", "catalogs": [
        {"id": 10, "title": "Kleidung", "catalogs": [
            {"id": 100, "title": "Jacken", "catalogs": []},
            {"id": 101, "title": "Kleider", "catalogs": []},
        ]},
    ]},
    {"id": 2, "title": "Unterhaltung", "catalogs": [
        {"id": 20, "title": "Bücher", "catalogs": [{"id": 200, "title": "Romane", "catalogs": []}]},
    ]},
]


def test_flatten_and_group_catalogs() -> None:
    leaves = vm.flatten_catalogs(TREE)
    assert {leaf["id"] for leaf in leaves} == {100, 101, 200}
    assert leaves[0]["path"] == "Damen > Kleidung > Jacken"
    assert set(vm.group_leaves(leaves)) == {"Damen > Kleidung", "Unterhaltung > Bücher"}


class FakeLLM:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers

    async def ainvoke(self, messages: list) -> Any:
        return SimpleNamespace(content=self.answers.pop(0))


async def test_choose_vinted_fields_two_steps() -> None:
    llm = FakeLLM([
        '{"group": "Damen > Kleidung"}',
        '```json\n{"catalog_id": 100, "brand": "Vaude", "condition": "very_good", "size": "M"}\n```',
    ])
    fields = await vm.choose_vinted_fields(
        llm, title="Vaude Regenjacke", description="Kaum getragen", ka_category="",
        leaves=vm.flatten_catalogs(TREE),
    )
    assert (fields.catalog_id, fields.brand, fields.condition, fields.size) == (100, "Vaude", "very_good", "M")


async def test_choose_vinted_fields_no_group() -> None:
    fields = await vm.choose_vinted_fields(
        FakeLLM(['{"group": null}']), title="Sofa", description="3-Sitzer", ka_category="",
        leaves=vm.flatten_catalogs(TREE),
    )
    assert fields.catalog_id is None and fields.notes


def test_build_draft_matches_captured_shape() -> None:
    draft = vm.build_draft(
        temp_uuid="u1", title="Jacke", description="Gut", price=25.0, currency="EUR",
        catalog_id=100, package_size="M", photo_ids=[11, 12], condition="very_good",
        brand_id=7, brand="Vaude", size_id=209,
    )
    assert draft["package_size_id"] == 2
    assert draft["assigned_photos"] == [{"id": 11, "orientation": 0}, {"id": 12, "orientation": 0}]
    assert {"code": "condition", "ids": [2]} in draft["item_attributes"]
    assert {"code": "size", "ids": [209]} in draft["item_attributes"]
    assert draft["temp_uuid"] == "u1" and draft["currency"] == "EUR"


def test_item_state_and_brand_size_pick() -> None:
    assert vm.item_state({"is_closed": True, "item_closing_action": "sold"}) == "sold"
    assert vm.item_state({"is_closed": True}) == "sold"
    assert vm.item_state({"is_closed": True, "item_closing_action": "deleted"}) == "active"
    assert vm.item_state({"is_reserved": True}) == "reserved"
    assert vm.item_state({"is_hidden": True}) == "hidden"
    assert vm.item_state({}) == "active"
    assert vm.pick_brand([{"id": 3, "title": "Vaude"}], "vaude") == (3, "Vaude")
    assert vm.pick_brand([{"id": 3, "title": "Vaude Kids"}], "Vaude") == (None, "Vaude")
    assert vm.pick_size([{"sizes": [{"id": 5, "title": "M"}, {"id": 6, "title": "L"}]}], "l") == 6


def test_ka_id_from_url() -> None:
    assert ka_id_from_url("https://www.kleinanzeigen.de/s-anzeige/jacke/3123456789-153-1234") == "3123456789"
    assert ka_id_from_url("https://www.kleinanzeigen.de/s-anzeige/id/3123456789") == "3123456789"
    assert ka_id_from_url("https://www.kleinanzeigen.de/m-meine-anzeigen.html") is None


# ----- sync with fakes ----------------------------------------------------------

class FakeClient:
    def __init__(self, wardrobe: list[dict] | None = None, fail_completion: bool = False) -> None:
        self.wardrobe = wardrobe or []
        self.fail_completion = fail_completion
        self.deleted: list[int] = []
        self.hidden: list[tuple[int, bool]] = []
        self.drafts: list[dict] = []
        self.missing: set[int] = set()
        self.photo_errors = 0

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def ensure_session(self) -> str:
        return "4711"

    async def wardrobe_items(self) -> list[dict]:
        return self.wardrobe

    async def get_item_edit(self, item_id: int) -> dict:
        if item_id in self.missing:
            raise VintedApiError("Artikel lesen", 404, "not found")
        return {"id": item_id, "is_draft": True,
                "photos": [{"id": 46000 + n, "image_no": n + 1} for n in range(len(self.drafts[-1]["assigned_photos"]) if self.drafts else 0)]}

    async def delete_item(self, item_id: int) -> None:
        self.deleted.append(item_id)

    async def set_hidden(self, item_id: int, hidden: bool) -> None:
        self.hidden.append((item_id, hidden))

    async def catalogs(self) -> list[dict]:
        return TREE

    async def search_brands(self, keyword: str) -> list[dict]:
        return [{"id": 3, "title": keyword}]

    async def size_groups(self, catalog_id: int) -> list[dict]:
        return []

    async def colors(self) -> list[dict]:
        return [{"id": 1, "title": "Schwarz"}, {"id": 12, "title": "Blau"}]

    async def upload_photo(self, image: bytes, temp_uuid: str, filename: str = "") -> int:
        return 900 + len(image)

    async def create_draft(self, draft: dict, sid: str) -> dict:
        self.drafts.append(draft)
        return {"id": 555}

    async def complete_draft(self, draft_id: int, draft: dict, sid: str) -> dict:
        self.completions = getattr(self, "completions", 0) + 1
        self.completed_photos = [p["id"] for p in draft["assigned_photos"]]
        if self.photo_errors > 0:
            self.photo_errors -= 1
            raise VintedApiError("Entwurf veröffentlichen", 422,
                                 '{"errors":[{"field":"photos","value":"Fehler beim Hochladen"}]}')
        if self.fail_completion:
            raise VintedApiError("Entwurf veröffentlichen", 422, '{"errors":[{"field":"size"}]}')
        return {"id": draft_id}


class FakeKA:
    deleted: list[str] = []

    def __init__(self) -> None:
        self.image_archive_dir = settings.kleinanzeigen_config_path / "image-archive"

    async def delete_listing(self, listing_id: str) -> bool:
        FakeKA.deleted.append(listing_id)
        return True

    async def download_single_listing(self, listing_id: str) -> None:
        return None


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch, tmp_path) -> VintedSyncService:
    import services.kleinanzeigen as ka_module
    import services.vinted_sync as sync_module

    monkeypatch.setattr(sync_module, "PHOTO_SETTLE_SECONDS", 0)
    monkeypatch.setattr(sync_module, "PHOTO_RETRY_SECONDS", 0)
    monkeypatch.setattr(settings, "kleinanzeigen_config_path", tmp_path)
    monkeypatch.setattr(settings, "vinted_backfill_per_run", 0)
    monkeypatch.setattr(settings, "vinted_missing_runs_before_delete", 2)
    monkeypatch.setattr(ka_module, "KleinanzeigenService", FakeKA)
    FakeKA.deleted = []
    return VintedSyncService()


def _ka(listing_id: str, title: str, status: str = "active") -> Listing:
    return Listing(listing_id=listing_id, title=title, price=20, status=status)


async def test_sold_on_vinted_deletes_kleinanzeigen(svc: VintedSyncService, monkeypatch) -> None:
    await vinted_mapping_repo.save("910001", status="active", vinted_item_id="71", title="Jacke A")
    client = FakeClient(wardrobe=[{"id": 71, "is_closed": True, "item_closing_action": "sold"}])
    monkeypatch.setattr(svc, "client", lambda: client)

    report = await svc.sync([_ka("910001", "Jacke A")])

    assert FakeKA.deleted == ["910001"]
    assert (await vinted_mapping_repo.get_by_ka("910001"))["status"] == "sold_vinted"
    assert any("verkauft" in line for line in report.lines)


async def test_gone_on_kleinanzeigen_deletes_vinted_after_two_runs(svc, monkeypatch) -> None:
    await vinted_mapping_repo.save("910002", status="active", vinted_item_id="72", title="Buch B")
    client = FakeClient(wardrobe=[{"id": 72}])
    monkeypatch.setattr(svc, "client", lambda: client)
    other = [_ka("999999", "Etwas anderes")]

    await svc.sync(other)
    assert client.deleted == []
    await svc.sync(other)
    assert client.deleted == [72]
    assert (await vinted_mapping_repo.get_by_ka("910002"))["status"] == "sold_ka"
    assert FakeKA.deleted == []


async def test_republish_with_same_title_is_followed(svc, monkeypatch) -> None:
    await vinted_mapping_repo.save("910003", status="active", vinted_item_id="73", title="Lampe C")
    client = FakeClient(wardrobe=[{"id": 73}])
    monkeypatch.setattr(svc, "client", lambda: client)

    await svc.sync([_ka("910033", "Lampe C")])

    assert client.deleted == []
    assert await vinted_mapping_repo.get_by_ka("910003") is None
    assert (await vinted_mapping_repo.get_by_ka("910033"))["vinted_item_id"] == "73"


async def test_reserved_hides_and_unreserved_shows(svc, monkeypatch) -> None:
    await vinted_mapping_repo.save("910004", status="active", vinted_item_id="74", title="Tasse D")
    client = FakeClient(wardrobe=[{"id": 74}])
    monkeypatch.setattr(svc, "client", lambda: client)

    await svc.sync([_ka("910004", "Tasse D", status="reserved")])
    assert client.hidden == [(74, True)]
    client.wardrobe = [{"id": 74, "is_hidden": True}]
    await svc.sync([_ka("910004", "Tasse D")])
    assert client.hidden == [(74, True), (74, False)]
    assert (await vinted_mapping_repo.get_by_ka("910004"))["status"] == "active"


async def test_deleted_by_hand_on_vinted_keeps_kleinanzeigen(svc, monkeypatch) -> None:
    await vinted_mapping_repo.save("910005", status="active", vinted_item_id="75", title="Vase E")
    client = FakeClient(wardrobe=[])
    client.missing = {75}
    monkeypatch.setattr(svc, "client", lambda: client)

    await svc.sync([_ka("910005", "Vase E")])

    assert FakeKA.deleted == []
    assert (await vinted_mapping_repo.get_by_ka("910005"))["status"] == "vinted_removed"


async def test_empty_kleinanzeigen_overview_aborts(svc, monkeypatch) -> None:
    await vinted_mapping_repo.save("910006", status="active", vinted_item_id="76", title="Uhr F")
    client = FakeClient(wardrobe=[{"id": 76}])
    monkeypatch.setattr(svc, "client", lambda: client)

    report = await svc.sync([])
    report = await svc.sync([])

    assert client.deleted == [] and report.errors


async def test_mirror_publishes_and_records_mapping(svc, monkeypatch) -> None:
    import services.openrouter as openrouter

    monkeypatch.setattr(openrouter, "get_text_llm", lambda temperature=0.0: FakeLLM([
        '{"group": "Damen > Kleidung"}',
        '{"catalog_id": 100, "brand": "Vaude", "condition": "good", "size": null, "color": "Blau"}',
    ]))
    client = FakeClient()
    client.photo_errors = 1
    result = await svc.mirror(MirrorInput(
        ka_listing_id="910007", title="Vaude Jacke", description="Gut. Abholung möglich.",
        price=30, images=[b"a", b"bb"], package_size="M",
    ), client)

    assert result.status == "active" and result.vinted_item_id == "555"
    assert client.drafts[0]["catalog_id"] == 100
    assert client.drafts[0]["brand_id"] == 3
    assert client.drafts[0]["color_ids"] == [12]
    assert client.completions == 2  # one retry after the photo processing error
    assert client.completed_photos == [46000, 46001]  # stored ids, not the upload's temp ids
    row = await vinted_mapping_repo.get_by_ka("910007")
    assert row["status"] == "active" and row["vinted_url"].endswith("/items/555")


async def test_mirror_keeps_draft_when_vinted_rejects(svc, monkeypatch) -> None:
    import services.openrouter as openrouter

    monkeypatch.setattr(openrouter, "get_text_llm", lambda temperature=0.0: FakeLLM([
        '{"group": "Damen > Kleidung"}', '{"catalog_id": 101, "condition": "good"}',
    ]))
    result = await svc.mirror(MirrorInput(
        ka_listing_id="910008", title="Kleid", description="Sommerkleid", price=15,
        images=[b"x"], package_size="S",
    ), FakeClient(fail_completion=True))

    assert result.status == "draft"
    assert (await vinted_mapping_repo.get_by_ka("910008"))["status"] == "draft"


async def test_mirror_skips_pickup_only(svc) -> None:
    result = await svc.mirror(MirrorInput(
        ka_listing_id="910009", title="Schrank", description="Groß", price=50,
        images=[b"x"], package_size="PICKUP",
    ), FakeClient())
    assert result.status == "skipped"


async def test_rekey_follows_bot_republish(svc) -> None:
    await vinted_mapping_repo.save("910010", status="active", vinted_item_id="80", title="Alt")
    await svc.follow_republish("910010", "910011", "Neu")
    row = await vinted_mapping_repo.get_by_ka("910011")
    assert row is not None and row["vinted_item_id"] == "80" and row["title"] == "Neu"


async def test_sold_on_vinted_finds_republished_kleinanzeigen_listing(svc, monkeypatch) -> None:
    await vinted_mapping_repo.save("910012", status="active", vinted_item_id="81", title="Rad G")
    client = FakeClient(wardrobe=[{"id": 81, "is_closed": True, "item_closing_action": "sold"}])
    monkeypatch.setattr(svc, "client", lambda: client)

    await svc.sync([_ka("910013", "Rad G")])

    assert FakeKA.deleted == ["910013"]
    assert (await vinted_mapping_repo.get_by_ka("910013"))["status"] == "sold_vinted"


def test_split_login_message_is_joined_and_filtered() -> None:
    from services.vinted_client import session_cookies

    header = (
        "datadome=dd1; cf_clearance=cf1; _ga=x; access_token_web=AAA.BBB.CCC; "
        "refresh_token_web=RRR.SSS.TTT; _vinted_fr_session=sess; v_uid=42"
    )
    parts = [header[:47], header[47:120], header[120:]]  # Telegram cuts anywhere
    cookies = session_cookies(parse_cookie_header("".join(parts)))
    assert cookies == {
        "access_token_web": "AAA.BBB.CCC", "refresh_token_web": "RRR.SSS.TTT",
        "_vinted_fr_session": "sess", "v_uid": "42",
    }


async def test_logged_in_token_wins_over_anonymous_cookie(tmp_path) -> None:
    from services.vinted_client import VintedClient

    anon = _jwt({"exp": time.time() + 9000, "anid": "a1"})
    user = _jwt({"sub": "3211", "exp": time.time() + 3600, "anid": "a1"})
    jar = [
        SimpleNamespace(name="access_token_web", value=user, domain=".vinted.de", path="/"),
        SimpleNamespace(name="access_token_web", value=anon, domain=".www.vinted.de", path="/"),
        SimpleNamespace(name="refresh_token_web", value=anon, domain=".www.vinted.de", path="/"),
        SimpleNamespace(name="_ga", value="x", domain=".google.com", path="/"),
    ]

    async def get_all() -> list:
        return jar

    client = VintedClient(base_url="https://www.vinted.de", profile_dir=tmp_path)
    client.browser = SimpleNamespace(cookies=SimpleNamespace(get_all=get_all))
    cookies = await client._cookies()
    assert cookies["access_token_web"] == user and "_ga" not in cookies
    with pytest.raises(Exception, match="nur anonym"):
        jar[0] = SimpleNamespace(name="access_token_web", value=anon, domain=".vinted.de", path="/")
        await client.ensure_session()


async def test_backfill_skipped_while_session_missing(svc, monkeypatch) -> None:
    from services.vinted_client import VintedAuthError

    class NoSession(FakeClient):
        async def ensure_session(self) -> str:
            raise VintedAuthError("nur anonym")

    calls: list[str] = []

    async def prepare(ka, listings, report):
        calls.append("prep")
        return []

    monkeypatch.setattr(svc, "_prepare_backfill", prepare)
    monkeypatch.setattr(svc, "client", lambda: NoSession())
    report = await svc.sync([_ka("1", "A")])
    assert report.auth_required and calls == ["prep"] and svc.auth_required()
    await svc.sync([_ka("1", "A")])
    assert calls == ["prep"]
    monkeypatch.setattr(svc, "client", lambda: FakeClient())
    await svc.sync([_ka("1", "A")])
    assert not svc.auth_required()


def test_isbn_helpers_and_error_format() -> None:
    assert vm.normalize_isbn("978-3-16-148410-0") == "9783161484100"
    assert vm.normalize_isbn("978-3-16-148410-1") is None
    assert vm.normalize_isbn("0-306-40615-2") == "0306406152"
    assert vm.find_isbn("Gut erhalten. ISBN 978 3 16 148410 0, Taschenbuch") == "9783161484100"
    assert vm.find_isbn("Tel 0171 1234567") is None
    assert vm.needs_isbn("Bücher & andere Medien > Bücher > Sachbücher")
    body = '{"code":99,"errors":[{"field":"photos","value":"Fehler beim Hochladen"},{"field":"isbn","value":"Bitte gib eine ISBN an"}]}'
    assert vm.format_validation_errors(body) == "photos: Fehler beim Hochladen; isbn: Bitte gib eine ISBN an"
    assert vm.pick_color([{"id": 1, "title": "Schwarz"}], "schwarz") == 1


def test_to_jpeg_converts_webp() -> None:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), "red").save(buf, format="WEBP")
    jpeg = vm.to_jpeg(buf.getvalue())
    assert jpeg[:3] == b"\xff\xd8\xff"
    assert max(Image.open(io.BytesIO(jpeg)).size) == vm.JPEG_MAX_SIDE


async def test_rejected_bot_draft_is_refilled_not_duplicated(svc, monkeypatch) -> None:
    import services.openrouter as openrouter

    monkeypatch.setattr(openrouter, "get_text_llm", lambda temperature=0.0: FakeLLM([
        '{"group": "Damen > Kleidung"}', '{"catalog_id": 100, "condition": "good", "color": "Schwarz"}',
    ]))
    await vinted_mapping_repo.save("910020", status="draft", vinted_item_id="90", title="Schlauch H",
                                   last_error="photos", count_attempt=True)
    client = FakeClient()
    updated: list[int] = []

    async def update_draft(draft_id: int, draft: dict, sid: str) -> dict:
        updated.append(draft_id)
        return {}

    client.update_draft = update_draft
    result = await svc.mirror(MirrorInput(
        ka_listing_id="910020", title="Schlauch H", description="Neu", price=6,
        images=[b"x"], package_size="S",
    ), client)

    assert updated == [90] and client.drafts == [] and client.deleted == []
    assert result.status == "active"
    assert (await vinted_mapping_repo.get_by_ka("910020"))["status"] == "active"


async def test_book_isbn_from_open_library(svc, monkeypatch) -> None:
    import services.openrouter as openrouter

    monkeypatch.setattr(openrouter, "get_text_llm", lambda temperature=0.0: FakeLLM([
        '{"group": "Unterhaltung > Bücher"}', '{"catalog_id": 200, "condition": "good", "color": "Schwarz"}',
    ]))

    import services.vinted_sync as sync_module

    async def lookup(**kwargs) -> tuple:
        assert kwargs["title"].startswith("Buch:")
        return "9783161484100", "Katalog (DNB/Open Library)"

    monkeypatch.setattr(sync_module, "find_book_isbn", lookup)
    monkeypatch.setattr(openrouter, "get_vision_llm", lambda temperature=0.0: None)
    client = FakeClient()
    result = await svc.mirror(MirrorInput(
        ka_listing_id="910021", title="Buch: Gaming revolutioniert die Welt - P", description="Sachbuch",
        price=8, images=[b"x"], package_size="S",
    ), client)
    assert client.drafts[0]["isbn"] == "9783161484100"
    assert "Katalog" in result.message


async def test_missing_isbn_keeps_draft_without_retry(svc, monkeypatch) -> None:
    import services.openrouter as openrouter

    class IsbnClient(FakeClient):
        async def complete_draft(self, draft_id: int, draft: dict, sid: str) -> dict:
            raise VintedApiError("Entwurf veröffentlichen", 422,
                                 '{"errors":[{"field":"isbn","value":"Bitte gib eine ISBN an"}]}')

    monkeypatch.setattr(openrouter, "get_text_llm", lambda temperature=0.0: FakeLLM([
        '{"group": "Unterhaltung > Bücher"}', '{"catalog_id": 200, "condition": "good"}',
    ]))

    import services.vinted_sync as sync_module

    async def lookup(**kwargs) -> tuple:
        return None, ""

    monkeypatch.setattr(sync_module, "find_book_isbn", lookup)
    monkeypatch.setattr(openrouter, "get_vision_llm", lambda temperature=0.0: None)
    result = await svc.mirror(MirrorInput(
        ka_listing_id="910022", title="Buch: Unbekannt", description="Roman", price=5,
        images=[b"x"], package_size="S",
    ), IsbnClient())
    assert result.status == "draft" and not result.retry and "Vinted-App" in result.message
    row = await vinted_mapping_repo.get_by_ka("910022")
    assert row["attempts"] >= 3  # the reconcile step leaves this draft alone


async def test_book_gets_language_attribute(svc, monkeypatch) -> None:
    import services.openrouter as openrouter
    import services.vinted_sync as sync_module

    monkeypatch.setattr(openrouter, "get_text_llm", lambda temperature=0.0: FakeLLM([
        '{"group": "Unterhaltung > Bücher"}',
        '{"catalog_id": 200, "condition": "good", "isbn": "978-3-16-148410-0", "book_language": "Englisch"}',
    ]))
    monkeypatch.setattr(openrouter, "get_vision_llm", lambda temperature=0.0: None)
    client = FakeClient()
    await svc.mirror(MirrorInput(
        ka_listing_id="910030", title="Buch: Dune", description="Roman", price=5,
        images=[b"x"], package_size="S",
    ), client)
    attrs = client.drafts[0]["item_attributes"]
    assert {"code": "language_book", "ids": [6435]} in attrs
    assert client.drafts[0]["isbn"] == "9783161484100"
    assert vm.book_language_id(None) == 6437


async def test_reset_attempts_reopens_final_drafts(svc) -> None:
    await vinted_mapping_repo.save("910031", status="draft", vinted_item_id="95", title="X")
    await vinted_mapping_repo.set_attempts("910031", 3)
    assert await vinted_mapping_repo.reset_attempts() >= 1
    assert (await vinted_mapping_repo.get_by_ka("910031"))["attempts"] == 0
