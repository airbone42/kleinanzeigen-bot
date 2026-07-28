"""Pydantic model for listing drafts."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Draft(BaseModel):
    """Represents a listing draft before submission to Kleinanzeigen."""

    id: Optional[int] = None
    chat_id: int
    title: str = Field(..., max_length=50, description="Listing title, max 50 chars")
    description: str = Field(..., min_length=10, description="Listing description")
    price: float = Field(..., ge=0, description="Price in EUR")
    category: str = Field(default="", description="Kleinanzeigen category")
    shipping_type: str = Field(default="PICKUP", description="SHIPPING or PICKUP")
    shipping_size: str = Field(default="PICKUP", description="PICKUP, S, M, or L")
    special_attributes: dict[str, str] = Field(
        default_factory=dict,
        description="Kleinanzeigen special_attributes, e.g. condition_s",
    )
    image_analysis: str = Field(default="", description="AI image analysis result")
    image_file_ids: list[str] = Field(default_factory=list, description="Telegram file IDs")
    status: str = Field(default="draft", description="Draft status")
    listing_id: Optional[str] = None
    listing_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Title cannot be empty")
        return v

    @field_validator("price")
    @classmethod
    def price_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Price cannot be negative")
        return round(v, 2)

    _SHIPPING_LABELS: dict[str, str] = {
        "S": "📦 Paket S (<2 kg, DHL/Hermes)",
        "M": "📦 Paket M (2–10 kg, DHL/Hermes)",
        "L": "📦 Paket L (bis 31 kg, DHL/Hermes)",
        "PICKUP": "🚗 Nur Abholung",
    }

    def format_for_telegram(self) -> str:
        """Format the draft as a Telegram message."""
        shipping_label = self._SHIPPING_LABELS.get(self.shipping_size, self.shipping_size)
        return (
            f"📝 *Entwurf erstellt:*\n\n"
            f"*Titel:* {self.title}\n"
            f"*Preis:* {self.price:.2f} EUR\n"
            f"*Versand:* {shipping_label}\n\n"
            f"*Beschreibung:*\n{self.description}"
        )
