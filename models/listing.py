"""Pydantic model for active Kleinanzeigen listings."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Listing(BaseModel):
    """Represents an active Kleinanzeigen listing."""

    listing_id: str = Field(..., description="Kleinanzeigen listing ID")
    chat_id: int = Field(default=0, description="Associated Telegram chat ID")
    title: str = Field(..., description="Listing title")
    description: str = Field(default="", description="Listing description")
    price: float = Field(..., ge=0, description="Price in EUR")
    category: str = Field(default="", description="Kleinanzeigen category")
    status: str = Field(default="active", description="Listing status")
    days_remaining: Optional[int] = Field(
        default=None, description="Days until listing expires"
    )
    url: Optional[str] = Field(default=None, description="URL to the listing")
    shipping_type: str = Field(default="SHIPPING", description="Shipping type (SHIPPING or PICKUP)")
    shipping_costs: Optional[float] = Field(default=None, description="Fixed shipping costs in EUR")
    shipping_options: Optional[str] = Field(default=None, description="JSON-encoded list of shipping options")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def is_expiring_soon(self) -> bool:
        """Check if listing expires within the type-specific threshold (PICKUP: 30 days, SHIPPING: 10 days)."""
        if self.days_remaining is None:
            return False
        from config.settings import settings
        threshold = (
            settings.pickup_expiring_days
            if self.shipping_type == "PICKUP"
            else settings.shipping_expiring_days
        )
        return self.days_remaining < threshold

    @property
    def should_delete(self) -> bool:
        """Check if listing price is too low for optimization (< 5 EUR)."""
        return self.price < 5.0

    def format_summary(self) -> str:
        """Format a short summary for Telegram."""
        days_str = f"noch {self.days_remaining} Tage" if self.days_remaining is not None else "unbekannte Laufzeit"
        return f"📦 *{self.title}* ({days_str})\nPreis: {self.price:.2f} EUR"
