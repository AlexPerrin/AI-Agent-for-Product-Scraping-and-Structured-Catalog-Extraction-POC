from __future__ import annotations

import asyncio
import json

import structlog

from config.settings import Settings
from storage.database import (
    get_products_for_normalization,
    mark_products_normalized,
    update_normalized_fields,
)

log = structlog.get_logger()

PROMPT_TEMPLATE = """
Normalize the following dental supply product records. For each record extract: unit_size, specifications.

Rules for unit_size:
- Extract ONLY the packaging quantity/unit — canonical form is "{{quantity}}/{{unit}}"
  e.g. "100/box", "1/vial", "4/pkg", "50/box"
- Common units: box, pkg, bag, vial, syringe, tube, bottle, roll, pair, case, each
- If no quantity is stated (e.g. a single physical item like a wall holder or instrument), return "1"
- Strip out any non-quantity descriptors (size, shape, color, dimensions) — those go in specifications
- Examples:
  - "x-small, 100/box" -> "100/box"
  - "Double (holds 2 boxes)\\nDimensions: 11\\"W x 10.5\\"H x 4\\"D" -> "1"
  - "bx/100" -> "100/box", "per box of 100" -> "100/box"
  - "1 vial, 2.5 ml" -> "2.5ml/vial"
  - "#15C, 100/box" -> "100/box"

Rules for specifications:
- A JSON object capturing the product's distinguishing attributes (the things that differentiate variants)
- Use intelligent, context-aware attribute keys — do NOT use a generic "Specification" key
- Choose the right key based on what the attribute actually describes:
  - Glove size -> {{"Size": "X-small"}}
  - Physical dimensions -> {{"Dimensions": "11\\"W x 10.5\\"H x 4\\"D"}}
  - Blade/instrument shape or number -> {{"Shape": "#15C"}}
  - Color -> {{"Color": "Blue"}}
  - Capacity (e.g. holds N boxes) -> {{"Capacity": "Double (holds 2 boxes)"}}
  - Flavor -> {{"Flavor": "Mint"}}
  - Material -> {{"Material": "Latex"}}
  - Multiple attributes -> {{"Size": "Medium", "Color": "Blue"}}
- If there are no distinguishing attributes beyond what's in the product name, return {{}}

Records (JSON):
{records}

Return a JSON array with one object per record: id, unit_size, specifications.
Example: [{{"id": 1, "unit_size": "100/box", "specifications": {{"Size": "X-small"}}}}]

Return ONLY the JSON array, no other text.
"""


class NormalizerAgent:
    """Normalizes unit_size and brand fields using an LLM in batches."""

    async def run(self, settings: Settings, db) -> None:
        log.info("normalizer_start")

        if not settings.openrouter_api_key:
            log.warning("normalizer_no_api_key_skipping")
            # Mark all pending as normalized so pipeline continues
            while True:
                batch = await get_products_for_normalization(db, settings.normalizer_batch_size)
                if not batch:
                    break
                ids = [r["id"] for r in batch]
                await mark_products_normalized(db, ids)
            return

        import litellm

        total_normalized = 0

        while True:
            batch = await get_products_for_normalization(db, settings.normalizer_batch_size)
            if not batch:
                break

            log.info("normalizer_batch", size=len(batch))

            # Build simplified records for the prompt
            records_for_prompt = []
            for rec in batch:
                records_for_prompt.append({
                    "id": rec["id"],
                    "item_number": rec["item_number"],
                    "product_name": rec["product_name"],
                    "raw_unit_size": rec.get("unit_size"),
                    "group_description": rec.get("group_description"),
                    "product_group_name": rec.get("product_group_name"),
                })

            prompt = PROMPT_TEMPLATE.format(records=json.dumps(records_for_prompt, indent=2))

            try:
                response = None
                for attempt in range(6):
                    try:
                        response = await litellm.acompletion(
                            model=settings.normalizer_model,
                            api_key=settings.openrouter_api_key,
                            messages=[{"role": "user", "content": prompt}],
                        )
                        break
                    except litellm.RateLimitError:
                        wait = 15 * (2 ** attempt)  # 15, 30, 60, 120, 240, 480s
                        log.warning("normalizer_rate_limit_retry", attempt=attempt + 1, wait_seconds=wait)
                        await asyncio.sleep(wait)
                else:
                    log.error("normalizer_rate_limit_exhausted")
                    ids = [r["id"] for r in batch]
                    await mark_products_normalized(db, ids)
                    continue

                content = response.choices[0].message.content.strip()
                # Strip markdown code fences if present
                if content.startswith("```"):
                    content = content.split("\n", 1)[1] if "\n" in content else content[3:]
                if content.endswith("```"):
                    content = content[:-3].strip()
                if content.startswith("json"):
                    content = content[4:].strip()

                normalized = json.loads(content)

                if not isinstance(normalized, list):
                    log.error("normalizer_unexpected_response_type", type=type(normalized).__name__)
                    # Still mark as normalized to avoid infinite loop
                    ids = [r["id"] for r in batch]
                    await mark_products_normalized(db, ids)
                    continue

                # Build a lookup by id
                norm_lookup = {item["id"]: item for item in normalized if "id" in item}

                for rec in batch:
                    rec_id = rec["id"]
                    if rec_id in norm_lookup:
                        norm = norm_lookup[rec_id]
                        specs = norm.get("specifications")
                        if specs and not isinstance(specs, str):
                            specs = json.dumps(specs)
                        elif not specs:
                            specs = "{}"
                        await update_normalized_fields(
                            db,
                            product_id=rec_id,
                            unit_size=norm.get("unit_size") or rec.get("unit_size"),
                            brand=rec.get("brand"),
                            specifications=specs,
                        )
                    # else keep original values

                ids = [r["id"] for r in batch]
                await mark_products_normalized(db, ids)
                total_normalized += len(batch)

            except json.JSONDecodeError as exc:
                log.error("normalizer_json_parse_error", error=str(exc))
                # Mark batch as normalized anyway so we don't loop forever
                ids = [r["id"] for r in batch]
                await mark_products_normalized(db, ids)
            except Exception as exc:
                log.error("normalizer_llm_error", error=str(exc))
                ids = [r["id"] for r in batch]
                await mark_products_normalized(db, ids)

        log.info("normalizer_complete", total_normalized=total_normalized)
