from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import aiosqlite
import structlog

log = structlog.get_logger()

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL UNIQUE,
    job_type    TEXT NOT NULL,          -- 'category' or 'product'
    status      TEXT NOT NULL DEFAULT 'pending',
    category_slug TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    error_msg   TEXT
);
"""

PRODUCTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    product_group_name        TEXT NOT NULL,
    product_name              TEXT NOT NULL,
    brand                     TEXT,
    item_number               TEXT NOT NULL UNIQUE,
    manufacturer_number       TEXT,
    category_hierarchy        TEXT DEFAULT '[]',    -- JSON array
    product_group_url         TEXT NOT NULL,
    price                     TEXT DEFAULT '{}',    -- JSON dict  {qty: price_str}
    description               TEXT,
    unit_size                 TEXT,
    specifications            TEXT DEFAULT '{}',    -- JSON dict  e.g. {"Size": "X-small"}
    availability              TEXT,
    group_description         TEXT,
    subgroup_name             TEXT,
    image_urls                TEXT DEFAULT '[]',    -- JSON array
    alternative_products      TEXT DEFAULT '[]',    -- JSON array
    scraped_at                TEXT NOT NULL DEFAULT (datetime('now')),
    extraction_method         TEXT DEFAULT 'css-selector',
    validation_status         TEXT DEFAULT 'pending',
    validation_notes          TEXT
);
"""


async def init_db(db_path: str) -> aiosqlite.Connection:
    """Create tables if needed and return an open connection."""
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute(JOBS_SCHEMA)
    await db.execute(PRODUCTS_SCHEMA)
    # Migration: add specifications column if it doesn't exist yet
    try:
        await db.execute("ALTER TABLE products ADD COLUMN specifications TEXT DEFAULT '{}'")
        await db.commit()
        log.info("database_migrated_specifications_column")
    except Exception:
        pass  # Column already exists
    await db.commit()
    log.info("database_initialised", path=db_path)
    return db


async def reset_db(db: aiosqlite.Connection) -> None:
    """Drop and recreate all tables."""
    await db.execute("DROP TABLE IF EXISTS jobs")
    await db.execute("DROP TABLE IF EXISTS products")
    await db.execute(JOBS_SCHEMA)
    await db.execute(PRODUCTS_SCHEMA)
    await db.commit()
    log.info("database_reset")


# ---------------------------------------------------------------------------
# Job queue helpers
# ---------------------------------------------------------------------------

async def insert_job(
    db: aiosqlite.Connection,
    url: str,
    job_type: str,
    category_slug: Optional[str] = None,
) -> None:
    """Insert a job, silently skipping duplicates."""
    await db.execute(
        """
        INSERT OR IGNORE INTO jobs (url, job_type, category_slug)
        VALUES (?, ?, ?)
        """,
        (url, job_type, category_slug),
    )


