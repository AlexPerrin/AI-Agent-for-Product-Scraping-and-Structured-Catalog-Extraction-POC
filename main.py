"""CLI entrypoint for the Frontier Dental scraping pipeline."""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog
import typer

from config.settings import Settings

# Configure structlog for readable console output
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

app = typer.Typer(
    name="frontier-dental",
    help="AI-powered product scraping pipeline for Safco Dental Supply.",
)


@app.command()
def run(
    reset: bool = typer.Option(False, "--reset/--no-reset", help="Clear all data and start fresh."),
    categories: Optional[list[str]] = typer.Option(None, "--categories", "-c", help="Override target category slugs."),
    skip_browser: bool = typer.Option(False, "--skip-browser", help="Skip product detail page scraping (Playwright)."),
    export_only: bool = typer.Option(False, "--export-only", help="Skip scraping, just export existing data."),
    limit: int = typer.Option(0, "--limit", "-n", help="Cap number of product pages to scrape (0 = unlimited)."),
) -> None:
    """Run the full scraping pipeline."""
    settings = Settings()

    if categories:
        settings.target_categories = categories

    if not settings.openrouter_api_key:
        log = structlog.get_logger()
        log.warning(
            "OPENROUTER_API_KEY not set. LLM steps (extractor fallback, normalizer, validator) "
            "will be skipped or use defaults."
        )

    from pipeline.orchestrator import Orchestrator

    orchestrator = Orchestrator()
    asyncio.run(orchestrator.run(settings, reset=reset, skip_browser=skip_browser, export_only=export_only, product_limit=limit))


@app.command()
def status() -> None:
    """Show current job queue and product count status."""

    async def _status() -> None:
        from storage.database import get_job_counts, get_product_counts, init_db

        settings = Settings()
        db = await init_db(settings.db_path)
        try:
            job_counts = await get_job_counts(db)
            product_counts = await get_product_counts(db)
        finally:
            await db.close()

        typer.echo("\n--- Job Queue ---")
        if job_counts:
            for status_name, count in sorted(job_counts.items()):
                typer.echo(f"  {status_name}: {count}")
        else:
            typer.echo("  (empty)")

        typer.echo("\n--- Products ---")
        typer.echo(f"  Total: {product_counts.get('total', 0)}")
        by_status = product_counts.get("by_validation_status", {})
        if by_status:
            for vs, count in sorted(by_status.items()):
                typer.echo(f"  {vs}: {count}")
        by_method = product_counts.get("by_extraction_method", {})
        if by_method:
            typer.echo("\n--- Extraction Methods ---")
            for method, count in sorted(by_method.items()):
                typer.echo(f"  {method}: {count}")
        typer.echo("")

    asyncio.run(_status())


@app.command(name="normalize")
def normalize_cmd() -> None:
    """Re-run the normalizer on existing extracted data and export."""

    async def _normalize() -> None:
        from agents.normalizer import NormalizerAgent
        from storage.database import init_db, reset_normalization
        from storage.export import export_csv, export_json

        settings = Settings()
        db = await init_db(settings.db_path)
        try:
            await reset_normalization(db)
            normalizer = NormalizerAgent()
            await normalizer.run(settings, db)
            csv_path = await export_csv(settings.db_path, settings.output_dir)
            json_path = await export_json(settings.db_path, settings.output_dir)
            typer.echo(f"Exported CSV:  {csv_path}")
            typer.echo(f"Exported JSON: {json_path}")
        finally:
            await db.close()

    asyncio.run(_normalize())


@app.command(name="validate")
def validate_cmd() -> None:
    """Cross-check spec-key consistency within product groups and export."""

    async def _validate() -> None:
        from agents.validator import ValidatorAgent
        from storage.export import export_csv, export_json

        settings = Settings()

        from storage.database import init_db
        db = await init_db(settings.db_path)
        try:
            validator = ValidatorAgent()
            await validator.run(settings, db)
            csv_path = await export_csv(settings.db_path, settings.output_dir)
            json_path = await export_json(settings.db_path, settings.output_dir)
            typer.echo(f"Exported CSV:  {csv_path}")
            typer.echo(f"Exported JSON: {json_path}")
        finally:
            await db.close()

    asyncio.run(_validate())


@app.command(name="export")
def export_cmd() -> None:
    """Export existing data to CSV and JSON."""

    async def _export() -> None:
        from storage.export import export_csv, export_json

        settings = Settings()
        csv_path = await export_csv(settings.db_path, settings.output_dir)
        json_path = await export_json(settings.db_path, settings.output_dir)
        typer.echo(f"Exported CSV:  {csv_path}")
        typer.echo(f"Exported JSON: {json_path}")

    asyncio.run(_export())


if __name__ == "__main__":
    app()
