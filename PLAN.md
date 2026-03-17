# Project Plan: AI Agent for Product Scraping and Structured Catalog Extraction

## 1. Problem Definition

Frontier Dental needs to extract structured product catalog data from Safco Dental Supply
(safcodental.com) and store it in a queryable format. The business goal is to monitor
competitor pricing, maintain an up-to-date product catalog mirror, and eventually automate
catalog management workflows.

The target site has two categories in scope for this POC:
- **Sutures & Surgical Products**: https://www.safcodental.com/catalog/sutures-surgical-products
- **Dental Exam Gloves**: https://www.safcodental.com/catalog/gloves

The system must be agent-based (not a monolithic script), designed for eventual production
hardening, and use AI where it creates practical value rather than as decoration.

## 2. Site Research & Understanding

Before designing the solution, the target site was thoroughly investigated. The findings
directly shape every architectural decision.

### 2.1 Site Technology

Safco Dental runs on **Magento** with the **Hyvä theme** (Alpine.js frontend). This has
a critical implication: product detail pages are **JavaScript-rendered**. A plain HTTP
request returns mostly JS bootstrapping code with no product content. A headless browser
(Playwright) is required to obtain the rendered DOM.

### 2.2 Sitemap

The site exposes a sitemap index at `/sitemap.xml` that references several child sitemaps.
Two are relevant to this project:

| File | Contents |
|---|---|
| `catalog.xml` | 80+ category and subcategory URLs at all depth levels |
| `products.xml` | 1,000+ individual product group URLs |

**`catalog.xml` — category discovery**

This file is the authoritative list of all category and subcategory paths on the site.
It is used by the Navigator Agent to build the URL work queue for the Category Scraper.
URLs span all three depth levels described in section 2.2.

**`products.xml` — product discovery**

This file is the authoritative list of all product group URLs. Rather than discovering
products by following links on category pages, the Navigator reads this file directly.
This approach avoids any pagination concerns and is unambiguous with respect to
`robots.txt` (which only disallows query-parameter-based navigation).

**Sitemap reliability caveat**

Not all URLs listed in `catalog.xml` resolve to a valid page. Some return HTTP 404
(e.g. `/catalog/anesthetics/injectable-anesthetics/injectable-anesthetics` — a
duplicate-slug path that was likely published by mistake). The Category Scraper must
check the HTTP response status for every catalog URL and skip 404s, logging them as
`skipped_404` in the job queue rather than treating them as scrape failures.

### 2.3 URL Structure

| URL Pattern | Description |
|---|---|
| `/catalog/[category]` | Top-level category listing page |
| `/catalog/[category]/[subcategory]` | Second-level subcategory listing page |
| `/catalog/[category]/[subcategory]/[sub-subcategory]` | Third-level subcategory listing page |
| `/product/[product-slug]` | Product group detail page |

Category depth goes up to at least 3 levels. For example:
- `/catalog/restorative-and-cosmetic-dentistry` — top-level
- `/catalog/restorative-and-cosmetic-dentistry/rubber-dam` — second-level
- `/catalog/restorative-and-cosmetic-dentistry/rubber-dam/rubber-dam-clamps` — third-level

### 2.4 Category Listing Pages

Category pages (`/catalog/[name]`) serve **static HTML** that includes JSON-LD structured
data (`ItemList` schema). This data contains product name, SKU, price, availability,
image URL, and brand for every product listed on the page — no JavaScript rendering needed.

Example: `/catalog/gloves` has 6 subcategories and lists ~50 products in its JSON-LD.
Each subcategory (`/catalog/gloves/nitrile-gloves`) has its own listing with JSON-LD.

This means category pages can be scraped cheaply and quickly with plain HTTP requests.
They serve as the **discovery layer** — giving us the full list of product URLs to visit.

### 2.4 Product Detail Pages (Critical Finding)

A product detail page (e.g., `/product/3m-trade-adper-trade-scotchbond-trade-multi-purpose`)
is rendered entirely on page load. All sections are present in the DOM simultaneously —
the navigation anchors (Products, Description, Reviews, Shop by Trending Products) simply
scroll the viewport, they do not lazy-load content.

The page is structured as follows:

