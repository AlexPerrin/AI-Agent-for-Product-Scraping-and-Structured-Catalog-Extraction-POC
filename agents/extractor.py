from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog
from bs4 import BeautifulSoup, Tag

from config.settings import Settings

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Tool schema for LLM fallback extraction
# ---------------------------------------------------------------------------

EXTRACT_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_product_variants",
        "description": "Extract all product variants from a Safco Dental product page",
        "parameters": {
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
                                        "item_number": {"type": "string"},
                                        "manufacturer_number": {"type": "string"},
                                        "product_name": {"type": "string"},
                                        "unit_size": {"type": "string"},
                                        "availability": {"type": "string"},
                                        "price": {"type": "number"},
                                    },
                                    "required": ["item_number", "product_name"],
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}

SYSTEM_PROMPT = (
    "You are extracting structured product data from a dental supply website. "
    "The page contains one product group with one or more named sub-group tables "
    "in the Products section. Each table row is an individually orderable SKU with "
    "its own item number, manufacturer part number, description, availability, and "
    "price. Extract every variant from every sub-group table."
)


class ExtractorAgent:
    """Extracts product variant data from rendered product page HTML.

    Uses a two-path strategy:
      1. Primary: CSS selectors (fast, free)
      2. Fallback: LLM via LiteLLM tool_use (handles unusual layouts)
    """

    async def extract(
        self,
        url: str,
        html: str,
        settings: Settings,
    ) -> list[dict[str, Any]]:
        """Extract product variants from *html*. Returns a list of flat dicts."""

        # -- Primary path: live-DOM CSS-selector data embedded by browser.py --
        live_results = self._extract_live_dom(html, url)
        if live_results:
            log.info("extractor_css_success", url=url, variant_count=len(live_results))
            return live_results

        # -- Fallback: static CSS selectors on serialized HTML --
        css_results = self._extract_css(html, url)
        if css_results:
            log.info("extractor_css_fallback_success", url=url, variant_count=len(css_results))
            return css_results

        # Distinguish catalog pages (no product data expected) from product pages
        # where extraction failed unexpectedly.
        if "/catalog/" in url:
            log.info("extractor_skip_catalog_url", url=url)
        else:
            log.warning("extractor_css_empty_no_table", url=url)
        return []

    # ------------------------------------------------------------------
    # PRIMARY: live-DOM CSS-selector extraction (via Playwright evaluate)
    # ------------------------------------------------------------------

    def _extract_live_dom(self, html: str, url: str) -> list[dict[str, Any]]:
        """Extract from the JSON blob embedded by browser.py.

        The browser runs CSS-selector queries against the live DOM (capturing
        Alpine.js x-text mutations) and embeds the result as a JSON comment.
        """
        m = re.search(r"<!-- LIVE_DOM_DATA: (.+?) -->", html, re.DOTALL)
        if not m:
            return []

        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            log.warning("extractor_live_dom_json_error", url=url, error=str(exc))
            return []

        variants_raw: list[dict] = data.get("variants_raw", [])
        product_group_name = data.get("product_group_name", "")
        brand = data.get("brand")
        group_description = data.get("group_description")
        image_urls = data.get("image_urls", [])

        # Enrich from serialized HTML if JS missed brand/description
        if not brand or not group_description or not product_group_name:
            soup = BeautifulSoup(html, "lxml")
            if not brand:
                brand = self._get_brand(soup)
            if not group_description:
                group_description = self._get_description(soup)
            if not product_group_name:
                product_group_name = self._get_group_name(soup)
            if not image_urls:
                image_urls = self._get_image_urls(soup)

        # No product table — create a single stub from page-level data if we have
        # at least a name and a "From" price (out-of-stock / no-table products).
        if not variants_raw:
            from_price = data.get("from_price")
            if product_group_name and from_price:
                slug = url.rstrip("/").split("/")[-1]
                price_dict = {"": from_price} if from_price else {}
                log.info("extractor_no_table_stub", url=url, from_price=from_price)
                return [{
                    "product_group_name": product_group_name,
                    "product_name": product_group_name,
                    "brand": brand,
                    "item_number": slug,
                    "manufacturer_number": None,
                    "product_group_url": url,
                    "price": price_dict,
                    "description": None,
                    "unit_size": None,
                    "availability": "Out of Stock",
                    "group_description": group_description,
                    "subgroup_name": None,
                    "image_urls": image_urls,
                    "alternative_products": [],
                    "extraction_method": "css-selector",
                    "validation_status": "pending",
                }]
            return []

        variants: list[dict[str, Any]] = []
        for raw in variants_raw:
            item_num_raw = raw.get("Item #", "")
            item_num = item_num_raw.split("\n")[0].strip()
            if not item_num:
                continue

            product_name = raw.get("Product Name", "").strip() or item_num
            mfr = raw.get("Mfr #", "").strip() or None
            raw_desc = raw.get("Description", "").strip() or None
            availability = raw.get("Stock Availability", "").strip() or None

            # Parse qty-break price string: "Qty\nPrice\n1\n$19.49\n6\n$17.99"
            price_dict: dict[int, str] = {}
            price_text = raw.get("Price", "")
            lines = [ln.strip() for ln in price_text.split("\n") if ln.strip()]
            i = 0
            while i < len(lines) - 1:
                qty_match = re.match(r"^(\d+)$", lines[i])
                price_match = re.search(r"([\d,]+\.?\d*)", lines[i + 1])
                if qty_match and price_match:
                    price_dict[int(qty_match.group(1))] = price_match.group(1).replace(",", "")
                    i += 2
                else:
                    i += 1
            if not price_dict:
                m2 = re.search(r"\$([\d,]+\.\d{2})", price_text)
                if m2:
                    price_dict[1] = m2.group(1).replace(",", "")

            variants.append({
                "product_group_name": product_group_name,
                "product_name": product_name,
                "brand": brand,
                "item_number": item_num,
                "manufacturer_number": mfr,
                "product_group_url": url,
                "price": price_dict,
                "description": raw_desc,
                "unit_size": raw_desc,
                "availability": availability,
                "group_description": group_description,
                "subgroup_name": raw.get("subgroup_name"),
                "image_urls": image_urls,
                "alternative_products": [],
                "extraction_method": "css-selector",
                "validation_status": "pending",
            })

        return variants

    # ------------------------------------------------------------------
    # SECONDARY: static CSS selector extraction (serialized HTML fallback)
    # ------------------------------------------------------------------

    def _extract_css(self, html: str, url: str) -> list[dict[str, Any]]:
        """Attempt to extract product variants using CSS selectors.

        Safco Dental uses a Hyva/Alpine.js theme. After the page is scrolled
        (handled in browser.py), the products panel renders as:

            #personalization-panel
              div.group-container          ← one per sub-group
                strong[x-text]            ← sub-group name (if > 1 group)
                div.grouped-items         ← one per orderable SKU
                  [data-attr="Item #"]    ← SKU / Safco item number
                  [data-attr="Product Name"]
                  [data-attr="Mfr #"]
                  [data-attr="Description"]
                  [data-attr="Stock Availability"]
                  [data-attr="Price"]     ← may appear twice; last has prices
        """
        soup = BeautifulSoup(html, "lxml")
        variants: list[dict[str, Any]] = []

        product_group_name = self._get_group_name(soup)
        brand = self._get_brand(soup)
        group_description = self._get_description(soup)
        image_urls = self._get_image_urls(soup)

        panel = soup.find(id="personalization-panel")
        if not panel:
            # Fall back to any div containing grouped-items
            panel = soup

        groups = panel.find_all("div", class_="group-container")
        if not groups:
            # No sub-groups — treat whole panel as one group
            rows = panel.find_all("div", class_="grouped-items")
            groups_data = [(None, rows)]
        else:
            groups_data = []
            for g in groups:
                # Sub-group heading is in a <strong> rendered by x-text="itemgroup"
                heading_el = g.find("strong", attrs={"x-text": True})
                subgroup_name = heading_el.get_text(strip=True) if heading_el else None
                if subgroup_name == "0" or subgroup_name == "":
                    subgroup_name = None
                rows = g.find_all("div", class_="grouped-items")
                groups_data.append((subgroup_name, rows))

        for subgroup_name, rows in groups_data:
            for row_el in rows:
                variant = self._parse_grouped_item_row(row_el)
                if not variant.get("item_number"):
                    continue
                variant.update({
                    "product_group_name": product_group_name,
                    "brand": brand,
                    "group_description": group_description,
                    "subgroup_name": subgroup_name,
                    "image_urls": image_urls,
                    "product_group_url": url,
                    "alternative_products": [],
                    "extraction_method": "css-selector",
                    "validation_status": "pending",
                })
                variants.append(variant)

        return variants

    def _parse_grouped_item_row(self, row_el: Any) -> dict[str, Any]:
        """Extract fields from a single div.grouped-items element."""
        result: dict[str, Any] = {}

        # Collect all [data-attr] spans; some attrs appear twice (Price, Qty)
        attr_spans: dict[str, list] = {}
        for span in row_el.find_all(attrs={"data-attr": True}):
            key = span["data-attr"]
            attr_spans.setdefault(key, []).append(span)

        # Item # — strip label suffixes like "Promo", "New!"
        item_el = (attr_spans.get("Item #") or [None])[0]
        if item_el:
            sku_span = item_el.find("span", class_="product-item-sku")
            if sku_span:
                result["item_number"] = sku_span.get_text(strip=True)
            else:
                raw = item_el.get_text(separator="\n", strip=True)
                result["item_number"] = raw.split("\n")[0].strip()

        name_el = (attr_spans.get("Product Name") or [None])[0]
        if name_el:
            result["product_name"] = name_el.get_text(strip=True)

        mfr_el = (attr_spans.get("Mfr #") or [None])[0]
        if mfr_el:
            result["manufacturer_number"] = mfr_el.get_text(strip=True) or None

        desc_el = (attr_spans.get("Description") or [None])[0]
        if desc_el:
            raw_desc = desc_el.get_text(strip=True) or None
            result["description"] = raw_desc
            result["unit_size"] = raw_desc

        avail_el = (attr_spans.get("Stock Availability") or [None])[0]
        if avail_el:
            result["availability"] = avail_el.get_text(strip=True) or None

        # Price — there are two Price spans; the last one has the actual prices
        price_spans = attr_spans.get("Price", [])
        price_dict: dict[int, str] = {}
        for price_span in reversed(price_spans):
            text = price_span.get_text(separator="\n", strip=True)
            # Pattern: "Qty\nPrice\n1\n$19.49\n6\n$17.99"
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            i = 0
            while i < len(lines) - 1:
                qty_str = lines[i]
                price_str = lines[i + 1]
                qty_match = re.match(r"^(\d+)$", qty_str)
                price_match = re.search(r"([\d,]+\.?\d*)", price_str)
                if qty_match and price_match:
                    price_dict[int(qty_match.group(1))] = price_match.group(1).replace(",", "")
                    i += 2
                else:
                    i += 1
            if price_dict:
                break

        # Fallback: any $X.XX pattern in the row
        if not price_dict:
            for m in re.finditer(r"\$([\d,]+\.\d{2})", row_el.get_text()):
                price_dict[1] = m.group(1).replace(",", "")
                break

        result["price"] = price_dict
        return result

    def _get_group_name(self, soup: BeautifulSoup) -> str:
        """Extract product group name from h1 or page title."""
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        title = soup.find("title")
        if title:
            return title.get_text(strip=True).split("|")[0].strip()
        return ""

    def _get_brand(self, soup: BeautifulSoup) -> str | None:
        """Extract brand name.

        On Safco Dental (Hyva theme), the brand sits in a div.pdp-brand-review
        directly above the h1.  Multiple selectors tried in priority order.
        """
        # Primary: Safco Dental Hyva theme — brand link above product title
        pdp_brand = soup.find(class_="pdp-brand-review")
        if pdp_brand:
            a = pdp_brand.find("a")
            if a:
                text = a.get_text(strip=True)
                if text:
                    return text

        # Fallback selectors for other Magento/Hyva layouts
        for sel in [
            {"class_": "product-brand"},
            {"class_": "product__brand"},
            {"class_": "brand-name"},
            {"class_": "manufacturer-name"},
            {"attrs": {"data-ui-id": "product-brand"}},
            {"attrs": {"itemprop": "brand"}},
        ]:
            el = soup.find(["span", "div", "a", "p", "strong"], **sel)
            if el:
                name_el = el.find(attrs={"itemprop": "name"})
                if name_el:
                    return name_el.get_text(strip=True)
                text = el.get_text(strip=True)
                if text:
                    return text

        return None

    def _get_description(self, soup: BeautifulSoup) -> str | None:
        """Extract product group description."""
        for sel in [
            {"class_": "product-description"},
            {"attrs": {"itemprop": "description"}},
            {"id": "description"},
            {"class_": "product attribute description"},
        ]:
            el = soup.find(["div", "p", "section"], **sel)
            if el:
                text = el.get_text(strip=True)
                if len(text) > 10:
                    return text[:2000]

        # Look for description section heading followed by content
        for heading in soup.find_all(["h2", "h3", "h4"]):
            if "description" in heading.get_text(strip=True).lower():
                sib = heading.find_next_sibling(["div", "p"])
                if sib:
                    text = sib.get_text(strip=True)
                    if len(text) > 10:
                        return text[:2000]
        return None

    @staticmethod
    def _strip_url_query(url: str) -> str:
        """Remove query string and fragment from a URL, returning the base URL."""
        parts = urlsplit(url)
        return urlunsplit(parts._replace(query="", fragment=""))

    def _get_image_urls(self, soup: BeautifulSoup) -> list[str]:
        """Extract product image URLs (base URL only, no query params)."""
        urls: list[str] = []

        # Primary: first Magento catalog product image (main product image)
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if "/media/catalog/product/" in src and "placeholder" not in src.lower():
                urls.append(self._strip_url_query(src))
                break  # only the first/main product image
        if urls:
            return urls

        # Fallback: gallery containers
        for sel in [
            {"class_": "gallery-placeholder"},
            {"class_": "product-image-gallery"},
            {"class_": "fotorama"},
        ]:
            container = soup.find(["div", "section"], **sel)
            if container:
                for img in container.find_all("img"):
                    src = img.get("src") or img.get("data-src") or ""
                    if src and "placeholder" not in src.lower():
                        urls.append(self._strip_url_query(src))
                if urls:
                    return list(dict.fromkeys(urls))

        return list(dict.fromkeys(urls))

    def _find_product_tables(self, soup: BeautifulSoup) -> list[dict[str, Any]]:
        """Find product variant tables and their subgroup headings.

        Returns list of dicts with 'table' (Tag) and 'subgroup_name' (str|None).
        """
        results: list[dict[str, Any]] = []

        # Strategy 1: Find <table> elements
        tables = soup.find_all("table")
        for table in tables:
            # Skip tiny tables (likely price tier tables within a row)
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            # Check if this looks like a product table by inspecting headers
            header_row = table.find("tr")
            if header_row:
                headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
                # Must have at least an item number-like and name-like column
                has_item = any(h for h in headers if "item" in h or "#" in h or "sku" in h)
                has_name = any(h for h in headers if "product" in h or "name" in h or "description" in h)
                if not (has_item or has_name) and len(headers) < 3:
                    continue

            # Find preceding heading as subgroup name
            subgroup_name = self._find_preceding_heading(table)
            results.append({"table": table, "subgroup_name": subgroup_name})

        if results:
            return results

        # Strategy 2: Look for div-based product grids
        product_sections = soup.find_all(
            ["div", "section"],
            class_=lambda c: c and any(
                kw in (c if isinstance(c, str) else " ".join(c)).lower()
                for kw in ["products-table", "product-items", "products-grid", "product-list"]
            ) if c else False,
        )
        for section in product_sections:
            inner_tables = section.find_all("table")
            if inner_tables:
                for t in inner_tables:
                    subgroup_name = self._find_preceding_heading(t)
                    results.append({"table": t, "subgroup_name": subgroup_name})
            else:
                # Treat the div itself as a "table" -- _extract_table_rows
                # will try to parse it
                subgroup_name = self._find_preceding_heading(section)
                results.append({"table": section, "subgroup_name": subgroup_name})

        # Strategy 3: Look for data-content-type blocks
        if not results:
            blocks = soup.find_all(attrs={"data-content-type": True})
            for block in blocks:
                inner_tables = block.find_all("table")
                for t in inner_tables:
                    rows = t.find_all("tr")
                    if len(rows) >= 2:
                        subgroup_name = self._find_preceding_heading(t)
                        results.append({"table": t, "subgroup_name": subgroup_name})

        return results

    def _find_preceding_heading(self, element: Tag) -> str | None:
        """Find the nearest preceding heading (h2-h4) as a subgroup name."""
        for sib in element.previous_siblings:
            if isinstance(sib, Tag) and sib.name in ("h2", "h3", "h4"):
                text = sib.get_text(strip=True)
                if text and len(text) < 200:
                    return text
            # Don't look past another table
            if isinstance(sib, Tag) and sib.name == "table":
                break
        # Check parent for heading
        parent = element.parent
        if parent:
            for sib in parent.previous_siblings:
                if isinstance(sib, Tag) and sib.name in ("h2", "h3", "h4"):
                    text = sib.get_text(strip=True)
                    if text and len(text) < 200:
                        return text
                if isinstance(sib, Tag) and sib.name == "table":
                    break
        return None

    def _extract_table_rows(self, table: Tag) -> list[dict[str, Any]]:
        """Extract variant data from table rows."""
        rows = table.find_all("tr")
        if not rows:
            return []

        # Determine column mapping from header row
        col_map = self._detect_columns(rows[0])
        data_rows = rows[1:] if col_map else rows

        variants: list[dict[str, Any]] = []
        for tr in data_rows:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue

            variant: dict[str, Any] = {}

            if col_map:
                for i, cell in enumerate(cells):
                    field = col_map.get(i)
                    if not field:
                        continue
                    if field == "price":
                        variant["price"] = self._parse_price_cell(cell)
                    else:
                        text = cell.get_text(strip=True)
                        variant[field] = text if text else None
            else:
                # No header detected -- try positional mapping
                # Common order: Item#, Product Name, Mfr#, Description, Availability, Price
                if len(cells) >= 6:
                    variant["item_number"] = cells[0].get_text(strip=True)
                    variant["product_name"] = cells[1].get_text(strip=True)
                    # Check for link text in name cell
                    link = cells[1].find("a")
                    if link:
                        variant["product_name"] = link.get_text(strip=True)
                    variant["manufacturer_number"] = cells[2].get_text(strip=True)
                    raw_desc = cells[3].get_text(strip=True)
                    variant["description"] = raw_desc
                    variant["unit_size"] = raw_desc
                    variant["availability"] = cells[4].get_text(strip=True)
                    variant["price"] = self._parse_price_cell(cells[5])
                elif len(cells) >= 4:
                    variant["item_number"] = cells[0].get_text(strip=True)
                    variant["product_name"] = cells[1].get_text(strip=True)
                    link = cells[1].find("a")
                    if link:
                        variant["product_name"] = link.get_text(strip=True)
                    variant["availability"] = cells[2].get_text(strip=True) if len(cells) > 2 else None
                    variant["price"] = self._parse_price_cell(cells[-1])

            # Only keep if we have an item number
            item_num = variant.get("item_number", "")
            vname = variant.get("product_name", "")
            if item_num and vname:
                variants.append(variant)

        return variants

    def _detect_columns(self, header_row: Tag) -> dict[int, str]:
        """Map column indices to field names based on header text."""
        cells = header_row.find_all(["th", "td"])
        col_map: dict[int, str] = {}
        keywords = {
            "item_number": ["item", "#", "sku", "item #", "safco"],
            "product_name": ["product", "name", "product name"],
            "manufacturer_number": ["mfr", "manufacturer", "mfr #", "mfg"],
            "unit_size": ["description", "size", "unit", "pack"],
            "availability": ["availability", "stock", "avail"],
            "price": ["price", "cost", "msrp"],
        }
        for i, cell in enumerate(cells):
            text = cell.get_text(strip=True).lower()
            for field, kws in keywords.items():
                if any(kw in text for kw in kws):
                    if field not in col_map.values():
                        col_map[i] = field
                        break
        return col_map

    def _parse_price_cell(self, cell: Tag) -> dict[int, str]:
        """Parse a price cell, handling both simple prices and qty-break tables."""
        price_dict: dict[int, str] = {}

        # Check for nested price table (qty breaks)
        nested_table = cell.find("table")
        if nested_table:
            rows = nested_table.find_all("tr")
            for row in rows:
                cells_inner = row.find_all(["td", "th"])
                if len(cells_inner) >= 2:
                    qty_text = cells_inner[0].get_text(strip=True)
                    price_text = cells_inner[1].get_text(strip=True)
                    qty = self._parse_qty(qty_text)
                    price_val = self._parse_price_value(price_text)
                    if qty and price_val:
                        price_dict[qty] = price_val
            if price_dict:
                return price_dict

        # Check for multiple price spans/divs (common in Magento themes)
        price_items = cell.find_all(["span", "div"], class_=lambda c: c and "price" in c.lower() if c else False)
        if len(price_items) > 1:
            for idx, item in enumerate(price_items):
                price_val = self._parse_price_value(item.get_text(strip=True))
                if price_val:
                    price_dict[idx + 1] = price_val
            if price_dict:
                return price_dict

        # Simple single price
        text = cell.get_text(strip=True)
        price_val = self._parse_price_value(text)
        if price_val:
            price_dict[1] = price_val

        return price_dict

    def _parse_qty(self, text: str) -> int | None:
        """Extract a quantity number from text like 'Qty 6' or '12+'."""
        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else None

    def _parse_price_value(self, text: str) -> str | None:
        """Extract a decimal price from text like '$36.99'."""
        match = re.search(r"\$?([\d,]+\.?\d*)", text)
        if match:
            return match.group(1).replace(",", "")
        return None

    # ------------------------------------------------------------------
    # FALLBACK: LLM extraction
    # ------------------------------------------------------------------

    async def _extract_llm(
        self, html: str, url: str, settings: Settings
    ) -> list[dict[str, Any]]:
        """Extract product variants using LLM tool_use as a fallback."""
        import litellm

        # Pre-process HTML: strip non-essential sections
        cleaned = self._preprocess_html(html)

        # Truncate to avoid token limits (roughly 100k chars ~ 25k tokens)
        if len(cleaned) > 100_000:
            cleaned = cleaned[:100_000]

        try:
            response = await litellm.acompletion(
                model=settings.extractor_model,
                api_key=settings.openrouter_api_key,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Extract all product variants from this Safco Dental product page.\n"
                            f"URL: {url}\n\n"
                            f"HTML:\n{cleaned}"
                        ),
                    },
                ],
                tools=[EXTRACT_TOOL],
                tool_choice={"type": "function", "function": {"name": "extract_product_variants"}},
            )
        except Exception as exc:
            log.error("extractor_llm_error", url=url, error=str(exc))
            return []

        return self._parse_llm_response(response, url)

    def _preprocess_html(self, html: str) -> str:
        """Strip scripts, styles, nav, footer, reviews, and trending from HTML."""
        soup = BeautifulSoup(html, "lxml")

        # Remove unwanted elements
        for tag_name in ["script", "style", "noscript", "svg"]:
            for el in soup.find_all(tag_name):
                el.decompose()

        for el in soup.find_all(["nav", "footer", "header"]):
            el.decompose()

        # Remove reviews and trending sections by heading text
        for heading in soup.find_all(["h2", "h3", "h4"]):
            text = heading.get_text(strip=True).lower()
            if any(kw in text for kw in ["review", "trending", "recently viewed", "related"]):
                # Remove the heading and all siblings until the next heading
                parent = heading.parent
                if parent:
                    parent.decompose()
                else:
                    heading.decompose()

        return soup.get_text(separator="\n", strip=True)[:100_000]

    def _parse_llm_response(
        self, response: Any, url: str
    ) -> list[dict[str, Any]]:
        """Parse the LLM tool_use response into flat variant dicts."""
        variants: list[dict[str, Any]] = []

        try:
            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            for tc in tool_calls:
                if tc.function.name != "extract_product_variants":
                    continue

                args = json.loads(tc.function.arguments)
                group_name = args.get("product_group_name", "")
                brand = args.get("brand")
                description = args.get("group_description")
                image_urls = args.get("image_urls", [])

                for subgroup in args.get("subgroups", []):
                    sg_name = subgroup.get("subgroup_name")
                    for v in subgroup.get("variants", []):
                        item_num = v.get("item_number", "")
                        vname = v.get("product_name", "")
                        if not item_num:
                            continue

                        price_dict: dict[int, str] = {}
                        raw_price = v.get("price")
                        if raw_price is not None:
                            price_dict = {1: str(raw_price)}

                        raw_desc = v.get("unit_size")
                        variant = {
                            "product_group_name": group_name,
                            "product_name": vname or item_num,
                            "brand": brand,
                            "item_number": item_num,
                            "manufacturer_number": v.get("manufacturer_number"),
                            "product_group_url": url,
                            "price": price_dict,
                            "description": raw_desc,
                            "unit_size": raw_desc,
                            "availability": v.get("availability"),
                            "group_description": description,
                            "subgroup_name": sg_name,
                            "image_urls": [self._strip_url_query(u) for u in image_urls],
                            "alternative_products": [],
                            "extraction_method": "llm-fallback",
                            "validation_status": "pending",
                        }
                        variants.append(variant)
        except Exception as exc:
            log.error("extractor_llm_parse_error", url=url, error=str(exc))

        return variants
