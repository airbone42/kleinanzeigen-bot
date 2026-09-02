"""Unit tests for the Kleinanzeigen CLI wrapper service."""
import asyncio
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from models.draft import Draft
from models.listing import Listing


@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory with required structure."""
    ads_dir = tmp_path / "ads"
    ads_dir.mkdir()
    (tmp_path / "downloaded-ads").mkdir()

    # Create minimal config.yaml
    config = {
        "login": {
            "username": "test@example.com",
            "password": "testpass",
        },
        "ad_defaults": {
            "contact": {
                "name": "Test User",
                "phone": "+49123456789",
                "street": "Teststrasse 1",
                "zipcode": "12345",
            },
            "republication_interval": 7,
        },
    }
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config, f)

    return tmp_path


@pytest.fixture
def service(temp_config_dir: Path) -> "KleinanzeigenService":
    """Create a service instance with temp config."""
    from services.kleinanzeigen import KleinanzeigenService

    with patch("services.kleinanzeigen.settings") as mock_settings:
        mock_settings.kleinanzeigen_config_path = temp_config_dir
        svc = KleinanzeigenService()
        svc.config_path = temp_config_dir
        svc.config_file = temp_config_dir / "config.yaml"
        svc.ads_dir = temp_config_dir / "ads"
        svc.downloaded_ads_dir = temp_config_dir / "downloaded-ads"
        return svc


class TestKleinanzeigenService:
    """Tests for the KleinanzeigenService."""

    def test_build_ad_yaml_valid_draft(self, service: "KleinanzeigenService") -> None:
        """Test YAML generation from a valid draft."""
        draft = Draft(
            chat_id=123,
            title="IKEA Kallax Regal weiss",
            description="Gut erhaltenes IKEA Kallax Regal in weiss. Keine Mängel.",
            price=45.0,
            category="Möbel",
        )

        data = service._build_ad_yaml(draft)

        assert data["active"] is True
        assert data["title"] == "IKEA Kallax Regal weiss"
        assert data["price"] == 45
        assert data["price_type"] == "NEGOTIABLE"
        assert data["type"] == "OFFER"
        assert "description" in data
        assert "contact" in data
        assert data["republication_interval"] == 7

    def test_build_ad_yaml_short_title_padded(
        self, service: "KleinanzeigenService"
    ) -> None:
        """Test that short titles are padded to meet minimum length."""
        draft = Draft(
            chat_id=123,
            title="Regal",  # 5 chars, needs to be at least 10
            description="Ein Regal zu verkaufen.",
            price=20.0,
        )

        data = service._build_ad_yaml(draft)
        assert len(data["title"]) >= 10

    def test_build_ad_yaml_give_away_for_zero_price(
        self, service: "KleinanzeigenService"
    ) -> None:
        """Test that price_type is GIVE_AWAY for price < 1."""
        draft = Draft(
            chat_id=123,
            title="Kostenloser Artikel hier",
            description="Gratis abzugeben.",
            price=0.0,
        )

        data = service._build_ad_yaml(draft)
        assert data["price_type"] == "GIVE_AWAY"

    def test_parse_ad_yaml_valid(
        self, service: "KleinanzeigenService", tmp_path: Path
    ) -> None:
        """Test parsing a valid ad YAML file."""
        from datetime import datetime, timezone

        ad_data = {
            "active": True,
            "id": 123456789,
            "title": "Test Artikel",
            "description": "Gut erhaltener Test Artikel",
            "price": 25,
            "price_type": "FIXED",
            "category": "Sonstiges",
            "republication_interval": 7,
            "created_on": "2026-03-10T00:00:00",
        }

        yaml_file = tmp_path / "test_ad.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(ad_data, f)

        listing = service._parse_ad_yaml(yaml_file)

        assert listing is not None
        assert listing.listing_id == "123456789"
        assert listing.title == "Test Artikel"
        assert listing.price == 25.0
        assert listing.days_remaining is not None

    def test_parse_ad_yaml_none_republication_interval_uses_default(
        self, service: "KleinanzeigenService", tmp_path: Path
    ) -> None:
        """Test parsing still computes days when republication_interval is None."""
        ad_data = {
            "active": True,
            "id": 123456780,
            "title": "Test Artikel 2",
            "description": "Gut erhaltener Test Artikel",
            "price": 25,
            "price_type": "FIXED",
            "category": "Sonstiges",
            "republication_interval": None,
            "created_on": "2026-03-01T00:00:00+00:00",
        }

        yaml_file = tmp_path / "test_ad_none_interval.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(ad_data, f)

        listing = service._parse_ad_yaml(yaml_file)

        assert listing is not None
        assert listing.days_remaining is not None

    def test_parse_ad_yaml_old_ad_days_remaining_cycles(
        self, service: "KleinanzeigenService", tmp_path: Path
    ) -> None:
        """Old ads compute days_remaining based on the current cycle."""
        from datetime import datetime, timezone, timedelta
        interval = service._load_default_republication_interval()
        created_dt = datetime.now(timezone.utc) - timedelta(days=interval * 2 + 5)
        ad_data = {
            "active": True,
            "id": 999000001,
            "title": "Uralte Anzeige",
            "description": "Schon lange drin",
            "price": 50,
            "category": "Sonstiges",
            "republication_interval": None,  # → config default
            "created_on": created_dt.isoformat(),
        }
        yaml_file = tmp_path / "old_ad.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(ad_data, f)

        listing = service._parse_ad_yaml(yaml_file)

        assert listing is not None
        elapsed = (datetime.now(timezone.utc) - created_dt).days
        expected = interval - (elapsed % interval)
        assert listing.days_remaining == expected
        assert listing.is_expiring_soon is (expected < 10)

    def test_parse_ad_yaml_inactive_skipped(
        self, service: "KleinanzeigenService", tmp_path: Path
    ) -> None:
        """Test that inactive ad YAML files are skipped."""
        ad_data = {
            "active": False,
            "id": 123456789,
            "title": "Inactive Ad",
            "description": "Should be skipped",
            "price": 25,
        }

        yaml_file = tmp_path / "inactive_ad.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(ad_data, f)

        listing = service._parse_ad_yaml(yaml_file)
        assert listing is None

    def test_parse_ad_yaml_no_id_skipped(
        self, service: "KleinanzeigenService", tmp_path: Path
    ) -> None:
        """Test that YAML files without ID (unpublished) are skipped."""
        ad_data = {
            "active": True,
            "title": "New Draft",
            "description": "Not yet published",
            "price": 25,
        }

        yaml_file = tmp_path / "new_draft.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(ad_data, f)

        listing = service._parse_ad_yaml(yaml_file)
        assert listing is None

    def test_extract_listing_url(self, service: "KleinanzeigenService") -> None:
        """Test URL extraction from CLI output."""
        output = (
            "Publishing ad... "
            "https://www.kleinanzeigen.de/s-anzeige/test-artikel/12345678-123 "
            "Done."
        )
        url = service._extract_listing_url(output)
        assert url is not None
        assert "kleinanzeigen.de" in url

    def test_extract_listing_url_not_found(
        self, service: "KleinanzeigenService"
    ) -> None:
        """Test URL extraction when URL is not in output."""
        output = "Publishing... Error occurred"
        url = service._extract_listing_url(output)
        assert url is None

    @pytest.mark.asyncio
    async def test_get_active_listings_parses_yamls(
        self, service: "KleinanzeigenService", temp_config_dir: Path
    ) -> None:
        """Test that get_active_listings reads YAML files correctly."""
        from datetime import datetime, timezone

        # Create a published ad YAML
        ad_data = {
            "active": True,
            "id": 987654321,
            "title": "Test Listing",
            "description": "Test description",
            "price": 30,
            "category": "Sonstiges",
            "republication_interval": 7,
            "created_on": "2026-02-28T00:00:00",
        }

        ad_dir = service.ads_dir / "ad_test"
        ad_dir.mkdir()
        yaml_file = ad_dir / "ad.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(ad_data, f)

        # Mock CLI download to succeed (but don't actually run it)
        with patch.object(service, "_run_cli") as mock_cli:
            mock_cli.return_value = (0, "", "")
            listings = await service.get_active_listings(refresh=False)

        assert len(listings) == 1
        assert listings[0].listing_id == "987654321"
        assert listings[0].title == "Test Listing"

    @pytest.mark.asyncio
    async def test_get_active_listings_refresh_purges_stale_download_cache(
        self, service: "KleinanzeigenService"
    ) -> None:
        """Refresh download should purge stale YAMLs before new download output."""
        from datetime import datetime, timezone

        stale_dir = service.downloaded_ads_dir / "ad_111_stale"
        stale_dir.mkdir(parents=True)
        with open(stale_dir / "ad_111.yaml", "w") as f:
            yaml.dump(
                {
                    "active": True,
                    "id": 111,
                    "title": "Stale Listing",
                    "description": "Old cached ad",
                    "price": 10,
                    "created_on": datetime.now(timezone.utc).isoformat(),
                },
                f,
            )

        async def _run_cli(*_args, **_kwargs):
            new_dir = service.downloaded_ads_dir / "ad_222_new"
            new_dir.mkdir(parents=True, exist_ok=True)
            with open(new_dir / "ad_222.yaml", "w") as f:
                yaml.dump(
                    {
                        "active": True,
                        "id": 222,
                        "title": "Fresh Listing",
                        "description": "New download",
                        "price": 15,
                        "created_on": datetime.now(timezone.utc).isoformat(),
                    },
                    f,
                )
            return 0, "", ""

        with patch.object(service, "_pre_session_setup", new=AsyncMock()):
            with patch.object(service, "_run_cli", new=AsyncMock(side_effect=_run_cli)):
                listings = await service.get_active_listings(refresh=True)

        assert {listing.listing_id for listing in listings} == {"222"}

    @pytest.mark.asyncio
    async def test_get_active_listings_no_cache_fallback_raises(
        self, service: "KleinanzeigenService"
    ) -> None:
        from services.kleinanzeigen import KleinanzeigenError

        async def _run_cli(*_args, **_kwargs):
            return 1, "", "boom"

        with patch.object(service, "_pre_session_setup", new=AsyncMock()):
            with patch.object(service, "_run_cli", new=AsyncMock(side_effect=_run_cli)):
                with pytest.raises(KleinanzeigenError):
                    await service.get_active_listings(allow_cache_fallback=False)

    @pytest.mark.asyncio
    async def test_get_published_listings_summary_parses_end_date(
        self, service: "KleinanzeigenService"
    ) -> None:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Europe/Berlin")
        today = datetime.now(tz).date()
        end_date = (today + timedelta(days=5)).strftime("%d.%m.%Y")
        creation_date = (today - timedelta(days=1)).strftime("%d.%m.%Y")
        ads = [
            {
                "id": 123,
                "title": "Test Ad",
                "price": 10,
                "category": "Test",
                "endDate": end_date,
                "creationDate": creation_date,
            }
        ]

        with patch.object(service, "_fetch_published_ads_raw", new=AsyncMock(return_value=ads)):
            listings = await service.get_published_listings_summary()

        assert len(listings) == 1
        assert listings[0].listing_id == "123"
        assert listings[0].days_remaining == 5
        assert listings[0].is_expiring_soon is True

    @pytest.mark.asyncio
    async def test_renew_listing_calls_publish_with_numeric_id(
        self, service: "KleinanzeigenService"
    ) -> None:
        """Test renewing an existing listing by numeric ID."""
        ad_data = {
            "active": True,
            "id": 987654321,
            "title": "Renewable Listing",
            "description": "Test description",
            "price": 30,
            "category": "Sonstiges",
        }
        downloaded_dir = service.downloaded_ads_dir / "ad_987654321_from_download"
        downloaded_dir.mkdir(parents=True)
        with open(downloaded_dir / "ad_987654321.yaml", "w") as f:
            yaml.dump(ad_data, f)

        with patch.object(service, "_run_cli", new=AsyncMock(return_value=(0, "", ""))) as mock_cli:
            success = await service.renew_listing("987654321")

        assert success is True
        mock_cli.assert_awaited_once_with("publish", "--ads", "987654321", timeout=300)
        assert (service.ads_dir / "ad_987654321" / "ad.yaml").exists()

    @pytest.mark.asyncio
    async def test_renew_listing_raises_when_cli_reports_no_outdated_ads(
        self, service: "KleinanzeigenService"
    ) -> None:
        """Test renew failure on no-op CLI output."""
        from services.kleinanzeigen import KleinanzeigenError

        ad_data = {
            "active": True,
            "id": 111222333,
            "title": "Renewable Listing",
            "description": "Test description",
            "price": 30,
            "category": "Sonstiges",
        }
        downloaded_dir = service.downloaded_ads_dir / "ad_111222333_from_download"
        downloaded_dir.mkdir(parents=True)
        with open(downloaded_dir / "ad_111222333.yaml", "w") as f:
            yaml.dump(ad_data, f)

        noop_output = "[INFO] DONE: No new/outdated ads found."
        with patch.object(service, "_run_cli", new=AsyncMock(return_value=(0, noop_output, ""))):
            with pytest.raises(KleinanzeigenError):
                await service.renew_listing("111222333")

    def test_ensure_listing_config_applies_overrides(
        self, service: "KleinanzeigenService"
    ) -> None:
        """Test that proactive renew overrides title/description/price in ad.yaml."""
        ad_data = {
            "active": True,
            "id": 444555666,
            "title": "Alt",
            "description": "Altbeschreibung",
            "price": 30,
            "price_type": "FIXED",
            "category": "Sonstiges",
            "images": [],
        }
        downloaded_dir = service.downloaded_ads_dir / "ad_444555666_from_download"
        downloaded_dir.mkdir(parents=True)
        with open(downloaded_dir / "ad_444555666.yaml", "w") as f:
            yaml.dump(ad_data, f)

        service._ensure_listing_config_for_publish(
            "444555666",
            title="Neu",
            description="Neu beschrieben",
            price=19.0,
        )

        with open(service.ads_dir / "ad_444555666" / "ad.yaml", encoding="utf-8") as f:
            updated = yaml.safe_load(f)
        assert updated["title"] == "Neu"
        assert updated["description"] == "Neu beschrieben"
        assert updated["price"] == 19

    @pytest.mark.asyncio
    async def test_get_active_listings_reads_downloaded_ads(
        self, service: "KleinanzeigenService"
    ) -> None:
        """Test that get_active_listings parses YAMLs from downloaded-ads."""
        ad_data = {
            "active": True,
            "id": 123123123,
            "title": "Downloaded Listing",
            "description": "From downloaded-ads",
            "price": 42,
            "category": "Sonstiges",
        }

        ad_dir = service.downloaded_ads_dir / "ad_123123123_test"
        ad_dir.mkdir(parents=True)
        yaml_file = ad_dir / "ad_123123123.yaml"
        with open(yaml_file, "w") as f:
            yaml.dump(ad_data, f)

        with patch.object(service, "_run_cli") as mock_cli:
            mock_cli.return_value = (0, "", "")
            listings = await service.get_active_listings(refresh=False)

        assert len(listings) == 1
        assert listings[0].listing_id == "123123123"
        assert listings[0].title == "Downloaded Listing"

    @pytest.mark.asyncio
    async def test_get_active_listings_deduplicates_ids_across_dirs(
        self, service: "KleinanzeigenService"
    ) -> None:
        """Test that duplicate listing IDs are returned only once."""
        ad_data = {
            "active": True,
            "id": 999888777,
            "title": "Duplicate Listing",
            "description": "same id",
            "price": 15,
            "category": "Sonstiges",
        }

        downloaded_dir = service.downloaded_ads_dir / "ad_999888777_from_download"
        downloaded_dir.mkdir(parents=True)
        with open(downloaded_dir / "ad_999888777.yaml", "w") as f:
            yaml.dump(ad_data, f)

        ads_dir = service.ads_dir / "ad_999888777_local"
        ads_dir.mkdir(parents=True)
        with open(ads_dir / "ad.yaml", "w") as f:
            yaml.dump(ad_data, f)

        with patch.object(service, "_run_cli") as mock_cli:
            mock_cli.return_value = (0, "", "")
            listings = await service.get_active_listings(refresh=False)

        assert len(listings) == 1
        assert listings[0].listing_id == "999888777"

    @pytest.mark.asyncio
    async def test_get_active_listings_refresh_false_skips_download(
        self, service: "KleinanzeigenService"
    ) -> None:
        """Discogs sync path should read cached YAMLs without CLI download."""
        ad_data = {
            "active": True,
            "id": 222333444,
            "title": "Cached Listing",
            "description": "no download needed",
            "price": 20,
            "category": "Sonstiges",
        }

        ad_dir = service.downloaded_ads_dir / "ad_222333444_cached"
        ad_dir.mkdir(parents=True)
        with open(ad_dir / "ad_222333444.yaml", "w") as f:
            yaml.dump(ad_data, f)

        with patch.object(service, "_run_cli", new=AsyncMock(return_value=(0, "", ""))) as mock_cli:
            listings = await service.get_active_listings(refresh=False)

        assert len(listings) == 1
        assert listings[0].listing_id == "222333444"
        mock_cli.assert_not_awaited()


class TestListingModel:
    """Tests for the Listing model."""

    def test_is_expiring_soon_true(self) -> None:
        """Test is_expiring_soon returns True for < 10 days remaining."""
        listing = Listing(
            listing_id="123",
            title="Test",
            price=25.0,
            days_remaining=9,
        )
        assert listing.is_expiring_soon is True

    def test_is_expiring_soon_false(self) -> None:
        """Test is_expiring_soon returns False for >= 10 days remaining."""
        listing = Listing(
            listing_id="123",
            title="Test",
            price=25.0,
            days_remaining=10,
        )
        assert listing.is_expiring_soon is False

    def test_default_republication_interval_60_days(self) -> None:
        """Test that default republication interval is 60 days (not 7)."""
        from services.kleinanzeigen import DEFAULT_REPUBLICATION_INTERVAL
        assert DEFAULT_REPUBLICATION_INTERVAL == 60

    def test_new_listing_not_expiring_soon(self) -> None:
        """Test that a listing created yesterday (59 days remaining) is not expiring soon."""
        listing = Listing(
            listing_id="123",
            title="Test",
            price=25.0,
            days_remaining=59,
        )
        assert listing.is_expiring_soon is False

    def test_should_delete_cheap_listing(self) -> None:
        """Test should_delete returns True for price < 5 EUR."""
        listing = Listing(
            listing_id="123",
            title="Test",
            price=3.99,
        )
        assert listing.should_delete is True

    def test_should_delete_valuable_listing(self) -> None:
        """Test should_delete returns False for price >= 5 EUR."""
        listing = Listing(
            listing_id="123",
            title="Test",
            price=5.00,
        )
        assert listing.should_delete is False


class TestRepublishWithOptimization:
    """Tests for republish_with_optimization – URL/ID extraction paths."""

    def _make_ad_dir(self, service: "KleinanzeigenService", listing_id: str) -> Path:
        ad_dir = service.ads_dir / f"ad_{listing_id}"
        ad_dir.mkdir(parents=True, exist_ok=True)
        ad_data = {
            "active": True,
            "id": int(listing_id),
            "title": "Altes Regal",
            "description": "Gut erhalten",
            "price": 20,
            "category": "Sonstiges",
        }
        with open(ad_dir / "ad.yaml", "w") as f:
            yaml.dump(ad_data, f)
        return ad_dir

    @pytest.mark.asyncio
    async def test_republish_success_url_in_output(self, service: "KleinanzeigenService") -> None:
        """URL found directly in CLI output → republish returns it."""
        from services.kleinanzeigen import KleinanzeigenService
        self._make_ad_dir(service, "111222333")

        stdout = "Ad published: https://www.kleinanzeigen.de/s-anzeige/altes-regal/999888777"
        mock_delete = AsyncMock(return_value=True)
        with patch.object(service, "_run_cli", new=AsyncMock(return_value=(0, stdout, ""))), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "delete_listing", new=mock_delete):
            result, warning = await service.republish_with_optimization("111222333", title="Neues Regal")

        assert "999888777" in result
        assert warning is None
        mock_delete.assert_awaited_once_with("111222333")

    @pytest.mark.asyncio
    async def test_republish_success_id_in_output(self, service: "KleinanzeigenService") -> None:
        """No URL in output but bare ID present → builds URL from ID."""
        from services.kleinanzeigen import KleinanzeigenService
        self._make_ad_dir(service, "111222334")

        stdout = "Listing /999888700 created successfully."
        mock_delete = AsyncMock(return_value=True)
        with patch.object(service, "_run_cli", new=AsyncMock(return_value=(0, stdout, ""))), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "delete_listing", new=mock_delete):
            result, _ = await service.republish_with_optimization("111222334")

        assert "999888700" in result
        mock_delete.assert_awaited_once_with("111222334")

    @pytest.mark.asyncio
    async def test_republish_success_id_from_yaml(self, service: "KleinanzeigenService") -> None:
        """No URL or ID in output; bot writes ID back into YAML → builds URL."""
        from services.kleinanzeigen import KleinanzeigenService
        ad_dir = self._make_ad_dir(service, "111222335")

        async def fake_run_cli(*args, **kwargs):
            # Simulate kleinanzeigen-bot writing the new id into YAML after publish
            new_yaml = service.ads_dir / "ad_new" / "ad.yaml"
            for d in service.ads_dir.iterdir():
                candidate = d / "ad.yaml"
                if candidate.exists():
                    import yaml as _yaml
                    with open(candidate) as f:
                        data = _yaml.safe_load(f) or {}
                    if "id" not in data or not data["id"]:
                        data["id"] = 777666555
                        with open(candidate, "w") as f:
                            _yaml.safe_dump(data, f)
            return (0, "Done.", "")

        mock_delete = AsyncMock(return_value=True)
        with patch.object(service, "_run_cli", new=AsyncMock(side_effect=fake_run_cli)), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "delete_listing", new=mock_delete):
            result, _ = await service.republish_with_optimization("111222335")

        assert "777666555" in result
        mock_delete.assert_awaited_once_with("111222335")

    @pytest.mark.asyncio
    async def test_republish_raises_when_no_id_found(self, service: "KleinanzeigenService") -> None:
        """CLI returns 0 but no URL/ID anywhere → raises KleinanzeigenError, old listing kept."""
        from services.kleinanzeigen import KleinanzeigenError
        self._make_ad_dir(service, "111222336")

        with patch.object(service, "_run_cli", new=AsyncMock(return_value=(0, "Done.", ""))), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "delete_listing", new=AsyncMock()) as mock_delete:
            with pytest.raises(KleinanzeigenError, match="Neue Anzeige konnte nicht bestätigt werden"):
                await service.republish_with_optimization("111222336")

        mock_delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_republish_raises_when_cli_fails(self, service: "KleinanzeigenService") -> None:
        """CLI returns non-zero → raises KleinanzeigenError."""
        from services.kleinanzeigen import KleinanzeigenError
        self._make_ad_dir(service, "111222337")

        with patch.object(service, "_run_cli", new=AsyncMock(return_value=(1, "", "Login failed"))), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "delete_listing", new=AsyncMock()) as mock_delete:
            with pytest.raises(KleinanzeigenError):
                await service.republish_with_optimization("111222337")

        mock_delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_republish_raises_when_bot_published_zero_ads(
        self, service: "KleinanzeigenService"
    ) -> None:
        """Regression: CLI exits 0 but reports '0 ads published' (e.g. 'failed after retries')
        → raises KleinanzeigenError with clear message, old listing kept, tmp dir cleaned up."""
        from services.kleinanzeigen import KleinanzeigenError
        self._make_ad_dir(service, "111222338")

        bot_output = (
            "[INFO] DONE: (Re-)published 0 ads (1 failed after retries)\n"
        )
        mock_delete = AsyncMock(return_value=True)
        with patch.object(service, "_run_cli", new=AsyncMock(return_value=(0, "", bot_output))), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "delete_listing", new=mock_delete):
            with pytest.raises(KleinanzeigenError, match="Veröffentlichung fehlgeschlagen"):
                await service.republish_with_optimization("111222338")

        mock_delete.assert_not_awaited()
        # Temporary ad dir must be cleaned up
        remaining_dirs = [d for d in service.ads_dir.iterdir() if d.is_dir() and d.name.startswith("ad_")]
        assert not remaining_dirs, f"Temp dirs not cleaned up: {remaining_dirs}"


class TestPublishNewAdShippingRetry:
    """Tests for _publish_new_ad shipping-dialog retry logic."""

    def _make_ad_dir_with_shipping(self, service: "KleinanzeigenService") -> tuple[Path, Path]:
        ad_dir = service.ads_dir / "ad_shoetest"
        ad_dir.mkdir(parents=True, exist_ok=True)
        ad_data = {
            "active": True,
            "title": "Specialized Recon",
            "description": "Tolle Schuhe",
            "price": 80,
            "category": "210/217/herren",
            "shipping_type": "SHIPPING",
            "shipping_options": ["DHL_5", "Hermes_M"],
        }
        new_yaml = ad_dir / "ad.yaml"
        with open(new_yaml, "w") as f:
            yaml.dump(ad_data, f)
        return ad_dir, new_yaml

    @pytest.mark.asyncio
    async def test_category_retry_on_dialog_failure_succeeds(
        self, service: "KleinanzeigenService"
    ) -> None:
        """First publish fails with 'Andere Versandmethoden'; category retry succeeds."""
        ad_dir, new_yaml = self._make_ad_dir_with_shipping(service)

        call_count = 0
        yaml_data_on_retry: dict = {}

        async def fake_run_cli(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (1, "", "XPath '//dialog//button[contains(., \"Andere Versandmethoden\")]' not found")
            with open(new_yaml) as f:
                yaml_data_on_retry.update(yaml.safe_load(f) or {})
            return (0, "Ad published: https://www.kleinanzeigen.de/s-anzeige/schuhe/888777666", "")

        mock_delete = AsyncMock(return_value=True)
        mock_suggest = AsyncMock(return_value=["Mode & Beauty > Schuhe", "Mode & Beauty > Herren"])
        with patch.object(service, "_run_cli", new=AsyncMock(side_effect=fake_run_cli)), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "delete_listing", new=mock_delete), \
             patch.object(service, "_suggest_alternative_categories", new=mock_suggest):
            result, warning = await service._publish_new_ad(ad_dir, new_yaml, "OLD123")

        assert "888777666" in result
        assert warning is None
        assert call_count == 2
        mock_delete.assert_awaited_once_with("OLD123")
        assert yaml_data_on_retry.get("category") == "Mode & Beauty > Schuhe"
        assert "special_attributes" not in yaml_data_on_retry

    @pytest.mark.asyncio
    async def test_no_retry_on_unrelated_failure(self, service: "KleinanzeigenService") -> None:
        """A non-dialog failure (e.g. login error) is NOT retried."""
        from services.kleinanzeigen import KleinanzeigenError
        ad_dir, new_yaml = self._make_ad_dir_with_shipping(service)

        call_count = 0

        async def fake_run_cli(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return (1, "", "Login failed")

        with patch.object(service, "_run_cli", new=AsyncMock(side_effect=fake_run_cli)), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "delete_listing", new=AsyncMock()):
            with pytest.raises(KleinanzeigenError, match="Veröffentlichung fehlgeschlagen"):
                await service._publish_new_ad(ad_dir, new_yaml, "OLD456")

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_all_category_retries_fail_uses_pickup(self, service: "KleinanzeigenService") -> None:
        """If all category retries fail, falls back to PICKUP and sets warning."""
        ad_dir, new_yaml = self._make_ad_dir_with_shipping(service)

        call_count = 0
        final_yaml_data: dict = {}

        async def fake_run_cli(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 4:
                return (1, "", "Andere Versandmethoden not found")
            # PICKUP fallback succeeds (call 5)
            with open(new_yaml) as f:
                final_yaml_data.update(yaml.safe_load(f) or {})
            return (0, "Ad published: https://www.kleinanzeigen.de/s-anzeige/schuhe/999000111", "")

        mock_delete = AsyncMock(return_value=True)
        mock_suggest = AsyncMock(return_value=["Mode & Beauty > Schuhe", "Mode & Beauty > Damen", "Mode & Beauty > Sonstiges"])
        with patch.object(service, "_run_cli", new=AsyncMock(side_effect=fake_run_cli)), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "delete_listing", new=mock_delete), \
             patch.object(service, "_suggest_alternative_categories", new=mock_suggest):
            result, warning = await service._publish_new_ad(ad_dir, new_yaml, "OLD789")

        assert "999000111" in result
        assert warning is not None
        assert "Abholung" in warning
        assert "210/217/herren" in warning
        # 1 initial + 3 category retries + 1 PICKUP fallback
        assert call_count == 5
        mock_delete.assert_awaited_once_with("OLD789")
        assert final_yaml_data.get("shipping_type") == "PICKUP"
        assert final_yaml_data.get("sell_directly") is False
        assert "shipping_options" not in final_yaml_data

    @pytest.mark.asyncio
    async def test_all_retries_and_pickup_fail_raises_error(self, service: "KleinanzeigenService") -> None:
        """If everything including PICKUP fallback fails, raises KleinanzeigenError."""
        from services.kleinanzeigen import KleinanzeigenError
        ad_dir, new_yaml = self._make_ad_dir_with_shipping(service)

        async def fake_run_cli(*args, **kwargs):
            return (1, "", "Andere Versandmethoden not found")

        mock_suggest = AsyncMock(return_value=["Mode & Beauty > Schuhe"])
        with patch.object(service, "_run_cli", new=AsyncMock(side_effect=fake_run_cli)), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "delete_listing", new=AsyncMock()), \
             patch.object(service, "_suggest_alternative_categories", new=mock_suggest):
            with pytest.raises(KleinanzeigenError):
                await service._publish_new_ad(ad_dir, new_yaml, "OLD999")

        AsyncMock().assert_not_awaited()


class TestPublishNewAdStalePage:
    """Tests for _publish_new_ad stale-browser-page retry logic."""

    def _make_ad_dir(self, service: "KleinanzeigenService") -> tuple[Path, Path]:
        ad_dir = service.ads_dir / "ad_staletest"
        ad_dir.mkdir(parents=True, exist_ok=True)
        ad_data = {"active": True, "title": "VW T5 Sitz", "description": "Gut", "price": 50}
        new_yaml = ad_dir / "ad.yaml"
        with open(new_yaml, "w") as f:
            yaml.dump(ad_data, f)
        return ad_dir, new_yaml

    _STALE_ERROR = "No HTML element found with ID 'pstad-descrptn' within 11.25 seconds."

    @pytest.mark.asyncio
    async def test_stale_page_retry_succeeds(self, service: "KleinanzeigenService") -> None:
        """Stale page error triggers retry (without profile clear) which succeeds."""
        ad_dir, new_yaml = self._make_ad_dir(service)
        call_count = 0

        async def fake_run_cli(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (1, "", f"published 0 ads (1 failed after retries)\n{self._STALE_ERROR}")
            return (0, "Ad published: https://www.kleinanzeigen.de/s-anzeige/test/123456", "")

        with patch.object(service, "_run_cli", new=AsyncMock(side_effect=fake_run_cli)), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "delete_listing", new=AsyncMock(return_value=True)):
            result, warning = await service._publish_new_ad(ad_dir, new_yaml, "3324369914")

        assert "123456" in result
        assert warning is None
        assert call_count == 2


    @pytest.mark.asyncio
    async def test_stale_page_retry_also_fails_raises_error(self, service: "KleinanzeigenService") -> None:
        """Stale page error triggers one retry; if retry also fails, raises KleinanzeigenError."""
        from services.kleinanzeigen import KleinanzeigenError
        ad_dir, new_yaml = self._make_ad_dir(service)
        call_count = 0

        async def fake_run_cli(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return (1, "", f"published 0 ads (1 failed after retries)\n{self._STALE_ERROR}")

        with patch.object(service, "_run_cli", new=AsyncMock(side_effect=fake_run_cli)), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()):
            with pytest.raises(KleinanzeigenError, match="Veröffentlichung fehlgeschlagen"):
                await service._publish_new_ad(ad_dir, new_yaml, "STALE2")

        assert call_count == 2  # initial + one retry, not more

    @pytest.mark.asyncio
    async def test_non_stale_failure_not_retried(self, service: "KleinanzeigenService") -> None:
        """A non-stale failure (e.g. login error) does NOT trigger the stale-page retry."""
        from services.kleinanzeigen import KleinanzeigenError
        ad_dir, new_yaml = self._make_ad_dir(service)
        call_count = 0

        async def fake_run_cli(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return (1, "", "Login failed: credentials invalid")

        mock_clear_profile = MagicMock()
        with patch.object(service, "_run_cli", new=AsyncMock(side_effect=fake_run_cli)), \
             patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "_clear_browser_profile", new=mock_clear_profile):
            with pytest.raises(KleinanzeigenError):
                await service._publish_new_ad(ad_dir, new_yaml, "NOSTALE")

        assert call_count == 1
        mock_clear_profile.assert_not_called()


class TestDraftModel:
    """Tests for the Draft model."""

    def test_draft_validates_title_max_length(self) -> None:
        """Test that title is validated for max length."""
        # Pydantic should allow title up to 50 chars
        draft = Draft(
            chat_id=123,
            title="A" * 50,
            description="Valid description",
            price=25.0,
        )
        assert len(draft.title) == 50

    def test_draft_rejects_negative_price(self) -> None:
        """Test that negative price is rejected."""
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            Draft(
                chat_id=123,
                title="Test Title Here",
                description="Valid description",
                price=-5.0,
            )

    def test_draft_format_for_telegram(self) -> None:
        """Test the Telegram message format."""
        draft = Draft(
            chat_id=123,
            title="IKEA Kallax Regal",
            description="Gut erhalten",
            price=45.0,
        )
        formatted = draft.format_for_telegram()
        assert "IKEA Kallax Regal" in formatted
        assert "45.00 EUR" in formatted
        assert "Gut erhalten" in formatted


class TestCoerceApiPrice:
    """`price` from m-meine-anzeigen-verwalten.json is not always a number."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, 0.0),
            (True, 0.0),
            (15, 15.0),
            (15.5, 15.5),
            ("15 €", 15.0),
            ("15,00 €", 15.0),
            ("1.250 € VB", 1250.0),
            ("VB", 0.0),
            ("Zu verschenken", 0.0),
            ("", 0.0),
            ({"amount": "25", "currencyCode": "EUR"}, 25.0),
            ({"priceInEuroCent": 2599}, 25.99),
            ({}, 0.0),
        ],
    )
    def test_coerce(self, raw, expected):
        from services.kleinanzeigen import _coerce_api_price

        assert _coerce_api_price(raw) == expected