```
[Header Section]
  Brand name               e.g. "3M HEALTH CARE (NOW SOLVENTUM)"
  Product group name       e.g. "Adper Scotchbond Multi-Purpose"
  Price range              e.g. "From $309.99"
  Short description + "Read More"
  Hero image(s)
  Warning banners          e.g. "HAZMAT — may not ship via air"

[Products Section]
  One or more named, collapsible sub-group tables, e.g.:
  ┌── Sub-group: "Adper Scotchbond Multi-Purpose" ──────────────────┐
  │  Item #  │ Product Name │ Mfr # │ Description │ Availability │ Price │
  │  2510134 │ ...system kit│ 7540S │ System kit  │ In stock     │$553.99│
  └──────────────────────────────────────────────────────────────────┘
  ┌── Sub-group: "Multi-Purpose adhesive" ──────────────────────────┐
  │  Item #  │ Product Name │ Mfr # │ Description │ Availability │ Price │
  │  2510137 │ ...refill 8ml│ 7543  │ Adhesive... │ In stock     │$309.99│
  └──────────────────────────────────────────────────────────────────┘
  (N more sub-groups may follow)

[Description Section]
  Full product description text

[Reviews Section]
  Customer ratings and reviews (not in scope)

[Shop by Trending Products Section]
  Unrelated/merchandised products (not in scope)
```

### 2.5 Data Hierarchy

One product URL maps to a 3-level hierarchy:

```
Product Group  (1 URL)
  └── Sub-group  (1..N named table sections on the Products section)
        └── Variant  (1..N orderable SKU rows per sub-group)
```

The **variant row** is the atomic unit of the catalog. It represents one orderable item
with a unique Safco Item Number, its own price, availability, and unit size.

### 2.6 robots.txt Constraints

The robots.txt disallows query parameters for filtering, sorting, and pagination
(`?price=`, `?order=`, `?dir=`, `?limit=`). Rather than relying on paginating category
pages, we use `products.xml` from the sitemap as the authoritative source of product URLs.
This avoids any robots.txt ambiguity and is more reliable.

## 3. Assumptions

1. **Scraping is permitted for this POC.** The robots.txt does not disallow `/catalog/`
   or `/product/` paths. This is a competitive intelligence exercise consistent with
   normal market research practices.

2. **All content is publicly visible.** No login is required to view product data,
   prices, or availability on the target categories.

3. **The two target categories are the scope boundary.** The pipeline is designed to
   handle the full site but will only be executed against sutures-surgical-products
   and gloves for this POC.

4. **Product URLs from `products.xml` are the ground truth.** We do not attempt to
   discover products by spidering links. The sitemap is authoritative.

5. **One row per orderable variant** is the correct output granularity. A product group
   with 3 sub-groups and 4 variants each produces 12 output rows, all sharing the same
   product_group_url and group-level fields.

6. **Price captures all quantity breaks.** Product rows may show a price table with
   multiple quantity tiers (e.g. Qty 1 = $36.99, Qty 6 = $32.99). All tiers are
   captured as a dict keyed by integer quantity. Single-price rows are stored as
   `{1: <price>}`.

7. **The site structure is stable enough for a CSS selector primary path.** We expect
   selectors to work for the majority of pages with LLM fallback handling edge cases
   and future layout drift.

## 4. Architecture Overview

The system is a config-driven ETL pipeline composed of discrete agents, each with a
single responsibility. Agents communicate through a shared SQLite job queue rather than
direct calls, making the pipeline resumable and observable.

```
┌──────────────────────────────────────────────────────────────────┐
│                        ORCHESTRATOR                              │
│  Reads config → sequences agents → reports summary              │
└────────────────────────────┬─────────────────────────────────────┘
                             │
              ┌──────────────▼──────────────┐
              │       NAVIGATOR AGENT        │  ◆ NO LLM
              │  Sitemap → URL work queue    │  XML parsing only
              └──────────────┬──────────────┘
                             │ writes URLs to SQLite queue
              ┌──────────────▼──────────────┐
              │    CATEGORY SCRAPER          │  ◆ NO LLM
              │  HTTP + JSON-LD extraction   │  static HTML, fast path
              └──────────────┬──────────────┘
                             │ product group URLs confirmed
              ┌──────────────▼──────────────┐
              │    PRODUCT SCRAPER           │  ◆ NO LLM
              │  Playwright renders pages    │  browser only
              └──────────────┬──────────────┘
                             │ raw HTML per page
              ┌──────────────▼──────────────┐
              │      EXTRACTOR AGENT         │  ◈ HYBRID
              │  CSS selectors (primary)     │  LLM fallback when
              │  LLM extraction (fallback)   │  selectors fail
              └──────────────┬──────────────┘
                             │ raw product dicts
              ┌──────────────▼──────────────┐
              │     NORMALIZER AGENT         │  ● LLM
              │  unit_size + brand cleanup   │  batch, cheap model
              └──────────────┬──────────────┘
                             │ normalized records
              ┌──────────────▼──────────────┐
              │      VALIDATOR AGENT         │  ● LLM
              │  semantic quality check      │  reasoning model
              └──────────────┬──────────────┘
                             │ validated records
              ┌──────────────▼──────────────┐
              │         STORAGE              │  ◆ NO LLM
              │  SQLite → CSV / JSON export  │
              └─────────────────────────────┘

  ◆ NO LLM — deterministic code only
  ◈ HYBRID  — CSS selectors primary, LLM fallback
  ● LLM     — LiteLLM call on every run
```

