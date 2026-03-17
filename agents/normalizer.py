from __future__ import annotations

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
Normalize the following product records from a dental supply catalog.
For each record, return normalized values for: unit_size, brand.

Rules:
- unit_size: canonical form is "{{quantity}}/{{unit}}" e.g. "100/box", "1/vial", "4/pkg"
  - If the raw value is empty or unclear, return null
  - Common units: box, pkg, bag, vial, syringe, tube, bottle, roll, pair, case, each
  - "bx/100" -> "100/box", "per box of 100" -> "100/box", "Box 100ct" -> "100/box"
  - "1 vial, 2.5 ml" -> "2.5ml/vial"
- brand: remove legal suffixes like "(NOW SOLVENTUM)", expand common abbreviations
  - "3M HEALTH CARE (NOW SOLVENTUM)" -> "3M / Solventum"
  - Keep brand name concise and recognizable
  - If brand is already clean, return it as-is
  - If brand is empty or null, return null

Records (JSON):
{records}

Return a JSON array with one object per record containing: id, unit_size, brand.
Example: [{{"id": 1, "unit_size": "100/box", "brand": "3M / Solventum"}}]

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
                    "description": rec.get("description"),
                    "unit_size": rec.get("unit_size"),
                    "brand": rec.get("brand"),
                    "product_group_name": rec.get("product_group_name"),
                })

            prompt = PROMPT_TEMPLATE.format(records=json.dumps(records_for_prompt, indent=2))

            try:
                response = await litellm.acompletion(
                    model=settings.normalizer_model,
                    api_key=settings.openrouter_api_key,
                    messages=[
                        {
                            "role": "user",
                            "content": prompt,
                        }
                    ],
                )

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
                        await update_normalized_fields(
                            db,
                            product_id=rec_id,
                            unit_size=norm.get("unit_size") or rec.get("unit_size"),
                            brand=norm.get("brand") or rec.get("brand"),
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
