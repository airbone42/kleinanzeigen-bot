"""Unit tests for the listing creator agent."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from models.state import ListingCreatorState


@pytest.fixture
def sample_state() -> ListingCreatorState:
    return {
        "images": [b"fake_image_bytes"],
        "image_file_ids": ["file123"],
        "image_analysis": "IKEA Kallax Regal, weiss, 4x2, gut erhalten",
        "title": "",
        "description": "",
        "price": 0.0,
        "category": "",
        "feedback": "",
        "draft_id": None,
        "status": "draft",
        "messages": [],
    }


class TestImageAnalysis:
    """Tests for the image analysis node."""

    @pytest.mark.asyncio
    async def test_analyze_images_no_images(self) -> None:
        """Test with no images returns default message."""
        from agents.nodes.image_analysis import analyze_images

        state: ListingCreatorState = {
            "images": [],
            "image_file_ids": [],
            "image_analysis": "",
            "title": "",
            "description": "",
            "price": 0.0,
            "category": "",
            "feedback": "",
            "draft_id": None,
            "status": "draft",
            "messages": [],
        }

        result = await analyze_images(state)
        assert "image_analysis" in result
        assert result["image_analysis"] == "Keine Bilder vorhanden."

    @pytest.mark.asyncio
    async def test_analyze_images_with_mock_llm(self) -> None:
        """Test image analysis with mocked LLM response."""
        from agents.nodes.image_analysis import analyze_images

        mock_response = MagicMock()
        mock_response.content = "IKEA Kallax Regal, weiss, sehr gut erhalten"

        with patch("agents.nodes.image_analysis.get_vision_llm") as mock_get_llm:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.return_value = mock_response
            mock_get_llm.return_value = mock_llm

            state: ListingCreatorState = {
                "images": [b"fake_bytes"],
                "image_file_ids": ["file123"],
                "image_analysis": "",
                "title": "",
                "description": "",
                "price": 0.0,
                "category": "",
                "feedback": "",
                "draft_id": None,
                "status": "draft",
                "messages": [],
            }

            result = await analyze_images(state)
            assert "image_analysis" in result
            assert "IKEA" in result["image_analysis"]


class TestTitleGeneration:
    """Tests for the title generation node."""

    @pytest.mark.asyncio
    async def test_generate_title_empty_analysis(self) -> None:
        """Test with empty analysis returns default title."""
        from agents.nodes.text_generation import generate_title

        state: ListingCreatorState = {
            "images": [],
            "image_file_ids": [],
            "image_analysis": "",
            "title": "",
            "description": "",
            "price": 0.0,
            "category": "",
            "feedback": "",
            "draft_id": None,
            "status": "draft",
            "messages": [],
        }

        result = await generate_title(state)
        assert "title" in result
        assert len(result["title"]) <= 50

    @pytest.mark.asyncio
    async def test_generate_title_truncates_long_titles(self) -> None:
        """Test that generated titles are truncated to 50 chars."""
        from agents.nodes.text_generation import generate_title

        mock_response = MagicMock()
        mock_response.content = "A" * 60  # 60 chars, should be truncated

        with patch("agents.nodes.text_generation.get_text_llm") as mock_get_llm:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.return_value = mock_response
            mock_get_llm.return_value = mock_llm

            state: ListingCreatorState = {
                "images": [],
                "image_file_ids": [],
                "image_analysis": "Test item",
                "title": "",
                "description": "",
                "price": 0.0,
                "category": "",
                "feedback": "",
                "draft_id": None,
                "status": "draft",
                "messages": [],
            }

            result = await generate_title(state)
            assert len(result["title"]) <= 50

    @pytest.mark.asyncio
    async def test_generate_title_includes_required_search_tokens(self) -> None:
        """Model number and size tokens from input must appear in title."""
        from agents.nodes.text_generation import generate_title

        mock_response = MagicMock()
        mock_response.content = "Dachträger Thule"

        with patch("agents.nodes.text_generation.get_text_llm") as mock_get_llm:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.return_value = mock_response
            mock_get_llm.return_value = mock_llm

            state: ListingCreatorState = {
                "images": [],
                "image_file_ids": [],
                "image_analysis": "Dachträger Modellnummer 711100, Breite 108",
                "title": "",
                "description": "",
                "price": 0.0,
                "category": "",
                "feedback": "",
                "draft_id": None,
                "status": "draft",
                "caption": "Passend, Größe 108, Nummer 711100",
                "shipping_type": "PICKUP",
                "shipping_size": "PICKUP",
                "messages": [],
            }

            result = await generate_title(state)
            out = result["title"]
            assert "108" in out
            assert "711100" in out

    @pytest.mark.asyncio
    async def test_generate_title_includes_exact_model_phrase(self) -> None:
        from agents.nodes.text_generation import generate_title

        mock_response = MagicMock()
        mock_response.content = "Tri Suit Herren"

        with patch("agents.nodes.text_generation.get_text_llm") as mock_get_llm:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.return_value = mock_response
            mock_get_llm.return_value = mock_llm

            state: ListingCreatorState = {
                "images": [],
                "image_file_ids": [],
                "image_analysis": 'Modell "Streamliner Bullet Tri Suit" in Premium-Ausführung',
                "title": "",
                "description": "",
                "price": 0.0,
                "category": "",
                "feedback": "",
                "draft_id": None,
                "status": "draft",
                "caption": "",
                "shipping_type": "PICKUP",
                "shipping_size": "PICKUP",
                "messages": [],
            }

            result = await generate_title(state)
            assert "Streamliner Bullet Tri Suit" in result["title"]


class TestPriceEstimation:
    """Tests for the price estimation node."""

    @pytest.mark.asyncio
    async def test_estimate_price_parses_response(self) -> None:
        """Test that price is correctly parsed from LLM response."""
        from agents.nodes.price_lookup import estimate_price

        mock_response = MagicMock()
        mock_response.content = "45"

        with patch("agents.nodes.price_lookup.get_llm") as mock_get_llm, patch(
            "agents.nodes.price_lookup._estimate_market_price_from_web",
            new=AsyncMock(return_value=None),
        ):
            mock_llm = AsyncMock()
            mock_llm.ainvoke.return_value = mock_response
            mock_get_llm.return_value = mock_llm

            state: ListingCreatorState = {
                "images": [],
                "image_file_ids": [],
                "image_analysis": "IKEA Kallax Regal",
                "title": "IKEA Kallax Regal weiss",
                "description": "",
                "price": 0.0,
                "category": "",
                "feedback": "",
                "draft_id": None,
                "status": "draft",
                "messages": [],
            }

            result = await estimate_price(state)
            assert "price" in result
            assert result["price"] == 45.0

    @pytest.mark.asyncio
    async def test_estimate_price_fallback_on_failure(self) -> None:
        """Test that price falls back to 10.0 on LLM failure."""
        from agents.nodes.price_lookup import estimate_price

        with patch("agents.nodes.price_lookup.get_llm") as mock_get_llm, patch(
            "agents.nodes.price_lookup._estimate_market_price_from_web",
            new=AsyncMock(return_value=None),
        ):
            mock_llm = AsyncMock()
            mock_llm.ainvoke.side_effect = Exception("API error")
            mock_get_llm.return_value = mock_llm

            state: ListingCreatorState = {
                "images": [],
                "image_file_ids": [],
                "image_analysis": "Test",
                "title": "Test item",
                "description": "",
                "price": 0.0,
                "category": "",
                "feedback": "",
                "draft_id": None,
                "status": "draft",
                "messages": [],
            }

            result = await estimate_price(state)
            assert result["price"] == 10.0

    @pytest.mark.asyncio
    async def test_estimate_price_uses_market_floor_for_premium_models(self) -> None:
        from agents.nodes.price_lookup import estimate_price

        with patch(
            "agents.nodes.price_lookup._estimate_price_llm",
            new=AsyncMock(return_value=75.0),
        ), patch(
            "agents.nodes.price_lookup._estimate_market_price_from_web",
            new=AsyncMock(return_value=150.0),
        ), patch(
            "agents.nodes.price_lookup._determine_shipping_llm",
            new=AsyncMock(return_value="PICKUP"),
        ):
            state: ListingCreatorState = {
                "images": [],
                "image_file_ids": [],
                "image_analysis": "Premium Streamliner Bullet Tri Suit",
                "title": "Streamliner Bullet Tri Suit",
                "description": "",
                "price": 0.0,
                "category": "",
                "feedback": "",
                "draft_id": None,
                "status": "draft",
                "messages": [],
            }
            result = await estimate_price(state)
            assert result["price"] >= 142.5


class TestDraftReview:
    """Tests for the draft review node."""

    @pytest.mark.asyncio
    async def test_review_fixes_empty_title(self) -> None:
        """Test that review fixes an empty title."""
        from agents.nodes.listing_finalize import finalize_listing

        state: ListingCreatorState = {
            "images": [],
            "image_file_ids": [],
            "image_analysis": "",
            "title": "",
            "description": "Valid description here",
            "price": 25.0,
            "category": "",
            "feedback": "",
            "draft_id": None,
            "status": "draft",
            "messages": [],
        }

        result = await finalize_listing(state)
        assert result["title"] != ""
        assert len(result["title"]) <= 50

    @pytest.mark.asyncio
    async def test_review_fixes_negative_price(self) -> None:
        """Test that review fixes a negative or zero price."""
        from agents.nodes.listing_finalize import finalize_listing

        state: ListingCreatorState = {
            "images": [],
            "image_file_ids": [],
            "image_analysis": "",
            "title": "Valid title here",
            "description": "Valid description",
            "price": -5.0,
            "category": "",
            "feedback": "",
            "draft_id": None,
            "status": "draft",
            "messages": [],
        }

        result = await finalize_listing(state)
        assert result["price"] >= 1.0

    @pytest.mark.asyncio
    async def test_review_truncates_long_title(self) -> None:
        """Test that review truncates title to 50 chars."""
        from agents.nodes.listing_finalize import finalize_listing

        state: ListingCreatorState = {
            "images": [],
            "image_file_ids": [],
            "image_analysis": "",
            "title": "X" * 100,
            "description": "Valid description",
            "price": 25.0,
            "category": "",
            "feedback": "",
            "draft_id": None,
            "status": "draft",
            "messages": [],
        }

        result = await finalize_listing(state)
        assert len(result["title"]) <= 50

    @pytest.mark.asyncio
    async def test_review_removes_defect_terms_without_caption_hint(self) -> None:
        """Defect terms must be removed unless caption explicitly says so."""
        from agents.nodes.listing_finalize import finalize_listing

        state: ListingCreatorState = {
            "images": [],
            "image_file_ids": [],
            "image_analysis": "Artikel wirkt defekt",
            "title": "Defekter Router",
            "description": "Der Artikel ist defekt und nicht funktionsfähig.",
            "price": 20.0,
            "category": "",
            "feedback": "",
            "draft_id": None,
            "status": "draft",
            "caption": "",
            "shipping_type": "PICKUP",
            "shipping_size": "PICKUP",
            "messages": [],
        }

        result = await finalize_listing(state)
        out = f"{result['title']} {result['description']}".lower()
        assert "defekt" not in out
        assert "kaputt" not in out
        assert "nicht funktionsfähig" not in out

    @pytest.mark.asyncio
    async def test_review_keeps_defect_terms_with_caption_hint(self) -> None:
        """Defect terms may remain when explicitly provided by seller caption."""
        from agents.nodes.listing_finalize import finalize_listing

        state: ListingCreatorState = {
            "images": [],
            "image_file_ids": [],
            "image_analysis": "Artikel wirkt defekt",
            "title": "Defekter Router",
            "description": "Der Artikel ist defekt und nicht funktionsfähig.",
            "price": 20.0,
            "category": "",
            "feedback": "",
            "draft_id": None,
            "status": "draft",
            "caption": "Hinweis: Das Gerät ist defekt.",
            "shipping_type": "PICKUP",
            "shipping_size": "PICKUP",
            "messages": [],
        }

        result = await finalize_listing(state)
        out = f"{result['title']} {result['description']}".lower()
        assert "defekt" in out

    @pytest.mark.asyncio
    async def test_review_removes_uncertainty_terms(self) -> None:
        """Unsichere Formulierungen wie 'scheint' sollen entfernt werden."""
        from agents.nodes.listing_finalize import finalize_listing

        state: ListingCreatorState = {
            "images": [],
            "image_file_ids": [],
            "image_analysis": "",
            "title": "IKEA Regal",
            "description": "Der Schrank scheint neuwertig und vermutlich kaum genutzt.",
            "price": 20.0,
            "category": "",
            "feedback": "",
            "draft_id": None,
            "status": "draft",
            "caption": "",
            "shipping_type": "PICKUP",
            "shipping_size": "PICKUP",
            "messages": [],
        }

        result = await finalize_listing(state)
        out = result["description"].lower()
        assert "scheint" not in out
        assert "vermutlich" not in out

    @pytest.mark.asyncio
    async def test_review_removes_image_observer_sentence(self) -> None:
        """Bildbeobachtungs-Saetze sollen nicht in der Anzeige landen."""
        from agents.nodes.listing_finalize import finalize_listing

        state: ListingCreatorState = {
            "images": [],
            "image_file_ids": [],
            "image_analysis": "",
            "title": "Skischuh",
            "description": (
                "Sehr gut erhaltener Skischuh in Größe 38.5. "
                "Eine Gewichtsangabe von 38.5Y ist auf der Verpackung vermerkt."
            ),
            "price": 35.0,
            "category": "",
            "feedback": "",
            "draft_id": None,
            "status": "draft",
            "caption": "",
            "shipping_type": "PICKUP",
            "shipping_size": "PICKUP",
            "messages": [],
        }

        result = await finalize_listing(state)
        out = result["description"].lower()
        assert "auf der verpackung vermerkt" not in out
        assert "sehr gut erhaltener skischuh" in out

    @pytest.mark.asyncio
    async def test_review_removes_seller_observer_phrase(self) -> None:
        from agents.nodes.listing_finalize import finalize_listing

        state: ListingCreatorState = {
            "images": [],
            "image_file_ids": [],
            "image_analysis": "",
            "title": "Tri Suit",
            "description": "Wie vom Verkäufer beschrieben hat das Modell sehr gute Aerodynamik.",
            "price": 120.0,
            "category": "",
            "feedback": "",
            "draft_id": None,
            "status": "draft",
            "caption": "",
            "shipping_type": "PICKUP",
            "shipping_size": "PICKUP",
            "messages": [],
        }
        result = await finalize_listing(state)
        assert "wie vom verkäufer beschrieben" not in result["description"].lower()


class TestGraphConcurrencyRegression:
    """Regression tests for concurrent state writes in listing creator graph."""

    @pytest.mark.asyncio
    async def test_finalize_waits_for_all_parallel_nodes(self) -> None:
        """finalize_listing must run only after description/category/price are done."""
        from agents import listing_creator as creator_module

        async def analyze_images(_: ListingCreatorState) -> dict[str, object]:
            return {"image_analysis": "analysis"}

        async def generate_title(_: ListingCreatorState) -> dict[str, object]:
            await asyncio.sleep(0.01)
            return {"title": "Title"}

        async def generate_description(_: ListingCreatorState) -> dict[str, object]:
            await asyncio.sleep(0.08)
            return {"description": "from_description"}

        async def generate_category(_: ListingCreatorState) -> dict[str, object]:
            return {"category": "Elektronik > Sonstiges"}

        async def estimate_price(_: ListingCreatorState) -> dict[str, object]:
            return {"price": 42.0}

        async def finalize_listing(_: ListingCreatorState) -> dict[str, object]:
            # Writing description here used to conflict with generate_description
            # when finalize started too early.
            return {"description": "from_finalize", "status": "draft"}

        with patch.object(creator_module, "analyze_images", analyze_images), patch.object(
            creator_module, "generate_title", generate_title
        ), patch.object(creator_module, "generate_description", generate_description), patch.object(
            creator_module, "generate_category", generate_category
        ), patch.object(
            creator_module, "estimate_price", estimate_price
        ), patch.object(
            creator_module, "finalize_listing", finalize_listing
        ):
            graph = creator_module.build_listing_creator_graph()
            result = await graph.ainvoke(
                {
                    "images": ["base64"],
                    "image_file_ids": ["file123"],
                    "image_analysis": "",
                    "title": "",
                    "description": "",
                    "price": 0.0,
                    "category": "",
                    "shipping_type": "PICKUP",
                    "shipping_size": "PICKUP",
                    "caption": "",
                    "feedback": "",
                    "draft_id": None,
                    "status": "draft",
                }
            )

        assert result["description"] == "from_finalize"
        assert result["status"] == "draft"
