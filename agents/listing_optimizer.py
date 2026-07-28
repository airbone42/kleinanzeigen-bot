"""LangGraph workflow for optimizing existing listings."""
import logging
from typing import Any, Literal

from langgraph.graph import StateGraph, END

from models.state import ListingOptimizerState
from agents.nodes.optimization import optimize_listing

logger = logging.getLogger(__name__)


def route_by_price(state: ListingOptimizerState) -> Literal["optimize", "suggest_delete"]:
    """Route based on current listing price.

    Listings under 5 EUR get a deletion suggestion,
    others get optimized.
    """
    if state.get("current_price", 0) < 5.0:
        return "suggest_delete"
    return "optimize"


async def suggest_deletion(state: ListingOptimizerState) -> dict[str, Any]:
    """Prepare deletion suggestion for low-price listings."""
    logger.info(f"Suggesting deletion for listing {state.get('listing_id')} (price < 5 EUR)")
    return {
        "action": "delete",
        "changes_summary": f"Preis {state.get('current_price', 0):.2f} EUR – Löschung empfohlen",
    }


async def mark_for_optimization(state: ListingOptimizerState) -> dict[str, Any]:
    """Mark listing for optimization."""
    return {"action": "optimize"}


def build_listing_optimizer_graph() -> Any:
    """Build the LangGraph state graph for listing optimization.

    Returns:
        Compiled LangGraph graph
    """
    graph = StateGraph(ListingOptimizerState)

    # Add nodes
    graph.add_node("check_price", mark_for_optimization)
    graph.add_node("optimize_listing", optimize_listing)
    graph.add_node("suggest_delete", suggest_deletion)

    # Entry: check price first
    graph.set_entry_point("check_price")

    # Route based on price
    graph.add_conditional_edges(
        "check_price",
        route_by_price,
        {
            "optimize": "optimize_listing",
            "suggest_delete": "suggest_delete",
        },
    )

    graph.add_edge("optimize_listing", END)
    graph.add_edge("suggest_delete", END)

    return graph.compile()


_optimizer_graph: Any = None


def get_listing_optimizer() -> Any:
    """Get or create the listing optimizer graph."""
    global _optimizer_graph
    if _optimizer_graph is None:
        _optimizer_graph = build_listing_optimizer_graph()
        logger.info("Listing optimizer graph initialized")
    return _optimizer_graph


# Module-level export for LangGraph Platform (referenced in langgraph.json)
listing_optimizer_graph = build_listing_optimizer_graph()
