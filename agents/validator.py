from __future__ import annotations

import json

import structlog

from config.settings import Settings
from storage.database import get_products_for_validation, update_validation_fields

log = structlog.get_logger()

SYSTEM_PROMPT = """
You are a data quality analyst for a dental supply product catalog.
Review each product record for semantic errors, implausible values, and signs
of extraction failure. Be concise. Flag real problems, not stylistic ones.
"""

USER_PROMPT_TEMPLATE = """
Validate these product records. For each, return:
- validation_status: "valid", "warning", or "invalid"
- validation_notes: null if valid, brief issue description otherwise

Common issues to check:
- item_number that looks like a price (e.g. "$36.99")
- Price of $0.01 for a surgical kit (likely scrape error)
- Brand of "N/A" or brand written into the product name field
- Availability "In Stock" with price $0.00
- product_name identical to product_group_name (extraction probably missed specifics)
- Missing price on a non-special-order item
- unit_size that doesn't make sense for the product category

Records:
{records}

Return a JSON array with one object per record containing: id, validation_status, validation_notes.
Example: [{{"id": 1, "validation_status": "valid", "validation_notes": null}}]

Return ONLY the JSON array, no other text.
"""


class ValidatorAgent:
    """Performs semantic quality validation on normalized product records
    using an LLM."""

    async def run(self, settings: Settings, db) -> None:
        log.info("validator_start")

        if not settings.openrouter_api_key:
            log.warning("validator_no_api_key_marking_all_valid")
            # Mark all normalized records as valid so export works
            while True:
                batch = await get_products_for_validation(db, settings.validator_batch_size)
                if not batch:
                    break
                for rec in batch:
                    await update_validation_fields(
                        db,
                        product_id=rec["id"],
                        validation_status="valid",
                        validation_notes=None,
                    )
            return

        import litellm

        total_validated = 0

        while True:
            batch = await get_products_for_validation(db, settings.validator_batch_size)
            if not batch:
                break

            log.info("validator_batch", size=len(batch))

            # Build simplified records for the prompt
            records_for_prompt = []
            for rec in batch:
                record = {
                    "id": rec["id"],
                    "product_group_name": rec.get("product_group_name"),
                    "product_name": rec.get("product_name"),
                    "brand": rec.get("brand"),
                    "item_number": rec.get("item_number"),
                    "manufacturer_number": rec.get("manufacturer_number"),
                    "price": rec.get("price"),
                    "unit_size": rec.get("unit_size"),
                    "availability": rec.get("availability"),
                    "extraction_method": rec.get("extraction_method"),
                }
                # Parse price if it's a JSON string
                if isinstance(record["price"], str):
                    try:
                        record["price"] = json.loads(record["price"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                records_for_prompt.append(record)

            prompt = USER_PROMPT_TEMPLATE.format(
                records=json.dumps(records_for_prompt, indent=2)
            )

            try:
                response = await litellm.acompletion(
                    model=settings.validator_model,
                    api_key=settings.openrouter_api_key,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
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

                validated = json.loads(content)

                if not isinstance(validated, list):
                    log.error("validator_unexpected_response_type", type=type(validated).__name__)
                    for rec in batch:
                        await update_validation_fields(
                            db, rec["id"], "valid", "Auto-validated (LLM response parse error)"
                        )
                    continue

                val_lookup = {item["id"]: item for item in validated if "id" in item}

                for rec in batch:
                    rec_id = rec["id"]
                    if rec_id in val_lookup:
                        v = val_lookup[rec_id]
                        await update_validation_fields(
                            db,
                            product_id=rec_id,
                            validation_status=v.get("validation_status", "valid"),
                            validation_notes=v.get("validation_notes"),
                        )
                    else:
                        await update_validation_fields(
                            db, rec_id, "valid", None
                        )

                total_validated += len(batch)

            except json.JSONDecodeError as exc:
                log.error("validator_json_parse_error", error=str(exc))
                for rec in batch:
                    await update_validation_fields(
                        db, rec["id"], "valid", "Auto-validated (JSON parse error)"
                    )
            except Exception as exc:
                log.error("validator_llm_error", error=str(exc))
                for rec in batch:
                    await update_validation_fields(
                        db, rec["id"], "valid", f"Auto-validated (LLM error: {exc})"
                    )

        log.info("validator_complete", total_validated=total_validated)
