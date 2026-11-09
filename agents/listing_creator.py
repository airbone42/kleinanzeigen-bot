"""LangGraph workflow for creating new listings from photos."""
import logging
from typing import Any

from langgraph.graph import StateGraph, END

from models.state import ListingCreatorState
from agents.nodes.image_analysis import analyze_images
from agents.nodes.text_generation import generate_title, generate_description, generate_category
from agents.nodes.price_lookup import estimate_price
from agents.nodes.listing_finalize import finalize_listing

logger = logging.getLogger(__name__)


def _route_after_analysis(state: ListingCreatorState) -> list[str]:
    """Stop the workflow when image analysis needs more input from the seller."""
    if state.get("needs_info"):
        return [END]
    return ["generate_title", "generate_category", "estimate_price"]


def build_listing_creator_graph() -> Any:
    """Build the LangGraph state graph for listing creation.

    Returns:
        Compiled LangGraph graph
    """
    graph = StateGraph(ListingCreatorState)

    # Add nodes
    graph.add_node("analyze_images", analyze_images)
    graph.add_node("generate_title", generate_title)
    graph.add_node("generate_description", generate_description)
    graph.add_node("generate_category", generate_category)
    graph.add_node("estimate_price", estimate_price)
    graph.add_node("finalize_listing", finalize_listing)

    # Fan-out from image analysis: title/category/price in parallel.
    # Description runs after title so naming/wording can stay consistent with it.
    graph.set_entry_point("analyze_images")
    # If the photos are not enough to identify the item, stop and ask instead of guessing.
    graph.add_conditional_edges("analyze_images", _route_after_analysis)
    graph.add_edge("generate_title", "generate_description")
    # Fan-in barrier: finalize only after all upstream nodes finished.
    graph.add_edge(
        ["generate_description", "generate_category", "estimate_price"],
        "finalize_listing",
    )
    graph.add_edge("finalize_listing", END)

    return graph.compile()


# Singleton graph instances
_listing_creator_graph: Any = None


def get_listing_creator() -> Any:
    """Get or create the listing creator graph."""
    global _listing_creator_graph
    if _listing_creator_graph is None:
        _listing_creator_graph = build_listing_creator_graph()
        logger.info("Listing creator graph initialized")
    return _listing_creator_graph


# Module-level exports for LangGraph Platform (referenced in langgraph.json)
listing_creator_graph = build_listing_creator_graph()