class TestPublishedAdsFetchResilience:
    """The in-page fetch() of the published-ads API fails sporadically."""

    @staticmethod
    def _bot() -> MagicMock:
        bot = MagicMock()
        bot.root_url = "https://www.kleinanzeigen.de"
        bot.web_open = AsyncMock()
        bot.web_execute = AsyncMock()
        return bot

    @pytest.mark.asyncio
    async def test_retries_transient_fetch_failure(
        self, service: "KleinanzeigenService"
    ) -> None:
        ads = [{"id": 1, "state": "active"}]
        fetch = AsyncMock(side_effect=[RuntimeError("TypeError: Failed to fetch"), ads])
        bot = self._bot()

        with patch("kleinanzeigen_bot.published_ads.fetch_published_ads", new=fetch):
            with patch("services.kleinanzeigen.PUBLISHED_ADS_RETRY_DELAY", 0):
                result = await service._fetch_published_ads_resilient(bot)

        assert result == ads
        assert fetch.await_count == 2
        bot.web_open.assert_awaited_once_with(bot.root_url)

    @pytest.mark.asyncio
    async def test_falls_back_to_navigation(self, service: "KleinanzeigenService") -> None:
        import json

        fetch = AsyncMock(side_effect=RuntimeError("TypeError: Failed to fetch"))
        bot = self._bot()
        bot.web_execute.return_value = json.dumps(
            {"ads": [{"id": 7, "state": "active", "title": "X"}], "paging": {"pageNum": 1, "last": 1}}
        )

        with patch("kleinanzeigen_bot.published_ads.fetch_published_ads", new=fetch):
            with patch("services.kleinanzeigen.PUBLISHED_ADS_RETRY_DELAY", 0):
                result = await service._fetch_published_ads_resilient(bot)

        assert [ad["id"] for ad in result] == [7]

    @pytest.mark.asyncio
    async def test_navigation_fallback_paginates(
        self, service: "KleinanzeigenService"
    ) -> None:
        import json

        bot = self._bot()
        bot.web_execute.side_effect = [
            json.dumps({"ads": [{"id": 1, "state": "active"}], "paging": {"pageNum": 1, "next": 2, "last": 2}}),
            json.dumps({"ads": [{"id": 2, "state": "active"}], "paging": {"pageNum": 2, "last": 2}}),
        ]

        result = await service._fetch_published_ads_via_navigation(bot)

        assert [ad["id"] for ad in result] == [1, 2]
        assert bot.web_open.await_count == 2

    @pytest.mark.asyncio
    async def test_navigation_fallback_ignores_non_json(
        self, service: "KleinanzeigenService"
    ) -> None:
        bot = self._bot()
        bot.web_execute.return_value = "<html>Anmelden</html>"

        assert await service._fetch_published_ads_via_navigation(bot) == []

    @pytest.mark.asyncio
    async def test_raises_when_fallback_also_fails(
        self, service: "KleinanzeigenService"
    ) -> None:
        from services.kleinanzeigen import KleinanzeigenError

        fetch = AsyncMock(side_effect=RuntimeError("TypeError: Failed to fetch"))
        bot = self._bot()
        bot.web_open.side_effect = [None, None, RuntimeError("navigation dead")]

        with patch("kleinanzeigen_bot.published_ads.fetch_published_ads", new=fetch):
            with patch("services.kleinanzeigen.PUBLISHED_ADS_RETRY_DELAY", 0):
                with pytest.raises(KleinanzeigenError):
                    await service._fetch_published_ads_resilient(bot)