## 5. Agent Specifications

### 5.1 Navigator Agent — No LLM

**Responsibility:** Discover all product and category URLs within the target scope and
populate the job queue.

**How it works:**

1. Fetches `https://www.safcodental.com/catalog.xml` with `httpx`.
2. Parses the XML sitemap to extract all `/catalog/` URLs.
3. Filters to only URLs that fall under the configured target categories
   (`sutures-surgical-products`, `gloves`) — including subcategory paths.
4. Fetches `https://www.safcodental.com/products.xml` and parses it.
5. For each product URL in `products.xml`, determines which target category it belongs
   to by cross-referencing the category listing pages fetched in the Category Scraper
   step (this avoids having to load each product page just to check its breadcrumb).
6. Writes discovered URLs to the SQLite `jobs` table with status `pending`.

**Resumability:** If jobs already exist in the queue for a run, the Navigator skips
re-inserting them. A `--reset` flag truncates the table and starts fresh.

**Output:** Populated `jobs` table with category URLs and product group URLs.

---

### 5.2 Category Scraper — No LLM

**Responsibility:** Fetch category and subcategory listing pages, extract product URLs
and lightweight product metadata from JSON-LD. No browser.

**How it works:**

1. Pulls all category/subcategory URLs from the job queue with status `pending`.
2. For each URL, makes a plain `httpx` GET request (fast, ~100ms/page).
3. Parses the HTML with BeautifulSoup and extracts the `<script type="application/ld+json">`
   block containing the `ItemList` schema.
4. From the ItemList, extracts for each listed product:
   - `product_group_url`
   - `product_group_name`
   - `brand`
   - `price` (listing price, may differ from variant price on detail page)
   - `availability`
   - `image_url`
   - `sku` (the group-level SKU visible on listing pages)
5. Writes these as partial records to the `products` table (marked `tier1_complete`).
6. Adds each `product_group_url` to the job queue for Tier 2 processing.
7. Extracts the `category_hierarchy` from the page breadcrumb.

**Rate limiting:** Configurable delay between requests (default 1s). Uses `tenacity`
for retries with exponential backoff on HTTP errors.

**Output:** Partial product records in DB + product URLs queued for Tier 2.

---

### 5.3 Product Scraper — No LLM

**Responsibility:** Render each product detail page using a headless browser and return
the full DOM to the Extractor Agent.

**How it works:**

1. Pulls product group URLs from the job queue with status `tier1_complete`.
2. Launches a Playwright Chromium instance (async, configurable concurrency via semaphore).
3. For each URL:
   - Navigates to the page.
   - Waits for the Products section table(s) to appear in the DOM
     (`networkidle` + explicit selector wait for the first table row).
   - Extracts the full `innerHTML` of the main content area.
   - Passes the HTML to the Extractor Agent.
4. Handles timeouts and navigation errors with retry logic.
5. Marks job as `tier2_complete` or `tier2_failed` with error details.

**Why Playwright over a managed service (Firecrawl, Cloudflare Browser Rendering):**
Playwright gives full control over concurrency, headers, wait conditions, and retry
behavior at no per-request cost. It is the right foundation for a system we own and
can scale. Managed services add per-request cost, external rate limits, and a dependency
on third-party availability.

---

### 5.4 Extractor Agent — Hybrid (CSS selectors primary, LLM fallback)

**Responsibility:** Parse the rendered product page HTML and extract all structured
variant data. This is where AI creates the most practical value.

