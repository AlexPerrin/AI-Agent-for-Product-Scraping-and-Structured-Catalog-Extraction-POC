from __future__ import annotations

from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
import structlog

from config.settings import Settings
from storage.database import insert_job

log = structlog.get_logger()

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
BASE_URL = "https://www.safcodental.com"
CATALOG_XML = f"{BASE_URL}/catalog.xml"
PRODUCTS_XML = f"{BASE_URL}/products.xml"


class NavigatorAgent:
    """Discovers category and product URLs from the sitemaps and populates
    the job queue in SQLite."""

    async def run(self, settings: Settings, db) -> None:
        log.info("navigator_start")

        category_urls = await self._fetch_sitemap_urls(CATALOG_XML)
        product_urls = await self._fetch_sitemap_urls(PRODUCTS_XML)

        # Filter categories to only those under target_categories
        filtered_cats = self._filter_category_urls(category_urls, settings.target_categories)
        log.info("navigator_categories_filtered", total=len(category_urls), matched=len(filtered_cats))

        # For products, we'll insert all from products.xml for now -- the
        # category scraper will later associate them.  We can also filter
        # products by checking if their slug relates to a target category,
        # but products.xml product URLs don't carry category info directly.
        # So we insert all product URLs and rely on the category scraper
        # to discover which ones belong to target categories.

        # Insert category jobs
        cat_count = 0
        for url in filtered_cats:
            slug = self._category_slug(url, settings.target_categories)
            await insert_job(db, url=url, job_type="category", category_slug=slug)
            cat_count += 1
        await db.commit()

        # Insert product jobs (all from sitemap -- the orchestrator will
        # only process those discovered by the category scraper)
        prod_count = 0
        for url in product_urls:
            await insert_job(db, url=url, job_type="product")
            prod_count += 1
        await db.commit()

        log.info(
            "navigator_complete",
            category_jobs=cat_count,
            product_jobs=prod_count,
        )

    # ------------------------------------------------------------------

    async def _fetch_sitemap_urls(self, sitemap_url: str) -> list[str]:
        """Fetch and parse a sitemap XML, returning all <loc> URLs."""
        async with httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FrontierDentalBot/1.0)"},
        ) as client:
            resp = await client.get(sitemap_url)
            resp.raise_for_status()

        root = ElementTree.fromstring(resp.text)
        urls: list[str] = []
        for loc in root.findall(".//sm:loc", SITEMAP_NS):
            if loc.text:
                urls.append(loc.text.strip())
        log.debug("sitemap_parsed", url=sitemap_url, count=len(urls))
        return urls

    def _filter_category_urls(
        self, urls: list[str], target_categories: list[str]
    ) -> list[str]:
        """Keep only catalog URLs that match one of the target categories
        (exact slug or sub-path)."""
        matched: list[str] = []
        for url in urls:
            path = urlparse(url).path  # e.g. /catalog/gloves/nitrile-gloves
            # Strip leading /catalog/
            remainder = path.removeprefix("/catalog/")
            if not remainder:
                continue
            top_slug = remainder.split("/")[0]
            if top_slug in target_categories:
                matched.append(url)
        return matched

    def _category_slug(self, url: str, target_categories: list[str]) -> str | None:
        """Return the matching target category slug for a catalog URL."""
        path = urlparse(url).path
        remainder = path.removeprefix("/catalog/")
        if not remainder:
            return None
        top_slug = remainder.split("/")[0]
        if top_slug in target_categories:
            return top_slug
        return None
