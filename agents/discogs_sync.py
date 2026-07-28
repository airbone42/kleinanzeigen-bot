"""LangGraph workflow for syncing Discogs inventory treasures to Kleinanzeigen."""
import logging
from typing import Any

from langgraph.graph import END, StateGraph

from models.state import DiscogsSyncState
from services.discogs_sync import (
    DiscogsSyncService,
    format_discogs_sync_summary,
    inventory_item_from_dict,
    inventory_item_to_dict,
)

logger = logging.getLogger(__name__)


async def fetch_inventory(state: DiscogsSyncState) -> dict[str, object]:
    """Load Discogs inventory into graph state."""
    service = DiscogsSyncService()
    items = await service.get_inventory_items()
    return {
        "inventory_items": [inventory_item_to_dict(item) for item in items],
    }


async def filter_treasures(state: DiscogsSyncState) -> dict[str, object]:
    """Filter inventory by treasure criteria."""
    items_raw = state.get("inventory_items", [])
    service = DiscogsSyncService()
    items = [inventory_item_from_dict(row) for row in items_raw]
    treasures = service.filter_treasures(items)
    return {
        "treasure_items": [inventory_item_to_dict(item) for item in treasures],
    }


async def sync_kleinanzeigen(state: DiscogsSyncState) -> dict[str, object]:
    """Publish missing treasures and delete removed Discogs listings."""
    service = DiscogsSyncService()
    inventory = [inventory_item_from_dict(row) for row in state.get("inventory_items", [])]

    sync_result = await service.sync(chat_id=state["chat_id"], inventory_items=inventory)

    result_payload = {
        "inventory_count": len(inventory),
        "treasure_count": len(state.get("treasure_items", [])),
        "published": sync_result.get("published", []),
        "deleted": sync_result.get("deleted", []),
        "updated_mappings": int(sync_result.get("updated_mappings", 0)),
        "errors": sync_result.get("errors", []),
    }

    return {
        "published": result_payload["published"],
        "deleted": result_payload["deleted"],
        "updated_mappings": result_payload["updated_mappings"],
        "errors": result_payload["errors"],
        "summary": format_discogs_sync_summary(result_payload),
    }


def build_discogs_sync_graph() -> Any:
    """Build the Discogs sync graph."""
    graph = StateGraph(DiscogsSyncState)
    graph.add_node("fetch_inventory", fetch_inventory)
    graph.add_node("filter_treasures", filter_treasures)
    graph.add_node("sync_kleinanzeigen", sync_kleinanzeigen)

    graph.set_entry_point("fetch_inventory")
    graph.add_edge("fetch_inventory", "filter_treasures")
    graph.add_edge("filter_treasures", "sync_kleinanzeigen")
    graph.add_edge("sync_kleinanzeigen", END)

    return graph.compile()


_discogs_sync_graph: Any = None


def get_discogs_sync_graph() -> Any:
    """Get or create singleton Discogs sync graph."""
    global _discogs_sync_graph
    if _discogs_sync_graph is None:
        _discogs_sync_graph = build_discogs_sync_graph()
        logger.info("Discogs sync graph initialized")
    return _discogs_sync_graph


discogs_sync_graph = build_discogs_sync_graph()
