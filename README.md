# AI Agent for Product Scraping and Structured Catalog Extraction

An AI-powered ETL pipeline that extracts structured product catalog data from Safco Dental Supply (safcodental.com). Built as a proof-of-concept for Frontier Dental's competitive intelligence workflow.

## Architecture Overview

```
                          main.py (Typer CLI)
                               |
                    +-----------+-----------+
                    |     ORCHESTRATOR      |
                    |  Sequences all stages |
                    +-----------+-----------+
                               |
         +---------------------+---------------------+
         |                     |                      |
   [1] Navigator        [2] Category           [3] Product Scraper
   Sitemap XML ----->   Scraper (httpx) ----->  + Extractor
   catalog.xml          JSON-LD parsing        Playwright render
   products.xml         Partial records        CSS selectors (primary)
   URL queue            to SQLite              LLM tool_use (fallback)
         |                     |                      |
         +---------------------+---------------------+
                               |
                    +----------+----------+
                    |                     |
              [4] Normalizer        [5] Validator
              LLM batch norm        LLM semantic QA
              unit_size, brand      valid/warning/invalid
              (Haiku - cheap)       (Sonnet - reasoning)
                    |                     |
                    +----------+----------+
                               |
                    +----------+----------+
                    |       EXPORT         |
                    |  CSV + JSON output   |
                    +----------------------+

Data flows through SQLite (job queue + product store).
Each stage reads/writes job status, making the pipeline resumable.
```

## Why This Approach

**Agent-based architecture** -- Each stage is an independent agent with a single responsibility. Agents communicate through a shared SQLite database rather than direct calls, which makes the pipeline resumable (kill and restart from where it left off) and observable (query the DB to see progress at any time).

**CSS selectors as primary extraction** -- For the known Magento/Hyva DOM structure, CSS selectors are fast, free, and deterministic. The LLM fallback only activates when selectors return empty results, keeping the hot path cost-free.

**LLM where it adds real value** -- AI is used at exactly three points: (1) extraction fallback when CSS selectors fail, (2) unit size and brand normalization where regex is unmaintainable, and (3) semantic validation catching errors that structural checks cannot. Everything else is deterministic code.

**Sitemap-first discovery** -- Rather than spidering the site, the pipeline reads `catalog.xml` and `products.xml` directly. This is faster, respects `robots.txt`, and gives a complete URL inventory without pagination concerns.

## Agent Responsibilities

| Agent | LLM? | What It Does |
|-------|------|-------------|
| **Navigator** | No | Parses sitemap XML files, filters URLs to target categories, populates the job queue |
| **Category Scraper** | No | Fetches category pages via HTTP, extracts product metadata from JSON-LD structured data |
| **Product Scraper** | No | Renders JS-heavy product pages using Playwright headless browser |
| **Extractor** | Hybrid | CSS selector extraction (primary), LLM tool_use extraction (fallback when selectors fail) |
| **Normalizer** | Yes | Batch-normalizes `unit_size` and `brand` fields using a cheap/fast model |
| **Validator** | Yes | Semantic quality validation -- catches logical errors like $0.01 prices or misplaced field values |

## Setup and Execution

### Prerequisites

- Python 3.11+
- An OpenRouter API key (for LLM steps)

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd Frontier-Dental-POC

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium

# Configure environment
cp .env.example .env
# Edit .env and add your OPENROUTER_API_KEY
```

### Running the Pipeline

```bash
# Full pipeline run
python main.py run

# Fresh start (clears all existing data)
python main.py run --reset

# Override target categories
python main.py run --categories sutures-surgical-products --categories gloves

# Skip browser rendering (only run discovery + category scraping)
python main.py run --skip-browser

# Export only (no scraping, just generate CSV/JSON from existing data)
python main.py run --export-only

# Check pipeline status
python main.py status

