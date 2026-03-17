from __future__ import annotations

import asyncio
from datetime import datetime

import structlog

from agents.category_scraper import CategoryScraperAgent
from agents.extractor import ExtractorAgent
from agents.navigator import NavigatorAgent
from agents.normalizer import NormalizerAgent
from agents.validator import ValidatorAgent
from config.settings import Settings
from models.product import JobStatus
from scraper.browser import BrowserRenderer
from scraper.http_client import HttpClient
from storage.database import (
    delete_json_ld_stubs_for_url,
    get_category_hierarchy_for_url,
    get_pending_jobs,
    get_product_counts,
    init_db,
    reset_db,
    update_job_status,
    upsert_product,
)
from storage.export import export_csv, export_json

log = structlog.get_logger()


class Orchestrator:
    """Sequences all pipeline stages and reports results."""

    async def run(
        self,
        settings: Settings,
        reset: bool = False,
        skip_browser: bool = False,
        export_only: bool = False,
        product_limit: int = 0,
    ) -> None:
        log.info("orchestrator_start", reset=reset, skip_browser=skip_browser, export_only=export_only, product_limit=product_limit)
        start_time = datetime.utcnow()

        db = await init_db(settings.db_path)

        try:
            if reset:
                await reset_db(db)

            if export_only:
                await self._export(settings)
                return

            # --- Step 1: Navigator ---
            log.info("stage_navigator")
            navigator = NavigatorAgent()
            await navigator.run(settings, db)

            # --- Step 2: Category Scraper ---
            log.info("stage_category_scraper")
            http_client = HttpClient(delay=settings.request_delay)
            try:
                cat_scraper = CategoryScraperAgent()
                await cat_scraper.run(settings, db, http_client)
            finally:
                await http_client.close()

            # --- Step 3: Product Scraper + Extractor ---
            if not skip_browser:
                log.info("stage_product_scraper_extractor")
                await self._run_product_extraction(settings, db, product_limit=product_limit)
            else:
                log.info("stage_product_scraper_skipped")

            # --- Step 4: Normalizer (disabled) ---
            # --- Step 5: Validator (disabled) ---

            # --- Step 6: Export ---
            log.info("stage_export")
            await self._export(settings)

            # --- Summary ---
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            counts = await get_product_counts(db)
            self._print_summary(counts, elapsed)

        finally:
            await db.close()

    async def _run_product_extraction(self, settings: Settings, db, product_limit: int = 0) -> None:
        """Render product pages with Playwright and extract variants."""
        # Get product jobs that are ready for tier-2 processing.
        # These are either tier1_complete (discovered by category scraper)
        # or pending product jobs that we want to try.
        product_jobs = await get_pending_jobs(db, job_type="product", status="tier1_complete")

        # Also grab any pending product jobs (from sitemap but not in
        # category scraper output -- these may or may not be in scope)
        pending_product_jobs = await get_pending_jobs(db, job_type="product", status="pending")

        # Only process pending product jobs if they have a category_slug
        # (meaning they were associated with a target category)
        scoped_pending = [j for j in pending_product_jobs if j.get("category_slug")]
        all_jobs = product_jobs + scoped_pending

        if product_limit > 0:
            all_jobs = all_jobs[:product_limit]
            log.info("product_extraction_limit_applied", limit=product_limit)

        if not all_jobs:
            log.info("no_product_jobs_for_extraction")
            return

        log.info("product_extraction_start", job_count=len(all_jobs))

        browser = BrowserRenderer()
        await browser.start()

        semaphore = asyncio.Semaphore(settings.browser_concurrency)
        extractor = ExtractorAgent()

        async def process_one(job: dict) -> None:
            url = job["url"]
            job_id = job["id"]
            try:
                # Grab category_hierarchy from any existing json-ld stub before overwriting
                category_hierarchy = await get_category_hierarchy_for_url(db, url)

                html = await browser.render_page(url, semaphore)
                variants = await extractor.extract(url, html, settings)

                if variants:
                    # Delete json-ld stubs for this URL (replaced by real CSS data)
                    await delete_json_ld_stubs_for_url(db, url)
                    for v in variants:
                        v["scraped_at"] = datetime.utcnow().isoformat()
                        if category_hierarchy and not v.get("category_hierarchy"):
                            v["category_hierarchy"] = category_hierarchy
                        await upsert_product(db, v)
                    await update_job_status(db, job_id, JobStatus.tier2_complete.value)
                    log.info("product_extracted", url=url, variants=len(variants))
                else:
                    await update_job_status(
                        db, job_id, JobStatus.tier2_failed.value,
                        "No variants extracted",
                    )
                    log.warning("product_extraction_empty", url=url)
            except Exception as exc:
                log.error("product_extraction_error", url=url, error=str(exc))
                await update_job_status(db, job_id, JobStatus.tier2_failed.value, str(exc)[:500])

        # Process concurrently in batches
        tasks = [process_one(job) for job in all_jobs]
        await asyncio.gather(*tasks, return_exceptions=True)

        await browser.stop()
        log.info("product_extraction_complete")

    async def _export(self, settings: Settings) -> None:
        """Run CSV and JSON export."""
        csv_path = await export_csv(settings.db_path, settings.output_dir)
        json_path = await export_json(settings.db_path, settings.output_dir)
        log.info("export_complete", csv=csv_path, json=json_path)

    def _print_summary(self, counts: dict, elapsed: float) -> None:
        """Print a human-readable summary of the pipeline run."""
        total = counts.get("total", 0)
        by_status = counts.get("by_validation_status", {})
        by_method = counts.get("by_extraction_method", {})

        valid = by_status.get("valid", 0)
        warning = by_status.get("warning", 0)
        invalid = by_status.get("invalid", 0)

        css_count = by_method.get("css-selector", 0)
        llm_count = by_method.get("llm-fallback", 0)
        jsonld_count = by_method.get("json-ld", 0)

        llm_fallback_rate = (llm_count / total * 100) if total > 0 else 0.0

        log.info(
            "pipeline_summary",
            total_records=total,
            valid=valid,
            warning=warning,
            invalid=invalid,
            css_selector=css_count,
            llm_fallback=llm_count,
            json_ld=jsonld_count,
            llm_fallback_rate_pct=round(llm_fallback_rate, 1),
            elapsed_seconds=round(elapsed, 1),
        )
