"""LangGraph state definitions for the Kleinanzeigen bot."""
from typing import Literal, Optional

from typing_extensions import TypedDict


class ListingCreatorState(TypedDict):
    """State for the listing creation workflow.

    Images are passed as base64-encoded strings (JSON-serializable for LangGraph Platform).
    """

    images: list[str]  # base64-encoded JPEG strings
    image_file_ids: list[str]  # Telegram file IDs for images
    image_analysis: str  # Result of image analysis
    title: str  # Generated title (max 50 chars)
    description: str  # Generated description
    price: float  # Estimated price in EUR
    category: str  # Kleinanzeigen category
    shipping_type: str  # SHIPPING or PICKUP based on item size
    shipping_size: str  # "PICKUP", "S", "M", or "L"
    caption: str  # Optional caption text from the user when uploading the photo
    feedback: str  # User feedback for revision
    draft_id: Optional[int]  # DB draft ID once saved
    status: Literal["draft", "approved", "published"]


class ListingOptimizerState(TypedDict):
    """State for the listing optimization workflow."""

    listing_id: str  # Kleinanzeigen listing ID
    current_title: str
    current_description: str
    current_price: float
    days_remaining: int
    suggested_title: str
    suggested_description: str
    suggested_price: float
    changes_summary: str
    action: Literal["optimize", "delete", "skip"]
    feedback: str
    status: Literal["pending", "approved", "rejected"]


class DiscogsSyncState(TypedDict):
    """State for Discogs inventory -> Kleinanzeigen sync workflow."""

    chat_id: int
    inventory_items: list[dict[str, object]]
    treasure_items: list[dict[str, object]]
    published: list[dict[str, str]]
    deleted: list[dict[str, str]]
    updated_mappings: int
    summary: str
    errors: list[str]