# Export existing data
python main.py export
```

### Running Without an API Key

The pipeline works without an OpenRouter API key -- it will skip LLM-dependent steps (extractor fallback, normalizer, validator) and use defaults. The CSS selector extraction path and all deterministic stages run normally.

## Output Schema

One row per orderable variant (SKU). Both CSV and JSON exports use this schema:

```json
{
  "product_group_name": "Adper Scotchbond Multi-Purpose",
  "variant_name": "Scotchbond Multi-Purpose System Kit",
  "brand": "3M / Solventum",
  "safco_item_number": "2510134",
  "manufacturer_part_number": "7540S",
  "category_hierarchy": ["Dental Supplies", "Bonding Agents"],
  "product_group_url": "https://www.safcodental.com/product/adper-scotchbond-multi-purpose",
  "price": {"1": "553.99"},
  "unit_size": "1/kit",
  "availability": "In Stock",
  "group_description": "Complete bonding system for...",
  "subgroup_name": "Adper Scotchbond Multi-Purpose",
  "image_urls": ["https://www.safcodental.com/media/catalog/product/..."],
  "alternative_products": [],
  "scraped_at": "2026-03-16T12:00:00",
  "extraction_method": "css-selector",
  "validation_status": "valid",
  "validation_notes": null
}
```

**Price field**: A dict mapping quantity tier to price. Single-price items: `{"1": "36.99"}`. Volume pricing: `{"1": "36.99", "6": "32.99", "12": "29.99"}`.

**CSV format**: The CSV flattens complex fields -- price becomes `Qty 1: $36.99 | Qty 6: $32.99`, lists become pipe-separated strings.

## Limitations

1. **No proxy rotation.** Requests come from a single IP. High-volume runs may trigger rate limiting.

2. **Playwright is slow.** Rendering JS-heavy Magento pages takes 2-5 seconds each vs ~100ms for static HTTP. The POC scope (two categories) is manageable; full-site crawls need distributed browser workers.

3. **LLM fallback adds cost.** If CSS selectors degrade across many pages (e.g., after a site redesign), LLM calls become expensive. The `extraction_method` metric provides early warning.

4. **No image downloading.** Image URLs are stored, not the images themselves.

5. **Category membership is sitemap-inferred.** Products are assigned to categories based on sitemap discovery. Cross-listed products only get their first category association.

6. **Single-process execution.** The pipeline runs in one process with async concurrency. Production scale requires distributed workers.

## Failure Handling

| Failure Mode | How It Is Handled |
|-------------|-------------------|
| HTTP 404 on category page | Job marked `skipped_404`, logged, pipeline continues |
| HTTP error / timeout | Retried 3 times with exponential backoff (1s, 2s, 4s via tenacity) |
| Playwright navigation timeout | Retried 2 times, then job marked `tier2_failed` with error details |
| CSS selectors return empty | Automatic fallback to LLM extraction |
| LLM API error | Logged, batch skipped, pipeline continues with next batch |
| LLM returns unparseable JSON | Logged, batch marked with defaults, pipeline continues |
| No API key configured | LLM steps skipped gracefully with warnings; deterministic steps run normally |
| Pipeline killed mid-run | Restart picks up from last checkpoint (each job has status in SQLite) |
| Duplicate products | `UNIQUE` constraint on `safco_item_number`; upserts update existing records |

## How to Scale to Full-Site Crawling

1. **Remove category filtering** -- Set `TARGET_CATEGORIES` to all category slugs or remove the filter entirely in the Navigator agent.

2. **Distributed browser rendering** -- Replace the single Playwright process with a Playwright cluster or Browserless.io pool. The semaphore-based concurrency model already supports this pattern.

3. **Replace SQLite with PostgreSQL** -- Add proper indexing on `safco_item_number`, `category_slug`, and `scraped_at`. Use connection pooling (asyncpg).

4. **Use a proper job queue** -- Replace the SQLite jobs table with Redis + ARQ or Celery for distributed task processing with dead-letter queues.

5. **Add proxy rotation** -- Configure rotating proxies and user agents to distribute request load across IPs.

6. **Schedule with Airflow or cron** -- Run differential scrapes (only reprocess products whose content hash changed) on a regular schedule.

7. **Increase rate limits** -- Tune `REQUEST_DELAY` and `BROWSER_CONCURRENCY` based on observed rate limiting behavior.

## How to Monitor Data Quality

1. **LLM fallback rate** -- Track the ratio of `extraction_method="llm-fallback"` records. A rising rate signals CSS selector degradation (site DOM changed). Run `python main.py status` to see current counts.

2. **Validation status distribution** -- Monitor the `valid` / `warning` / `invalid` split. A spike in `invalid` records indicates extraction problems.

3. **Known-page regression tests** -- Maintain a set of 10-20 known product pages with expected output. Run extraction against them after each deploy and alert if results diverge.

4. **Structured logging** -- All pipeline stages use `structlog` with structured key-value output. Ingest logs into Datadog, Grafana, or similar for dashboards and alerting.

5. **Database queries** -- The SQLite database is the source of truth. Query it directly to audit specific records, check job queue health, or investigate extraction failures:

```bash
# Count by validation status
sqlite3 frontier_dental.db "SELECT validation_status, COUNT(*) FROM products GROUP BY validation_status"

# Find LLM fallback records
sqlite3 frontier_dental.db "SELECT safco_item_number, variant_name FROM products WHERE extraction_method='llm-fallback'"

# Check failed jobs
sqlite3 frontier_dental.db "SELECT url, error_msg FROM jobs WHERE status='tier2_failed'"
```