class TestExtractBotError:
    """User-visible publish errors must name the real cause, not setup noise."""

    NODRIVER_WARNING = (
        "[WARNING] nodriver CDP re-attach patch not found: installed nodriver may miss "
        "the flat-mode fix. Symptom: repeated `Re-attaching CDP session after -32601`."
    )

    def test_error_line_wins_over_earlier_warning(self, service: "KleinanzeigenService") -> None:
        output = (
            f"{self.NODRIVER_WARNING}\n"
            "[DEBUG] Re-attaching CDP session after -32601\n"
            "[ERROR] All 3 attempts failed for [Foo]: No HTML element found using XPath '//a'.\n"
        )
        assert service._extract_bot_error(output).startswith("[ERROR] All 3 attempts failed")

    def test_noise_warning_is_ignored(self, service: "KleinanzeigenService") -> None:
        assert service._extract_bot_error(self.NODRIVER_WARNING) == "Unbekannter Fehler (Details: /logs)"

    def test_real_warning_is_kept(self, service: "KleinanzeigenService") -> None:
        output = f"{self.NODRIVER_WARNING}\n[WARNING] Attempt 1/3 failed for 'Foo': boom\n"
        assert service._extract_bot_error(output) == "[WARNING] Attempt 1/3 failed for 'Foo': boom"

    def test_last_error_wins(self, service: "KleinanzeigenService") -> None:
        output = "[ERROR] first\n[ERROR] second\n"
        assert service._extract_bot_error(output) == "[ERROR] second"

    def test_validation_details_are_appended(self, service: "KleinanzeigenService") -> None:
        output = (
            "2026-09-01 04:18:55,362 [ERROR] 1 validation error for [Ad]:\n"
            "- : Value error, sell_directly requires shipping_type to be SHIPPING\n"
            "\n"
            " _    _      _\n"
        )
        result = service._extract_bot_error(output)
        assert result.endswith("sell_directly requires shipping_type to be SHIPPING")
        assert "_    _" not in result