**Two-path design:**

#### Primary path: CSS Selectors

For the known Hyvä/Magento DOM structure, CSS selectors are fast and free:

```
Products section
  └── Each sub-group container  [data-content-type="block"] or similar
        ├── Sub-group heading   h3, h4, or labelled div
        └── Table rows          tr elements within the sub-group
              ├── Item #        td:nth-child(1)
              ├── Product Name  td:nth-child(2)
              ├── Mfr #         td:nth-child(3)
              ├── Description   td:nth-child(4)
              ├── Availability  td:nth-child(5)
              └── Price table   qty/price rows within the variant row
```

The selector path runs first. If it extracts at least one complete variant record
(Item # + name + price), it is considered successful and the LLM path is skipped.

#### Fallback path: LLM Extraction

When selectors return empty or incomplete data (DOM structure changed, unusual layout,
collapsible section not expanded), the agent falls back to an LLM.

**How the LLM call works:**

The agent sends a structured prompt via **LiteLLM** with **tool use** (structured
output). The model is configurable per agent in `settings.py` — the default for the
Extractor is a capable reasoning model (e.g. `openrouter/anthropic/claude-sonnet-4-5`).
The tool schema defines exactly the shape of data we want:

```python
tools = [{
    "name": "extract_product_variants",
    "description": "Extract all product variants from a Safco Dental product page",
    "input_schema": {
        "type": "object",
        "properties": {
            "product_group_name": {"type": "string"},
            "brand": {"type": "string"},
            "group_description": {"type": "string"},
            "image_urls": {"type": "array", "items": {"type": "string"}},
            "subgroups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subgroup_name": {"type": "string"},
                        "variants": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "safco_item_number": {"type": "string"},
                                    "manufacturer_part_number": {"type": "string"},
                                    "variant_name": {"type": "string"},
                                    "unit_size": {"type": "string"},
                                    "availability": {"type": "string"},
                                    "price": {"type": "number"}
                                },
                                "required": ["safco_item_number", "variant_name"]
                            }
                        }
                    }
                }
            }
        }
    }
}]
```

The HTML passed to the LLM is **preprocessed**: scripts, style blocks, nav, footer, and
the Reviews and Shop by Trending Products sections are stripped before sending. This
reduces token count significantly and focuses the model on the relevant DOM.

The system prompt instructs the model:
> "You are extracting structured product data from a dental supply website. The page
> contains one product group with one or more named sub-group tables in the Products
> section. Each table row is an individually orderable SKU with its own item number,
> manufacturer part number, description, availability, and price. Extract every variant
> from every sub-group table."

The model responds with a `tool_use` block whose `input` matches our schema exactly. No
string parsing required — the structured output is directly deserializable into our
Pydantic models.

**The extraction_method field** on every output record records which path was used
(`"css-selector"` or `"llm-fallback"`), enabling monitoring of selector health over time.
A rising LLM-fallback rate is an early signal that the site's DOM has changed.

---

### 5.5 Normalizer Agent — LLM

**Responsibility:** Normalize inconsistently formatted field values across records into
a canonical schema. This is a targeted, high-value LLM use case.

**Why LLM over regex:** Unit sizes on dental supply sites are highly inconsistent
across manufacturers and categories:

| Raw value | Normalized |
|---|---|
| `"bx/100"` | `"100/box"` |
| `"per box of 100"` | `"100/box"` |
| `"Box 100ct"` | `"100/box"` |
| `"1 vial, 2.5 ml"` | `"2.5ml/vial"` |
| `"3M HEALTH CARE (NOW SOLVENTUM)"` | `"3M / Solventum"` |

Building a comprehensive regex for all combinations across 1,000+ products across
80+ categories is brittle and essentially unmaintainable. Claude normalizes these in
batch with awareness of dental industry conventions.

**Availability is not normalized by LLM.** The availability field appears to be a
static enum sourced from the Magento catalog. Observed values are:

| Raw site value | Stored as-is |
|---|---|
| `"In Stock"` | `"In Stock"` |
| `"Special Order"` | `"Special Order"` |
| `"Backorder"` | `"Backorder"` |
| `"Direct from Manufacturer"` | `"Direct from Manufacturer"` |
| `"Out of Stock"` | `"Out of Stock"` *(likely, unconfirmed)* |

Because the value set is small and consistent, a simple lookup table (dict or Enum)
maps raw values to canonical snake_case strings at parse time — no LLM call needed.
Any value not in the lookup is stored verbatim and flagged for review.

**How it works:**

The Normalizer runs after all Extractor results are written to the database. It pulls
records in batches of 50 (configurable) and sends a single Claude API call per batch:

```python
prompt = f"""
Normalize the following product records from a dental supply catalog.
For each record, return normalized values for: unit_size, brand.

Rules:
- unit_size: canonical form is "{quantity}/{unit}" e.g. "100/box", "1/vial", "4/pkg"
- brand: remove legal suffixes like "(NOW SOLVENTUM)", expand common abbreviations

Records (JSON):
{json.dumps(batch)}

Return a JSON array with one object per record containing: id, unit_size, brand.
"""
```

The LLM returns a JSON array. The Normalizer applies the normalized values back to the
database records in a single UPDATE batch.

**Cost control:** Normalization is a straightforward transformation task — it is
assigned a cheaper, faster model in config (e.g. `openrouter/anthropic/claude-haiku-4-5`).
Each agent's model is independently configurable in `settings.py`.

---

### 5.6 Validator Agent — LLM

**Responsibility:** Perform semantic quality validation on normalized records — catching
errors that rules and schemas cannot.

**Why LLM over schema validation:** Pydantic handles structural validation (types,
required fields). The Validator handles *semantic* validation — errors that are
structurally valid but logically wrong:

- A Safco Item Number that looks like a price (`"$36.99"`)
- A price of `$0.01` for a surgical kit (likely a scrape error)
- A brand of `"N/A"` written into the product name field
- An availability of `"In stock"` with a price of `$0.00`
- A variant name identical to the product group name (extraction probably missed specifics)
- Missing price on a non-special-order item

**How it works:**

The Validator runs after normalization on batches of 20 records:

```python
system_prompt = """
You are a data quality analyst for a dental supply product catalog.
Review each product record for semantic errors, implausible values, and signs
of extraction failure. Be concise. Flag real problems, not stylistic ones.
"""

user_prompt = f"""
Validate these product records. For each, return:
- validation_status: "valid", "warning", or "invalid"
- validation_notes: null if valid, brief issue description otherwise

Records:
{json.dumps(batch)}
"""
```

Results are written back to `validation_status` and `validation_notes` fields.
Invalid records are excluded from the final export. Warning records are included
but flagged. This gives a clean output dataset with a traceable audit trail.

## 6. Tech Stack

| Layer | Library | Rationale |
|---|---|---|
| HTTP (static pages) | `httpx` (async) | Async-native, connection pooling, timeout control |
| Browser rendering | `playwright` (async) | Full JS rendering, free, production-grade |
| HTML parsing | `beautifulsoup4` + `lxml` | JSON-LD extraction and DOM preprocessing |
| LLM agents | `litellm` | Unified LLM interface; model swappable per agent via config using `openrouter/` prefix |
| Data models | `pydantic v2` | Schema enforcement and serialization |
| Storage | `aiosqlite` | Async SQLite for job queue and product store |
| Retry logic | `tenacity` | Exponential backoff with jitter on HTTP/browser errors |
| Config | `pydantic-settings` + `.env` | Type-safe config, secrets via environment variables |
| Logging | `structlog` | Structured JSON logs for observability |
| CLI | `typer` | Clean entrypoint for running the pipeline |
| Export | stdlib `csv` + `json` | CSV and JSON output |

**Python version:** 3.11+

### LLM Configuration

All LLM calls are made through [LiteLLM](https://docs.litellm.ai), which provides a
unified interface over 100+ model providers. Switching providers or models requires
only a config change — no agent code changes. OpenRouter is used as the routing layer,
so any model available there is accessible via the `openrouter/` prefix.

Each AI agent has its own model setting in `settings.py`:

```python
class Settings(BaseSettings):
    openrouter_api_key: str             # OPENROUTER_API_KEY in .env

    # Per-agent model selection — any LiteLLM-supported model string
    extractor_model: str  = "openrouter/anthropic/claude-sonnet-4-5"   # high accuracy needed
    normalizer_model: str = "openrouter/anthropic/claude-haiku-4-5"    # fast, cheap, simple task
    validator_model: str  = "openrouter/anthropic/claude-sonnet-4-5"   # semantic reasoning needed
```

LiteLLM reads `OPENROUTER_API_KEY` from the environment automatically. Each agent
calls `litellm.acompletion(model=settings.<agent>_model, ...)`, keeping all call sites
model-agnostic. Swapping to a different provider (e.g. `openai/gpt-4o` or
`google/gemini-2.0-flash`) requires changing only the model string in `.env`.

## 7. Output Schema

One row per orderable variant. CSV and JSON exports both use this flat schema.

```python
class ProductVariant(BaseModel):
    # Product name
    product_group_name: str             # Product group name (page header)
    variant_name: str                   # Individual orderable SKU name

    # Brand / manufacturer
    brand: str | None                   # Normalized e.g. "3M / Solventum"

    # SKU / item number / product code
    safco_item_number: str              # Safco's catalog number — primary key
    manufacturer_part_number: str | None

    # Category hierarchy
    category_hierarchy: list[str]       # e.g. ["Dental Supplies", "Bonding agents"]

    # Product URL
    product_group_url: str

    # Price — all quantities are in USD
    price: dict[int, Decimal]           # e.g. {1: 36.99, 6: 32.99, 12: 29.99}; empty dict if price unavailable

    # Unit / pack size
    unit_size: str | None               # Normalized e.g. "100/box"

    # Availability / stock indicator
    availability: str | None            # Normalized e.g. "in_stock"

    # Description
    group_description: str | None

    # Specifications / attributes
    subgroup_name: str | None           # Named table section within Products tab; None if only one table

    # Image URL(s)
    image_urls: list[str]

    # Alternative products
    alternative_products: list[str]     # Product group URLs of related/alternative items

    # Pipeline metadata
    scraped_at: datetime
    extraction_method: str              # "css-selector" | "llm-fallback"
    validation_status: str              # "valid" | "warning" | "invalid"
    validation_notes: str | None
```

## 8. Project Structure

```
frontier-dental-poc/
├── config/
│   └── settings.py          # Pydantic settings loaded from .env
├── agents/
│   ├── navigator.py          # Sitemap parsing, URL queue population
│   ├── extractor.py          # CSS selector + LLM extraction logic
│   ├── normalizer.py         # LLM batch normalization
│   └── validator.py          # LLM semantic validation
├── scraper/
│   ├── http_client.py        # httpx session with rate limiting + retries
│   └── browser.py            # Playwright page rendering
├── models/
│   └── product.py            # Pydantic ProductVariant schema
├── storage/
│   ├── database.py           # SQLite schema, job queue, product CRUD
│   └── export.py             # CSV / JSON export
├── pipeline/
│   └── orchestrator.py       # Top-level ETL sequencing and checkpointing
├── main.py                   # CLI entrypoint (typer)
├── .env.example              # Required environment variables
├── requirements.txt
└── README.md
```

## 9. Pipeline Execution Flow

```
Step 1: Navigator Agent
  ├── Parse catalog.xml → collect subcategory URLs for target categories
  ├── Parse products.xml → collect product URLs
  └── Write all URLs to jobs table with status = "pending"

Step 2: Category Scraper
  ├── For each category/subcategory URL (status=pending)
  │   ├── httpx GET → parse JSON-LD ItemList
  │   ├── Write partial product records to products table
  │   └── Mark job status = "tier1_complete"
  └── Enqueue product group URLs for Tier 2

Step 3: Product Scraper + Extractor Agent
  ├── For each product URL (status=tier1_complete)
  │   ├── Playwright renders page
  │   ├── Extractor attempts CSS selector path
  │   ├── If selectors fail → Extractor calls LLM via LiteLLM (fallback)
  │   ├── Write extracted variants to products table
  │   └── Mark job status = "tier2_complete" or "tier2_failed"
  └── (Concurrent up to configured semaphore limit, default 3 pages)

Step 4: Normalizer Agent
  ├── Pull all tier2_complete records in batches of 50
  ├── Call LLM via LiteLLM (model: settings.normalizer_model) to normalize unit_size, brand
  └── Write normalized values back to DB

Step 5: Validator Agent
  ├── Pull all normalized records in batches of 20
  ├── Call LLM via LiteLLM (model: settings.validator_model) for semantic validation
  └── Write validation_status + validation_notes to DB

Step 6: Export
  ├── Query all records where validation_status in ("valid", "warning")
  ├── Write output/products.csv
  ├── Write output/products.json
  └── Print summary: total records, valid/warning/invalid counts, LLM fallback rate
```

**Resumability:** Every step reads and writes job status in SQLite before and after
processing. Killing and restarting the pipeline continues from where it left off.
The `--reset` CLI flag clears all state for a fresh run.

---

## 10. Production Hardening Path

| Concern | POC Approach | Production Upgrade |
|---|---|---|
| Rate limiting | `asyncio.Semaphore` + fixed delay between requests | Token bucket / adaptive rate limiting respecting `Retry-After` |
| Retries | `tenacity` (3 attempts, exponential backoff) | Circuit breaker, dead-letter queue for permanent failures |
| Browser scaling | Single Playwright process, semaphore-limited concurrency | Playwright cluster or Browserless.io for distributed rendering |
| Job queue | SQLite `jobs` table | Redis + ARQ or Celery for distributed workers |
| Storage | SQLite | PostgreSQL with proper indexing on SKU, category, scraped_at |
| Secrets | `.env` file | AWS Secrets Manager or HashiCorp Vault |
| Observability | `structlog` JSON to stdout | Ingest logs into Datadog/Grafana; alert on LLM fallback rate spikes |
| Scheduling | Manual CLI execution | Apache Airflow DAG or GitHub Actions cron |
| Deduplication | UNIQUE constraint on safco_item_number in SQLite | Hash-based change detection — only reprocess records where content hash changed |
| Selector health | extraction_method field logged | Automated test suite: run against 10 known pages after each deploy, alert if CSS selector success rate drops below threshold |
| Anti-bot | None | Rotating user agents, configurable request headers, optional proxy pool |
| Config | `.env` file | Environment-specific config files (dev/staging/prod) |

---

## 11. Where AI Adds Value vs. Where It Does Not

This table is the honest answer to "use AI where it creates practical value."

| Task | AI Used? | Reasoning |
|---|---|---|
| Sitemap XML parsing | No | Deterministic XML — code is faster, cheaper, correct |
| JSON-LD extraction from category pages | No | Structured data with a fixed schema |
| CSS selector extraction from product pages | No | Fast, free, works for the majority of pages |
| Extraction from irregular/changed page layouts | **Yes** | CSS selectors break silently; LLM reads layout intent and extracts correctly despite DOM changes. This is the core resilience mechanism. |
| Unit size normalization | **Yes** | Hundreds of manufacturer-specific format variations; regex is unmaintainable at scale |
| Availability string normalization | **No** | Appears to be a static Magento enum (`"In Stock"`, `"Special Order"`, `"Backorder"`, `"Direct from Manufacturer"`, `"Out of Stock"`). A lookup table at parse time is sufficient; LLM adds no value here. |
| Brand name normalization | **Yes** | Acquisitions and rebrands (e.g., "3M HEALTH CARE (NOW SOLVENTUM)") require semantic understanding |
| Semantic data validation | **Yes** | Structural validation (Pydantic) catches type errors; LLM catches logical errors like a price of $0.01 for a surgical kit |
| Pagination / retry / dedup logic | No | Purely algorithmic |
| Job queue management | No | Purely algorithmic |

**Summary:** LLM is used at three points — extraction fallback, normalization, and
validation. The hot path (majority of records) uses no LLM. LLM cost is bounded and
predictable.

---

## 12. Known Limitations of the POC

1. **No proxy rotation.** The scraper makes requests from a single IP. High-volume
   runs may trigger rate limiting or IP blocking from the target site.

2. **Playwright is slow.** Rendering a JS-heavy Magento page takes 2–5 seconds per
   page vs. ~100ms for static HTTP. The POC runs a small number of pages; full-site
   crawls need distributed browser workers.

3. **LLM fallback adds latency and cost.** If CSS selectors degrade across many pages
   (e.g., after a site redesign), LLM calls on every page become expensive. The
   extraction_method metric is the early warning signal.

4. **Quantity-break pricing is captured in full.** The `price` field is a
   `dict[int, Decimal]` storing all quantity tiers (e.g. `{1: 36.99, 6: 32.99}`).
   Single-price rows are stored as `{1: <price>}`. Pages where no price is visible
   store an empty dict.

5. **No image downloading.** We store image URLs, not the images themselves.

6. **Category membership inferred from sitemap.** Products are assigned to the target
   category based on their URL appearing in the sitemap during Navigator discovery.
   If a product appears in multiple categories, only the first association is captured.
