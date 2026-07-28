"""Unit tests for the listing optimizer agent."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from models.state import ListingOptimizerState


@pytest.fixture
def cheap_listing_state() -> ListingOptimizerState:
    """State for a listing with price < 5 EUR."""
    return {
        "listing_id": "12345",
        "current_title": "Alte Bücher",
        "current_description": "3 alte Bücher abzugeben",
        "current_price": 3.0,
        "days_remaining": 2,
        "suggested_title": "",
        "suggested_description": "",
        "suggested_price": 0.0,
        "changes_summary": "",
        "action": "optimize",
        "feedback": "",
        "status": "pending",
        "messages": [],
    }


@pytest.fixture
def valuable_listing_state() -> ListingOptimizerState:
    """State for a listing with price >= 5 EUR."""
    return {
        "listing_id": "67890",
        "current_title": "IKEA Kallax Regal",
        "current_description": "Gut erhaltenes Regal in weiss",
        "current_price": 45.0,
        "days_remaining": 3,
        "suggested_title": "",
        "suggested_description": "",
        "suggested_price": 0.0,
        "changes_summary": "",
        "action": "optimize",
        "feedback": "",
        "status": "pending",
        "messages": [],
    }


class TestListingOptimizer:
    """Tests for the listing optimizer workflow."""

    @pytest.mark.asyncio
    async def test_cheap_listing_suggests_deletion(
        self, cheap_listing_state: ListingOptimizerState
    ) -> None:
        """Test that listings < 5 EUR get deletion suggestion."""
        from agents.listing_optimizer import get_listing_optimizer

        optimizer = get_listing_optimizer()
        result = await optimizer.ainvoke(cheap_listing_state)

        assert result["action"] == "delete"
        assert "changes_summary" in result

    @pytest.mark.asyncio
    async def test_valuable_listing_gets_optimized(
        self, valuable_listing_state: ListingOptimizerState
    ) -> None:
        """Test that listings >= 5 EUR get optimization suggestions."""
        from agents.nodes.optimization import optimize_listing

        mock_response = MagicMock()
        mock_response.content = '''{
            "title": "IKEA Kallax Regal 2x4 weiss",
            "description": "Top erhaltenes IKEA Kallax Regal in weiss, Maße: 147x77cm. Nur Abholung.",
            "price": 39.00,
            "changes_summary": "Preis leicht reduziert, Maße ergänzt"
        }'''

        with patch("agents.nodes.optimization.get_optimization_llm") as mock_get_llm:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.return_value = mock_response
            mock_get_llm.return_value = mock_llm

            result = await optimize_listing(valuable_listing_state)

            assert "suggested_title" in result
            assert "suggested_price" in result
            assert result["suggested_price"] == 39.0
            assert len(result["suggested_title"]) <= 50

    @pytest.mark.asyncio
    async def test_optimization_fallback_on_error(
        self, valuable_listing_state: ListingOptimizerState
    ) -> None:
        """Test that optimization falls back gracefully on LLM error."""
        from agents.nodes.optimization import optimize_listing

        with patch("agents.nodes.optimization.get_optimization_llm") as mock_get_llm:
            mock_llm = AsyncMock()
            mock_llm.ainvoke.side_effect = Exception("API timeout")
            mock_get_llm.return_value = mock_llm

            result = await optimize_listing(valuable_listing_state)

            # Should return current values as fallback
            assert result["suggested_title"] == valuable_listing_state["current_title"]
            assert result["suggested_price"] == valuable_listing_state["current_price"]


class TestRouting:
    """Tests for the optimizer routing logic."""

    def test_route_cheap_listing(self) -> None:
        """Test routing for cheap listing."""
        from agents.listing_optimizer import route_by_price

        state: ListingOptimizerState = {
            "listing_id": "1",
            "current_title": "Test",
            "current_description": "Test",
            "current_price": 3.99,
            "days_remaining": 2,
            "suggested_title": "",
            "suggested_description": "",
            "suggested_price": 0.0,
            "changes_summary": "",
            "action": "optimize",
            "feedback": "",
            "status": "pending",
            "messages": [],
        }

        result = route_by_price(state)
        assert result == "suggest_delete"

    def test_route_valuable_listing(self) -> None:
        """Test routing for valuable listing."""
        from agents.listing_optimizer import route_by_price

        state: ListingOptimizerState = {
            "listing_id": "1",
            "current_title": "Test",
            "current_description": "Test",
            "current_price": 5.0,
            "days_remaining": 2,
            "suggested_title": "",
            "suggested_description": "",
            "suggested_price": 0.0,
            "changes_summary": "",
            "action": "optimize",
            "feedback": "",
            "status": "pending",
            "messages": [],
        }

        result = route_by_price(state)
        assert result == "optimize"
