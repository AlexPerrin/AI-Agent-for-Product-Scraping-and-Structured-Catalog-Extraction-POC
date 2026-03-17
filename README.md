# AI Agent for Product Scraping and Structured Catalog Extraction

An AI-powered ETL pipeline that extracts structured product catalog data from Safco Dental Supply (safcodental.com). Built as a proof-of-concept for Frontier Dental's competitive intelligence workflow.

## Architecture Overview

```mermaid
flowchart TD
    CLI["main.py (Typer CLI)"]
    ORCH["Orchestrator<br>(sequences all stages)"]
    NAV["[1] Navigator"]
    CAT["[2] Category Scraper"]
    PROD["[3] Product Scraper + Extractor"]
    NORM["[4] Normalizer"]
    VAL["[5] Validator"]
    EXPORT["Export<br>CSV + JSON"]
    DB[("SQLite<br>job queue + products")]

    CLI --> ORCH
    ORCH --> NAV --> CAT --> PROD --> NORM --> VAL --> EXPORT
    NAV & CAT & PROD & NORM & VAL <--> DB
```

Each agent is independent and communicates only through SQLite. Killing and restarting the pipeline resumes from the last completed stage.

### Agent Details

| Stage | Agent | LLM | Tools | Responsibility |
| ----- | ----- | --- | ----- | -------------- |
| 1 | **Navigator** | No | `httpx`, `xml.etree` | Parses `catalog.xml` / `products.xml` sitemaps; filters to target categories; populates the job queue |
| 2 | **Category Scraper** | No | `httpx`, `BeautifulSoup` | Fetches category pages via HTTP; extracts product URLs and partial metadata from JSON-LD structured data |
| 3 | **Product Scraper** | No | `Playwright`, `tenacity` | Renders JS-heavy product pages with a headless browser; retries on timeout; passes raw HTML to the Extractor |
| 3 | **Extractor** | Fallback only | `BeautifulSoup` (primary), `LiteLLM` (fallback) | CSS selector extraction is primary; LLM `tool_use` activates only when selectors return empty |
| 4 | **Normalizer** | Yes | `LiteLLM` | Normalizes `unit_size` into a canonical form (e.g. `"bx/100"` → `"100/box"`) and infers `specifications` attributes from context — reasoning that `"X-small"` is a `Size`, `"#15C"` is a `Shape`, `"Latex"` is a `Material`, etc. |
| 5 | **Validator** | Yes | `LiteLLM` | Ensures specification attribute idempotency across LLM batches — detects when the same attribute was labelled differently (e.g. `"Shape"` vs `"Blade"`) and normalizes to one canonical key per attribute. Falls back to most-frequent-key selection when no API key is set. |

## Why This Approach

**Agent-based architecture** -- Each stage is an independent agent with a single responsibility. Agents communicate through a shared SQLite database rather than direct calls, which makes the pipeline resumable (kill and restart from where it left off) and observable (query the DB to see progress at any time).

**CSS selectors as primary extraction** -- For the known Magento/Hyva DOM structure, CSS selectors are fast, free, and deterministic. The LLM fallback only activates when selectors return empty results, keeping the hot path cost-free.

**Sitemap-first discovery** -- Rather than spidering the site, the pipeline reads `catalog.xml` and `products.xml` directly. This is faster, respects `robots.txt`, and gives a complete URL inventory without pagination concerns.

**LLM usage decisions** -- AI is used only where deterministic code would be brittle or unmaintainable:

