from __future__ import annotations

import csv
import json
import os
from typing import Any

import aiosqlite
import structlog

from storage.database import get_export_records, init_db

log = structlog.get_logger()


def _price_tiers(price_json: str) -> list[tuple[str, str]]:
    """Return a list of (quantity, price_per_unit) for every price tier, sorted ascending.

    Example: '{"1": "19.49", "6": "17.99"}' -> [('1', '$19.49'), ('6', '$17.99')]
    No-table stubs with key "" return [('', '$X.XX')].
    """
    try:
        d = json.loads(price_json) if isinstance(price_json, str) else price_json
    except (json.JSONDecodeError, TypeError):
        return [("", str(price_json))]
    if not d:
        return [("", "")]
    try:
        tiers = sorted(d.items(), key=lambda x: int(x[0]))
    except (ValueError, TypeError):
        # Non-integer key (no-table stub)
        qty, price = next(iter(d.items()))
        return [("", f"${price}")]
    return [(str(qty), f"${price}") for qty, price in tiers]


def _flatten_list(val: str | list, sep: str = " | ") -> str:
    """Convert a JSON array (or already a list) to a separated string."""
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    if isinstance(val, list):
        return sep.join(str(v) for v in val)
    return str(val)


CSV_COLUMNS = [
    "product_group_name",
    "product_name",
    "brand",
    "item_number",
    "manufacturer_number",
    "category_hierarchy",
    "product_group_url",
    "Quantity",
    "price_per_unit",
    "availability",
    "group_description",
    "unit_size",
    "specifications",
    "image_urls",
    "scraped_at",
    "extraction_method",
    "validation_status",
    "validation_notes",
]


async def export_csv(db_path: str, output_dir: str) -> str:
    """Export valid/warning records to CSV. Returns the output path."""
    db = await init_db(db_path)
    try:
        records = await get_export_records(db)
    finally:
        await db.close()

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "products.csv")

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            row = dict(rec)
            tiers = _price_tiers(row.get("price", "{}"))
            row["category_hierarchy"] = _flatten_list(row.get("category_hierarchy", "[]"), sep=" / ")
            row["image_urls"] = _flatten_list(row.get("image_urls", "[]"))
            # Replace embedded newlines in all string fields so each product
            # occupies exactly one CSV row.
            for key, val in row.items():
                if isinstance(val, str) and "\n" in val:
                    row[key] = " ".join(val.split())
            # Clean availability: strip UI button text like "Notify Me", "Add to Cart"
            avail = row.get("availability") or ""
            for noise in ("Notify Me", "Add to Cart", "Email Me"):
                avail = avail.replace(noise, "").strip(" ,|")
            row["availability"] = avail or None
            # Emit one row per quantity/price tier
            for qty, price_per_unit in tiers:
                out = dict(row)
                out["Quantity"] = qty
                out["price_per_unit"] = price_per_unit
                # Out-of-stock no-table stubs: blank fields that couldn't be scraped.
                # item_number holds the URL slug internally for DB deduplication only.
                if out.get("availability") == "Out of Stock" and qty == "":
                    out["item_number"] = None
                    out["product_name"] = None
                writer.writerow(out)

    log.info("exported_csv", path=out_path, record_count=len(records))
    return out_path


async def export_json(db_path: str, output_dir: str) -> str:
    """Export valid/warning records to JSON. Returns the output path."""
    db = await init_db(db_path)
    try:
        records = await get_export_records(db)
    finally:
        await db.close()

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "products.json")

    JSON_FIELDS = [
        "product_group_name",
        "product_name",
        "brand",
        "item_number",
        "manufacturer_number",
        "category_hierarchy",
        "product_group_url",
        "price",
        "unit_size",
        "specifications",
        "availability",
        "description",
        "image_urls",
        "scraped_at",
        "extraction_method",
        "validation_status",
        "validation_notes",
    ]

    cleaned: list[dict[str, Any]] = []
    for rec in records:
        row = dict(rec)
        for key in ("price", "category_hierarchy", "image_urls", "alternative_products", "specifications"):
            if isinstance(row.get(key), str):
                try:
                    row[key] = json.loads(row[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        # Rename group_description -> description; drop raw description (duplicate of unit_size)
        row["description"] = row.pop("group_description", None)
        # Emit only the specified fields in order
        cleaned.append({k: row.get(k) for k in JSON_FIELDS})

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2, default=str)

    log.info("exported_json", path=out_path, record_count=len(cleaned))
    return out_path
