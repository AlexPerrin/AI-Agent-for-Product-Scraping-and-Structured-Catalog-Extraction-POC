from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import structlog
from bs4 import BeautifulSoup

from config.settings import Settings
from models.product import JobStatus
from scraper.http_client import HttpClient
from storage.database import (
    get_pending_jobs,
    promote_product_job,
    update_job_status,
    upsert_product,
)

log = structlog.get_logger()


class CategoryScraperAgent:
    """Scrapes category listing pages via plain HTTP, extracting product
    metadata from JSON-LD structured data."""

    async def run(self, settings: Settings, db, http_client: HttpClient) -> None:
        log.info("category_scraper_start")

        jobs = await get_pending_jobs(db, job_type="category", status="pending")
        log.info("category_scraper_pending_jobs", count=len(jobs))

        processed = 0
        skipped_404 = 0

        for job in jobs:
            url = job["url"]
            job_id = job["id"]
            category_slug = job.get("category_slug")

            try:
                status_code, html = await http_client.get_page_no_raise(url)
            except Exception as exc:
                log.error("category_fetch_error", url=url, error=str(exc))
                await update_job_status(db, job_id, JobStatus.tier2_failed.value, str(exc))
                continue

            if status_code == 404:
                log.warning("category_404", url=url)
                await update_job_status(db, job_id, JobStatus.skipped_404.value)
                skipped_404 += 1
                continue

            if status_code != 200:
                log.warning("category_unexpected_status", url=url, status=status_code)
                await update_job_status(
                    db, job_id, JobStatus.tier2_failed.value,
                    f"HTTP {status_code}",
                )
                continue

            # Parse JSON-LD and breadcrumbs
            soup = BeautifulSoup(html, "lxml")
            products = self._extract_json_ld_products(soup, url)
            category_hierarchy = self._extract_breadcrumb(soup)

            # Upsert partial product records and enqueue product URLs
            for prod in products:
                prod["category_hierarchy"] = category_hierarchy
                prod["scraped_at"] = datetime.utcnow().isoformat()
                await upsert_product(db, prod)

                # Promote the product job to tier1_complete so the
                # Product Scraper picks it up. Uses upsert so it works
                # whether the Navigator pre-inserted it or not.
                if prod.get("product_group_url"):
                    await promote_product_job(
                        db,
                        url=prod["product_group_url"],
                        category_slug=category_slug,
                    )

            await db.commit()
            await update_job_status(db, job_id, JobStatus.tier1_complete.value)
            processed += 1
            log.debug(
                "category_scraped",
                url=url,
                products_found=len(products),
            )

        log.info(
            "category_scraper_complete",
            processed=processed,
            skipped_404=skipped_404,
        )

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_json_ld_products(
        self, soup: BeautifulSoup, page_url: str
    ) -> list[dict[str, Any]]:
        """Extract product data from JSON-LD ItemList blocks."""
        products: list[dict[str, Any]] = []

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue

            # Handle both direct ItemList and @graph arrays
            items_to_check = [data]
            if isinstance(data, list):
                items_to_check = data
            if isinstance(data, dict) and "@graph" in data:
                items_to_check = data["@graph"]

            for item in items_to_check:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") != "ItemList":
                    continue

                for element in item.get("itemListElement", []):
                    prod = self._parse_item_list_element(element, page_url)
                    if prod:
                        products.append(prod)

        return products

    def _parse_item_list_element(
        self, element: dict[str, Any], page_url: str
    ) -> dict[str, Any] | None:
        """Parse a single itemListElement into a partial product record."""
        # URL lives on the ListItem itself, not the nested Product object
        url = element.get("url", "")
        item = element.get("item", element)
        if not isinstance(item, dict):
            return None

        name = item.get("name", "")
        if not url:
            url = item.get("url", "")
        if not name:
            return None

        # Price from offers
        price_dict: dict[int, str] = {}
        offers = item.get("offers", {})
        if isinstance(offers, dict):
            raw_price = offers.get("price")
            if raw_price is not None:
                try:
                    price_dict = {1: str(raw_price)}
                except (ValueError, TypeError):
                    pass
        elif isinstance(offers, list):
            for i, offer in enumerate(offers):
                raw_price = offer.get("price")
                if raw_price is not None:
                    price_dict[i + 1] = str(raw_price)

        availability = ""
        if isinstance(offers, dict):
            avail_raw = offers.get("availability", "")
            # Schema.org uses URLs like "https://schema.org/InStock"
            availability = avail_raw.rsplit("/", 1)[-1] if avail_raw else ""
            # Map schema.org values to human-readable
            avail_map = {
                "InStock": "In Stock",
                "OutOfStock": "Out of Stock",
                "PreOrder": "Special Order",
                "BackOrder": "Backorder",
                "LimitedAvailability": "In Stock",
            }
            availability = avail_map.get(availability, availability)

        brand = ""
        brand_data = item.get("brand", {})
        if isinstance(brand_data, dict):
            brand = brand_data.get("name", "")
        elif isinstance(brand_data, str):
            brand = brand_data

        image_url = item.get("image", "")
        image_urls = [image_url] if image_url else []

        sku = item.get("sku", "")

        return {
            "product_group_name": name,
            "product_name": name,
            "brand": brand or None,
            "item_number": sku or name,  # fallback to name if no SKU
            "product_group_url": url,
            "price": price_dict,
            "availability": availability or None,
            "image_urls": image_urls,
            "extraction_method": "json-ld",
            "validation_status": "pending",
        }

    def _extract_breadcrumb(self, soup: BeautifulSoup) -> list[str]:
        """Extract category hierarchy from breadcrumb nav or JSON-LD BreadcrumbList."""
        # Try JSON-LD BreadcrumbList first
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue

            items_to_check = [data]
            if isinstance(data, list):
                items_to_check = data
            if isinstance(data, dict) and "@graph" in data:
                items_to_check = data["@graph"]

            for item in items_to_check:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") != "BreadcrumbList":
                    continue
                elements = item.get("itemListElement", [])
                # Sort by position and extract names
                sorted_elems = sorted(elements, key=lambda e: e.get("position", 0))
                crumbs = []
                for elem in sorted_elems:
                    name = ""
                    if isinstance(elem.get("item"), dict):
                        name = elem["item"].get("name", "")
                    elif isinstance(elem.get("name"), str):
                        name = elem["name"]
                    if name and name.lower() != "home":
                        crumbs.append(name)
                if crumbs:
                    return crumbs

        # Fallback: HTML breadcrumb nav
        nav = soup.find("nav", attrs={"aria-label": "Breadcrumb"})
        if not nav:
            nav = soup.find("nav", class_="breadcrumbs")
        if not nav:
            nav = soup.find("div", class_="breadcrumbs")
        if nav:
            links = nav.find_all("a")
            crumbs = [a.get_text(strip=True) for a in links if a.get_text(strip=True).lower() != "home"]
            if crumbs:
                return crumbs

        return []