class TestDeleteListing:
    """delete_listing must not trip the bot's ad validation."""

    @pytest.mark.asyncio
    async def test_temp_yaml_overrides_conflicting_ad_defaults(
        self, service: "KleinanzeigenService"
    ) -> None:
        """config.yaml ad_defaults may pair PICKUP with sell_directly: true, which is invalid."""
        written: dict = {}

        async def fake_run_cli(*args, **kwargs):
            temp_yaml = service.ads_dir / "_del_12345" / "ad.yaml"
            with open(temp_yaml, encoding="utf-8") as f:
                written.update(yaml.safe_load(f))
            return 0, "", ""

        with patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "_run_cli", new=fake_run_cli):
            assert await service.delete_listing("12345") is True

        assert written["shipping_type"] == "PICKUP"
        assert written["sell_directly"] is False

    @pytest.mark.asyncio
    async def test_failure_message_uses_extracted_error(
        self, service: "KleinanzeigenService"
    ) -> None:
        from services.kleinanzeigen import KleinanzeigenError

        stderr = (
            "2026-09-01 04:18:55,362 [ERROR] 1 validation error for [Ad]:\n"
            "- : Value error, sell_directly requires shipping_type to be SHIPPING\n"
        )
        with patch.object(service, "_pre_session_setup", new=AsyncMock()), \
             patch.object(service, "_run_cli", new=AsyncMock(return_value=(1, "banner", stderr))):
            with pytest.raises(KleinanzeigenError) as exc:
                await service.delete_listing("12345")

        assert "sell_directly requires shipping_type to be SHIPPING" in str(exc.value)
        assert "banner" not in str(exc.value)
