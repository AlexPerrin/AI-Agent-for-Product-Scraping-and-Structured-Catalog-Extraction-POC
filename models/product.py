from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class JobStatus(str, enum.Enum):
    pending = "pending"
    tier1_complete = "tier1_complete"
    tier2_complete = "tier2_complete"
    tier2_failed = "tier2_failed"
    skipped_404 = "skipped_404"


class Job(BaseModel):
    """Represents a single scraping job in the queue."""

    id: Optional[int] = None
    url: str
    job_type: str  # "category" or "product"
    status: str = JobStatus.pending.value
    category_slug: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    error_msg: Optional[str] = None


class ProductVariant(BaseModel):
    """One orderable variant row -- the atomic unit of the catalog."""

    # Product name
    product_group_name: str
    product_name: str

    # Brand / manufacturer
    brand: Optional[str] = None

    # SKU / item number / product code
    item_number: str
    manufacturer_number: Optional[str] = None

    # Category hierarchy
    category_hierarchy: list[str] = Field(default_factory=list)

    # Product URL
    product_group_url: str

    # Price -- all quantities are in USD
    price: dict[int, Decimal] = Field(default_factory=dict)

    # Raw description from product table (e.g. "X-small, 200/box")
    description: Optional[str] = None

    # Unit / pack size (normalized form, e.g. "200/box")
    unit_size: Optional[str] = None

    # Availability / stock indicator
    availability: Optional[str] = None

    # Group-level description
    group_description: Optional[str] = None

    # Specifications / attributes
    subgroup_name: Optional[str] = None

    # Image URL(s)
    image_urls: list[str] = Field(default_factory=list)

    # Alternative products
    alternative_products: list[str] = Field(default_factory=list)

    # Pipeline metadata
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    extraction_method: str = "css-selector"
    validation_status: str = "pending"
    validation_notes: Optional[str] = None