| Agent | LLM at Runtime | Rationale |
| ----- | -------------- | --------- |
| Navigator | No — built with [coding agent](#how-i-use-ai-tools-in-development) | Sitemap XML has a fixed schema; `xml.etree` parsing is deterministic and free. Claude Code generated the sitemap fetch and URL filtering logic using the sitemap XML structure as ground truth. |
| Category Scraper | No — built with [coding agent](#how-i-use-ai-tools-in-development) | JSON-LD structured data is machine-readable by design; no ambiguity to resolve. Claude Code wrote the BeautifulSoup parsing using the sitemap XML as ground truth for expected page structure. |
| Product Scraper | No — built with [coding agent](#how-i-use-ai-tools-in-development) | Page rendering is a mechanical browser operation; no reasoning required. Claude Code used the sitemap XML as ground truth and identified the need for Playwright by comparing expected URLs against incomplete static HTTP responses. |
| Extractor | Fallback only | CSS selectors cover ~95% of pages; LLM only fires when the DOM returns nothing, avoiding cost on the hot path. Claude Code wrote the selector logic using `products_example_output.csv` as ground truth, verifying extracted values matched known-correct field values. |
| Normalizer | Yes | Two tasks that both require reasoning: (1) unit size strings (`"bx/100"`, `"per box of 100"`, `"2.5ml/vial"`) are too varied for regex; (2) specifications require the LLM to infer what an attribute *is* from context — e.g. recognising `"X-small"` as `Size`, `"#15C"` as `Shape`, `"Latex"` as `Material` — rather than just parsing a value |
| Validator | Yes | The Normalizer LLM is not deterministic — it may label the same attribute `"Shape"` in one batch and `"Blade"` in another. The Validator ensures idempotency: union-find detects keys that are mutually exclusive across variants of the same product (a structural signal they occupy the same attribute slot), then the LLM confirms whether they are true aliases and picks one canonical name, guaranteeing consistent keys across the full dataset |

## Usage

### Prerequisites

- Python 3.11+
- An OpenRouter API key (for LLM steps)

### Installation

```bash
# Clone the repository
git clone github.com/AlexPerrin/AI-Agent-for-Product-Scraping-and-Structured-Catalog-Extraction-POC/edit/main/README.md
cd AI-Agent-for-Product-Scraping-and-Structured-Catalog-Extraction-POC

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

# Cap the number of product pages scraped (useful for testing)
python main.py run --limit 20

# Skip browser rendering (only run discovery + category scraping)
python main.py run --skip-browser

# Export only (no scraping, just generate CSV/JSON from existing data)
python main.py run --export-only

# Check pipeline status
python main.py status

# Re-run normalizer on already-extracted data and export
python main.py normalize

# Re-run validator (spec key harmonization) on normalized data and export
python main.py validate

# Export existing data
python main.py export
```

## Sample Outputs

Full output files from a run across both target categories (64 products) are available for download:

- [products.csv](output/products.csv)
- [products.json](output/products.json)
- [output.xlsx](output/output.xlsx)

### JSON

```json
{
  "product_group_name": "Nuvo™",
  "product_name": "Nuvo vinyl gloves small 100/box",
  "brand": "Dash",
  "item_number": "4680227",
  "manufacturer_number": "NV100S",
  "category_hierarchy": ["Dental Supplies", "Dental Exam Gloves", "Vinyl gloves"],
  "product_group_url": "https://www.safcodental.com/product/nuvo-trade",
  "price": {"1": "7.49"},
  "unit_size": "100/box",
  "specifications": {"Size": "Small"},
  "availability": "In stock",
  "description": "Powder-free vinyl examination gloves.",
  "image_urls": ["https://www.safcodental.com/media/catalog/product/d/r/druii_lc.jpg"],
  "scraped_at": "2026-03-17T19:40:46.197745",
  "extraction_method": "css-selector",
  "validation_status": "valid",
  "validation_notes": null
}
```

### CSV

| product\_group\_name | product\_name | brand | item\_number | manufacturer\_number | category\_hierarchy | product\_group\_url | Quantity | price\_per\_unit | availability | group\_description | unit\_size | specifications | image\_urls | scraped\_at | extraction\_method | validation\_status | validation\_notes |
| -------------------- | ------------- | ----- | ------------ | -------------------- | ------------------- | ------------------- | -------- | ---------------- | ------------ | ------------------ | ---------- | -------------- | ----------- | ----------- | ------------------ | ------------------ | ----------------- |
| Nuvo™ | Nuvo vinyl gloves small 100/box | Dash | 4680227 | NV100S | Dental Supplies / Dental Exam Gloves / Vinyl gloves | `https://…/nuvo-trade` | 1 | $7.49 | In stock | Powder-free vinyl examination gloves. | 100/box | `{"Size": "Small"}` | `https://…/druii_lc.jpg` | 2026-03-17T19:40:46 | css-selector | valid | |

## Output Schema

One row per orderable variant (SKU). Both CSV and JSON exports use this schema:

| Field | Type | Description |
| ----- | ---- | ----------- |
| `product_group_name` | `string` | Name of the product group (parent listing shared across variants) |
| `product_name` | `string` | Full name of this specific variant |
| `brand` | `string` | Manufacturer or brand name |
| `item_number` | `string` | Safco item number (unique per variant) |
| `manufacturer_number` | `string` | Manufacturer's own part number |
| `category_hierarchy` | `string[]` / `string` | Breadcrumb path from root to leaf category (array in JSON, `/`-separated in CSV) |
| `product_group_url` | `string` | URL of the product page on safcodental.com |
| `price` | `object` / `string` | Quantity-tier pricing; keys are minimum order quantities, values are unit prices (e.g. `{"1": "7.49", "6": "6.99"}`). Flattened to `Qty 1: $7.49 \| Qty 6: $6.99` in CSV. |
| `unit_size` | `string` | Normalised pack size in canonical form (e.g. `100/box`, `2.5ml/vial`, `1`) |
| `specifications` | `object` / `string` | LLM-inferred variant attributes such as `Size`, `Material`, `Shape`, `Color`, `Dimensions`. Empty object when no distinguishing attributes exist. Serialised as a JSON string in CSV. |
| `availability` | `string` | Stock status as shown on the product page (e.g. `In stock`, `Backorder`) |
| `description` | `string` | Product description |
| `image_urls` | `string[]` / `string` | Product image URLs (array in JSON, pipe-separated in CSV) |
| `scraped_at` | `string` (ISO 8601) | Timestamp of when the product page was scraped |
| `extraction_method` | `string` | `css-selector` or `llm-fallback` — indicates which extraction path was used |
| `validation_status` | `string` | `valid` for all records after validator runs |
| `validation_notes` | `string \| null` | Additional notes from the validator, if any |

## Limitations

1. **No proxy rotation.** Requests come from a single IP. High-volume runs may trigger rate limiting.

2. **Playwright is slow.** Rendering JS-heavy Magento pages takes 2-5 seconds each vs ~100ms for static HTTP. The POC scope (two categories) is manageable; full-site crawls need distributed browser workers.

3. **LLM fallback adds cost.** If CSS selectors degrade across many pages (e.g., after a site redesign), LLM calls become expensive. The `extraction_method` metric provides early warning.

4. **No image downloading.** Image URLs are stored, not the images themselves.

5. **Category membership is sitemap-inferred.** Products are assigned to categories based on sitemap discovery. Cross-listed products only get their first category association.

6. **Single-process execution.** The pipeline runs in one process with async concurrency. Production scale requires distributed workers.

## Failure Handling

| Failure Mode | How It Is Handled |
| ------------ | ----------------- |
| HTTP 404 on category page | Job marked `skipped_404`, logged, pipeline continues |
| HTTP error / timeout | Retried 3 times with exponential backoff (1s, 2s, 4s via tenacity) |
| Playwright navigation timeout | Retried 2 times, then job marked `tier2_failed` with error details |
| CSS selectors return empty | Automatic fallback to LLM extraction |
| LLM API error (normalizer) | Logged, batch skipped, pipeline continues with next batch |
| LLM returns unparseable JSON | Logged, batch marked with defaults, pipeline continues |
| LLM rate limit | Exponential backoff retry (up to 6 attempts: 15s, 30s, 60s, 120s, 240s, 480s) |
| LLM API error (validator) | Falls back to deterministic most-frequent-key selection for that product group |
| No API key configured | LLM steps skipped gracefully with warnings; normalizer skips normalization, validator uses deterministic fallback |
| Pipeline killed mid-run | Restart picks up from last checkpoint (each job has status in SQLite) |
| Duplicate products | `UNIQUE` constraint on `safco_item_number`; upserts update existing records |

## How to Scale to Full-Site Crawling

1. **Remove category filtering** -- Set `TARGET_CATEGORIES` to all category slugs or remove the filter entirely in the Navigator agent.

2. **Distributed browser rendering** -- Replace the single Playwright process with a Playwright cluster or Browserless.io pool. The semaphore-based concurrency model already supports this pattern.

3. **Replace SQLite with PostgreSQL** -- Add proper indexing to the products database and use connection pooling (asyncpg) to support concurrent writers across distributed workers.

4. **Implement the job queue with Kafka** -- Decouple the job queue from the products database entirely. Each pipeline stage becomes a Kafka consumer group, publishing completed work as events for the next stage. This enables independent scaling of each stage, persistent replay, and dead-letter handling without coupling job state to the product store.

5. **Integrate structured logging with an observability platform** -- The pipeline already uses `structlog` with structured key-value output. Wire this into Datadog or Grafana to build dashboards and alerts for pipeline performance (throughput, latency per stage, retry rates) and data quality (LLM fallback rate, spec key corrections, extraction failures).

6. **Schedule with Airflow or cron** -- Run differential scrapes (only reprocess products whose content hash changed) on a regular schedule.

7. **Add proxy rotation** -- Configure rotating proxies and user agents to distribute request load across IPs.

8. **Increase rate limits** -- Tune `REQUEST_DELAY` and `BROWSER_CONCURRENCY` based on observed rate limiting behavior.

## How to Monitor Data Quality

1. **LLM fallback rate** -- Track the ratio of `extraction_method="llm-fallback"` records. A rising rate signals CSS selector degradation (site DOM changed). Run `python main.py status` to see current counts.

2. **Spec key harmonization counts** -- The validator logs `specs_fixed` (number of records whose spec keys were renamed for consistency). A high count after a re-run signals the normalizer is producing inconsistent keys, likely due to LLM drift.

3. **Known-page regression tests** -- Maintain a set of 10-20 known product pages with expected output. Run extraction against them after each deploy and alert if results diverge.

4. **Structured logging** -- All pipeline stages use `structlog` with structured key-value output. Ingest logs into Datadog, Grafana, or similar for dashboards and alerting.

5. **Database queries** -- The SQLite database is the source of truth. Query it directly to audit specific records, check job queue health, or investigate extraction failures:

```bash
# Count by extraction method
sqlite3 frontier_dental.db "SELECT extraction_method, COUNT(*) FROM products GROUP BY extraction_method"

# Find LLM fallback records
sqlite3 frontier_dental.db "SELECT item_number, product_name FROM products WHERE extraction_method='llm-fallback'"

# Check failed jobs
sqlite3 frontier_dental.db "SELECT url, error_msg FROM jobs WHERE status='tier2_failed'"

# Check spec key variety across a product group
sqlite3 frontier_dental.db "SELECT product_name, specifications FROM products WHERE product_group_name='Latex Examination Gloves'"
```

## How I Use AI Tools in Development

### Tooling Setup

This project was built using **Claude Code** inside VS Code as the primary coding agent. Two integrations shaped the workflow significantly:

- **Context7 MCP** -- Connected via MCP server, Context7 gives Claude Code access to live library documentation. Rather than relying on potentially stale training data, the agent fetches current docs for libraries like Playwright, LiteLLM, and httpx at the point of code generation. This was particularly useful when working with Playwright's async API, where small version-specific differences in method signatures would otherwise cause silent failures.

- **claude-devtools** -- A local repo used to trace prompt inputs, outputs, and context window contents during development. When an LLM agent (normalizer, validator) was producing unexpected output, claude-devtools made it possible to inspect exactly what prompt was sent, what the model returned, and whether context was being truncated or polluted — without guessing.

### Output-First Development for Scraping Agents

For the deterministic agents (Navigator, Category Scraper, Product Scraper, Extractor), the most effective workflow was **providing the desired output before writing the extraction code**. Before implementing the Extractor, `products_example_output.csv` was created by hand with correct field values for a small set of real products — capturing the exact item numbers, prices, unit sizes, and category hierarchies that should appear in the final dataset.

This gave the coding agent a concrete ground truth to work backwards from: instead of reasoning abstractly about what CSS selectors might exist on an unknown page, it could verify its extraction logic against known-correct values.

The main challenge was that safcodental.com uses a Magento/Hyva frontend with heavily JS-rendered product pages. Static HTTP requests returned incomplete HTML with missing prices, variant tables, and specifications. The agent needed to be directed to use **Playwright to fully render each page** before attempting CSS extraction — a non-obvious requirement that only became clear once the expected output values were available for comparison and the raw HTTP responses could be shown to visibly lack them.
