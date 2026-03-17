from __future__ import annotations

import asyncio
import json

import structlog
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

log = structlog.get_logger()

USER_AGENT = "Mozilla/5.0 (compatible; FrontierDentalBot/1.0)"

# JavaScript run in the live DOM using CSS selectors to extract Alpine.js-rendered
# product data. Alpine.js sets textContent via x-text mutations that are not present
# in serialized HTML, so we query the live DOM directly.
_EXTRACT_JS = """
() => {
    const result = {
        product_group_name: "",
        brand: null,
        group_description: null,
        image_urls: [],
        variants_raw: [],
    };

    // Product group name
    const h1 = document.querySelector("h1");
    if (h1) result.product_group_name = h1.textContent.trim();

    // Brand — sits in .pdp-brand-review directly above h1
    const brandEl = document.querySelector(
        ".pdp-brand-review a, .product-brand, .product__brand, .brand-name, .manufacturer-name"
    );
    if (brandEl) result.brand = brandEl.textContent.trim() || null;

    // Description
    const descEl = document.querySelector(
        "[itemprop='description'], .product-description, #description .value"
    );
    if (descEl) {
        const t = descEl.textContent.trim();
        if (t.length > 10) result.group_description = t.substring(0, 2000);
    }

    // Primary product image — look in gallery container first, then first catalog img.
    // We want only the main product image, not related/trending product thumbnails.
    const gallerySelectors = [
        "[data-gallery-role='gallery-placeholder'] img[src*='/media/catalog/product/']",
        ".gallery-placeholder img[src*='/media/catalog/product/']",
        ".product-media img[src*='/media/catalog/product/']",
        ".product-image-photo",
    ];
    let mainImg = null;
    for (const sel of gallerySelectors) {
        mainImg = document.querySelector(sel);
        if (mainImg) break;
    }
    if (!mainImg) {
        // Fallback: first catalog product image anywhere in the page header area
        const allImgs = document.querySelectorAll("img[src*='/media/catalog/product/']");
        if (allImgs.length) mainImg = allImgs[0];
    }
    if (mainImg) {
        const raw = mainImg.src || mainImg.dataset.src || "";
        if (raw && !raw.includes("placeholder")) {
            try {
                const u = new URL(raw);
                result.image_urls.push(u.origin + u.pathname);
            } catch {
                result.image_urls.push(raw);
            }
        }
    }

    // "From $X.XX" price shown when there is no product table (out-of-stock / single SKU)
    const priceBox = document.querySelector(".price-box .price, .final-price .price, span.price");
    if (priceBox) {
        const priceText = priceBox.textContent.trim();
        const m = priceText.match(/\$([\d,]+\.?\d*)/);
        if (m) result.from_price = m[1].replace(/,/g, "");
    }

    // Product variants from div.grouped-items (CSS selector on live DOM)
    const rows = document.querySelectorAll("div.grouped-items");
    rows.forEach(row => {
        const variant = {};

        // Subgroup name from nearest ancestor .group-container strong[x-text]
        const container = row.closest(".group-container");
        if (container) {
            const strong = container.querySelector("strong[x-text]");
            variant.subgroup_name = strong ? strong.textContent.trim() || null : null;
        } else {
            variant.subgroup_name = null;
        }

        // All [data-attr] elements in this row (CSS selector)
        const attrEls = row.querySelectorAll("[data-attr]");
        const attrs = {};
        attrEls.forEach(el => {
            const key = el.getAttribute("data-attr");
            if (!attrs[key]) attrs[key] = [];
            attrs[key].push(el.innerText.trim());
        });

        variant["Item #"]             = (attrs["Item #"] || [""])[0];
        variant["Product Name"]        = (attrs["Product Name"] || [""])[0];
        variant["Mfr #"]               = (attrs["Mfr #"] || [""])[0];
        variant["Description"]         = (attrs["Description"] || [""])[0];
        variant["Stock Availability"]  = ((attrs["Stock Availability"] || [""])[0]).split("\\n")[0].trim();

        // Price: use the last Price element (qty-break table is there)
        const priceVals = attrs["Price"] || [];
        variant["Price"] = priceVals.length > 0 ? priceVals[priceVals.length - 1] : "";

        result.variants_raw.push(variant);
    });

    return result;
}
"""


class BrowserRenderer:
    """Playwright-based page renderer for JS-heavy pages."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        log.info("browser_started")

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        log.info("browser_stopped")

    @retry(
        stop=stop_after_attempt(2),
        retry=retry_if_exception_type((PlaywrightTimeout, Exception)),
        wait=wait_fixed(1),
        reraise=True,
    )
    async def render_page(self, url: str, semaphore: asyncio.Semaphore) -> str:
        """Navigate to *url* with a headless browser, wait for content,
        and return the rendered HTML with embedded structured data.

        For Safco product pages with Alpine.js-rendered product tables, the
        live DOM is queried via JavaScript to extract text content that isn't
        present in serialized HTML. The result is embedded as a JSON comment
        at the start of the returned HTML for the extractor to consume.

        Concurrency is limited by *semaphore*.
        """
        async with semaphore:
            context = await self._browser.new_context(user_agent=USER_AGENT)
            page = await context.new_page()
            try:
                log.debug("browser_navigate", url=url)
                # Use "load" instead of "networkidle" — avoids waiting for CDN/analytics
                await page.goto(url, wait_until="load", timeout=30_000)

                # Step-scroll to trigger Alpine.js x-defer="intersect" observers.
                # Each await gives the browser an event-loop tick so IntersectionObserver
                # callbacks fire before the next scroll position is set.
                await page.evaluate("window.scrollTo(0, 600)")
                await page.wait_for_timeout(300)
                await page.evaluate("window.scrollTo(0, 1400)")
                await page.wait_for_timeout(300)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(300)

                # Run CSS-selector queries against the live DOM to capture
                # Alpine.js x-text rendered content (not present in inner_html).
                try:
                    product_data = await page.evaluate(_EXTRACT_JS)
                    has_variants = bool(product_data.get("variants_raw"))
                    if has_variants:
                        log.debug(
                            "browser_live_dom_extracted",
                            url=url,
                            variant_count=len(product_data["variants_raw"]),
                        )
                    prefix = f"<!-- LIVE_DOM_DATA: {json.dumps(product_data)} -->\n"
                except Exception as exc:
                    log.warning("browser_js_extract_failed", url=url, error=str(exc))
                    prefix = ""

                # Also return serialized HTML for metadata (description, fallbacks).
                for container_sel in ["main", "#maincontent", "body"]:
                    element = await page.query_selector(container_sel)
                    if element:
                        html = await element.inner_html()
                        if html.strip():
                            log.debug("browser_rendered", url=url, size=len(html))
                            return prefix + html

                html = await page.content()
                log.debug("browser_rendered_full_page", url=url, size=len(html))
                return prefix + html
            finally:
                await page.close()
                await context.close()