async def get_pending_jobs(
    db: aiosqlite.Connection,
    job_type: str,
    status: str = "pending",
) -> list[dict[str, Any]]:
    """Return all jobs of *job_type* with the given *status*."""
    cursor = await db.execute(
        "SELECT * FROM jobs WHERE job_type = ? AND status = ?",
        (job_type, status),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_job_status(
    db: aiosqlite.Connection,
    job_id: int,
    status: str,
    error_msg: Optional[str] = None,
) -> None:
    await db.execute(
        """
        UPDATE jobs
           SET status = ?, updated_at = datetime('now'), error_msg = ?
         WHERE id = ?
        """,
        (status, error_msg, job_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Product helpers
# ---------------------------------------------------------------------------

def _serialise_product(data: dict[str, Any]) -> dict[str, Any]:
    """Convert Python objects to DB-storable strings."""
    out = dict(data)
    for key in ("price", "category_hierarchy", "image_urls", "alternative_products"):
        if key in out and not isinstance(out[key], str):
            out[key] = json.dumps(out[key], default=str)
    if "scraped_at" in out and isinstance(out["scraped_at"], datetime):
        out["scraped_at"] = out["scraped_at"].isoformat()
    return out


async def upsert_product(db: aiosqlite.Connection, data: dict[str, Any]) -> None:
    """Insert or update a product variant keyed on item_number."""
    d = _serialise_product(data)
    cols = [
        "product_group_name", "product_name", "brand", "item_number",
        "manufacturer_number", "category_hierarchy", "product_group_url",
        "price", "description", "unit_size", "availability", "group_description",
        "subgroup_name", "image_urls", "alternative_products",
        "scraped_at", "extraction_method", "validation_status", "validation_notes",
    ]
    # Build only columns present in the data dict
    present = [c for c in cols if c in d]
    placeholders = ", ".join(["?"] * len(present))
    col_names = ", ".join(present)
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in present if c != "item_number")

    await db.execute(
        f"""
        INSERT INTO products ({col_names})
        VALUES ({placeholders})
        ON CONFLICT(item_number) DO UPDATE SET {update_clause}
        """,
        tuple(d[c] for c in present),
    )
    await db.commit()


async def get_products_for_normalization(
    db: aiosqlite.Connection,
    batch_size: int = 50,
) -> list[dict[str, Any]]:
    """Return products that have been extracted but not yet normalized."""
    cursor = await db.execute(
        """
        SELECT id, item_number, product_name, unit_size, brand,
               product_group_name, group_description
          FROM products
         WHERE extraction_method IS NOT NULL
           AND extraction_method != 'json-ld'
           AND validation_status = 'pending'
         LIMIT ?
        """,
        (batch_size,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_normalized_fields(
    db: aiosqlite.Connection,
    product_id: int,
    unit_size: Optional[str],
    brand: Optional[str],
    specifications: Optional[str] = None,
) -> None:
    await db.execute(
        "UPDATE products SET unit_size = ?, brand = ?, specifications = ? WHERE id = ?",
        (unit_size, brand, specifications or "{}", product_id),
    )
    await db.commit()


async def get_all_normalized_products(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all normalized products for cross-group validation."""
    cursor = await db.execute(
        """
        SELECT id, product_group_name, product_name, group_description, specifications
          FROM products
         WHERE validation_status = 'normalized'
        """
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_product_specifications(
    db: aiosqlite.Connection,
    product_id: int,
    specifications: str,
) -> None:
    await db.execute(
        "UPDATE products SET specifications = ? WHERE id = ?",
        (specifications, product_id),
    )
    await db.commit()


async def update_validation_fields(
    db: aiosqlite.Connection,
    product_id: int,
    validation_status: str,
    validation_notes: Optional[str],
) -> None:
    await db.execute(
        "UPDATE products SET validation_status = ?, validation_notes = ? WHERE id = ?",
        (validation_status, validation_notes, product_id),
    )
    await db.commit()


async def reset_normalization(db: aiosqlite.Connection) -> None:
    """Reset all products back to pending so the normalizer re-processes them."""
    await db.execute(
        "UPDATE products SET validation_status = 'pending', specifications = '{}'"
        " WHERE extraction_method = 'css-selector'"
    )
    await db.commit()


async def mark_products_normalized(db: aiosqlite.Connection, ids: list[int]) -> None:
    """Mark a batch of products as having completed normalisation."""
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"UPDATE products SET validation_status = 'normalized' WHERE id IN ({placeholders})",
        ids,
    )
    await db.commit()


async def get_export_records(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return all css-selector extracted records for export (excludes json-ld stubs)."""
    cursor = await db.execute(
        """
        SELECT * FROM products
         WHERE extraction_method = 'css-selector'
           AND validation_status NOT IN ('invalid')
        """
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def promote_product_job(
    db: aiosqlite.Connection,
    url: str,
    category_slug: Optional[str],
) -> None:
    """Mark a product job as tier1_complete and set its category_slug.

    Works whether the job was pre-inserted by the Navigator (pending) or
    is being inserted fresh by the Category Scraper.
    """
    await db.execute(
        """
        INSERT INTO jobs (url, job_type, status, category_slug)
        VALUES (?, 'product', 'tier1_complete', ?)
        ON CONFLICT(url) DO UPDATE SET
            status = 'tier1_complete',
            category_slug = COALESCE(excluded.category_slug, category_slug),
            updated_at = datetime('now')
        """,
        (url, category_slug),
    )


async def get_category_hierarchy_for_url(
    db: aiosqlite.Connection, url: str
) -> list | None:
    """Return the category_hierarchy of an existing stub record for this URL."""
    cursor = await db.execute(
        "SELECT category_hierarchy FROM products WHERE product_group_url = ? LIMIT 1",
        (url,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    val = row["category_hierarchy"]
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return None
    return val


async def delete_json_ld_stubs_for_url(
    db: aiosqlite.Connection, url: str
) -> None:
    """Delete category-scraper json-ld stub records for a URL (replaced by CSS extraction)."""
    await db.execute(
        "DELETE FROM products WHERE product_group_url = ? AND extraction_method = 'json-ld'",
        (url,),
    )
    await db.commit()


async def get_all_product_urls(db: aiosqlite.Connection) -> set[str]:
    """Return the set of all product_group_url values currently stored."""
    cursor = await db.execute("SELECT DISTINCT product_group_url FROM products")
    rows = await cursor.fetchall()
    return {r["product_group_url"] for r in rows}


async def get_job_counts(db: aiosqlite.Connection) -> dict[str, int]:
    """Return a summary of job counts by status."""
    cursor = await db.execute(
        "SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status"
    )
    rows = await cursor.fetchall()
    return {r["status"]: r["cnt"] for r in rows}


async def get_product_counts(db: aiosqlite.Connection) -> dict[str, Any]:
    """Return product count summary."""
    cursor = await db.execute("SELECT COUNT(*) as total FROM products")
    total = (await cursor.fetchone())["total"]

    cursor = await db.execute(
        "SELECT validation_status, COUNT(*) as cnt FROM products GROUP BY validation_status"
    )
    rows = await cursor.fetchall()
    by_status = {r["validation_status"]: r["cnt"] for r in rows}

    cursor = await db.execute(
        "SELECT extraction_method, COUNT(*) as cnt FROM products GROUP BY extraction_method"
    )
    rows = await cursor.fetchall()
    by_method = {r["extraction_method"]: r["cnt"] for r in rows}

    return {"total": total, "by_validation_status": by_status, "by_extraction_method": by_method}
