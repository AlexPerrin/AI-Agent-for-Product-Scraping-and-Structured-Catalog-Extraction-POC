from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict

import structlog

from config.settings import Settings
from storage.database import (
    get_all_normalized_products,
    update_product_specifications,
    update_validation_fields,
)

log = structlog.get_logger()

SYSTEM_PROMPT = """You are a data quality specialist for a dental supply product catalog.
Your task is to determine whether specification keys used across product variants refer to the
same attribute or genuinely different attributes, and to choose the most precise canonical name."""

USER_PROMPT_TEMPLATE = """Product group: "{group_name}"
Group description: {group_description}

The following specification keys appear across variants of this product but never in the same record.
Each key may represent the same underlying attribute (just named inconsistently across LLM batches)
or a genuinely different attribute.

Keys and sample values:
{keys_with_samples}

Instructions:
- Group keys that refer to the same attribute together under one canonical name.
- Keys that refer to distinct attributes must remain separate.
- Choose the most semantically precise and appropriate canonical name for each group.
- Do NOT merge keys that describe different things (e.g. "Shape" and "Material" are different).

Return a JSON object mapping every key listed above to its canonical name.
Keys that are aliases of each other will map to the same canonical name.
Keys that are distinct attributes map to their own best name.

Example: {{"Blade": "Shape", "Shape": "Shape", "Material": "Material"}}

Return ONLY the JSON object, no other text."""


class ValidatorAgent:
    """Cross-checks specification keys within product groups and harmonizes them.

    Detection is deterministic: mutually exclusive keys within a group (keys
    that never appear together in the same product record) are flagged as
    candidates for the same attribute slot.

    An LLM then judges each flagged cluster: are the keys genuine aliases
    (e.g. "Blade" and "Shape" for instrument blade number) or distinct
    attributes that happen to be mutually exclusive (e.g. "Shape" and
    "Material")?  Only confirmed aliases are renamed; the canonical name is
    chosen by the LLM for best semantic fit.

    Falls back to the most-frequent key (deterministic) if no API key is set.
    """

    async def run(self, settings: Settings, db) -> None:
        log.info("validator_start")

        products = await get_all_normalized_products(db)

        if not products:
            log.info("validator_no_products")
            return

        # Group by product_group_name
        groups: dict[str, list[dict]] = defaultdict(list)
        for p in products:
            groups[p["product_group_name"]].append(p)

        use_llm = bool(settings.openrouter_api_key)
        if not use_llm:
            log.warning("validator_no_api_key_using_deterministic_fallback")

        import litellm as _litellm  # imported here to avoid top-level dep

        total_fixed = 0
        for group_name, group_products in groups.items():
            fixed = await self._harmonize_group(
                _litellm if use_llm else None,
                settings,
                db,
                group_name,
                group_products,
            )
            total_fixed += fixed

        for p in products:
            await update_validation_fields(db, p["id"], "valid", None)

        log.info(
            "validator_complete",
            groups=len(groups),
            products=len(products),
            specs_fixed=total_fixed,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _harmonize_group(
        self,
        litellm,
        settings: Settings,
        db,
        group_name: str,
        products: list[dict],
    ) -> int:
        """Detect key mismatches and resolve them — via LLM if available."""
        parsed: list[tuple[int, dict]] = []
        for p in products:
            specs_raw = p.get("specifications") or "{}"
            try:
                specs = json.loads(specs_raw) if isinstance(specs_raw, str) else specs_raw
            except (json.JSONDecodeError, TypeError):
                specs = {}
            parsed.append((p["id"], specs))

        key_counts: Counter = Counter()
        for _, specs in parsed:
            for k in specs:
                key_counts[k] += 1

        if len(key_counts) <= 1:
            return 0

        all_keys = list(key_counts)

        # Index: key -> set of product IDs that carry it
        key_to_ids: dict[str, set[int]] = defaultdict(set)
        for pid, specs in parsed:
            for k in specs:
                key_to_ids[k].add(pid)

        # Find clusters of mutually exclusive keys (no product has both)
        clusters = self._find_mutual_exclusion_clusters(all_keys, key_to_ids)

        # Only care about clusters with more than one key
        conflicted = [c for c in clusters if len(c) > 1]
        if not conflicted:
            return 0

        # Resolve canonical names
        if litellm is not None:
            renames = await self._resolve_with_llm(
                litellm, settings, group_name, products, conflicted, key_counts
            )
        else:
            renames = self._resolve_deterministic(conflicted, key_counts)

        if not renames:
            return 0

        log.info("validator_key_mismatch", group=group_name, renames=renames)

        fixed = 0
        for pid, specs in parsed:
            if not any(k in renames for k in specs):
                continue
            new_specs = {renames.get(k, k): v for k, v in specs.items()}
            await update_product_specifications(db, pid, json.dumps(new_specs))
            fixed += 1

        return fixed

    def _find_mutual_exclusion_clusters(
        self,
        all_keys: list[str],
        key_to_ids: dict[str, set[int]],
    ) -> list[list[str]]:
        """Group mutually exclusive keys into clusters using union-find."""
        parent: dict[str, str] = {k: k for k in all_keys}

        def find(k: str) -> str:
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        def union(k1: str, k2: str) -> None:
            r1, r2 = find(k1), find(k2)
            if r1 != r2:
                parent[r2] = r1

        for i, k1 in enumerate(all_keys):
            for k2 in all_keys[i + 1:]:
                if key_to_ids[k1].isdisjoint(key_to_ids[k2]):
                    union(k1, k2)

        cluster_map: dict[str, list[str]] = defaultdict(list)
        for k in all_keys:
            cluster_map[find(k)].append(k)

        return list(cluster_map.values())

    async def _resolve_with_llm(
        self,
        litellm,
        settings: Settings,
        group_name: str,
        products: list[dict],
        conflicted: list[list[str]],
        key_counts: Counter,
    ) -> dict[str, str]:
        """Ask the LLM to pick canonical key names for each conflicted cluster."""
        # Collect sample values per key (up to 5)
        key_samples: dict[str, list[str]] = defaultdict(list)
        for p in products:
            specs_raw = p.get("specifications") or "{}"
            try:
                specs = json.loads(specs_raw) if isinstance(specs_raw, str) else specs_raw
            except (json.JSONDecodeError, TypeError):
                specs = {}
            for k, v in specs.items():
                if len(key_samples[k]) < 5:
                    key_samples[k].append(str(v))

        # Format all conflicted keys with their samples
        all_conflict_keys: set[str] = {k for cluster in conflicted for k in cluster}
        keys_with_samples = "\n".join(
            f"  \"{k}\" ({key_counts[k]} products): {', '.join(repr(s) for s in key_samples[k])}"
            for k in sorted(all_conflict_keys)
        )

        group_desc = None
        for p in products:
            if p.get("group_description"):
                group_desc = p["group_description"][:200]
                break

        prompt = USER_PROMPT_TEMPLATE.format(
            group_name=group_name,
            group_description=repr(group_desc) if group_desc else "N/A",
            keys_with_samples=keys_with_samples,
        )

        for attempt in range(4):
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
                content = self._strip_fences(content)
                mapping: dict[str, str] = json.loads(content)
                # Build renames: only keys whose canonical name differs from their current name
                return {k: v for k, v in mapping.items() if v != k}

            except litellm.RateLimitError:
                wait = 15 * (2 ** attempt)
                log.warning("validator_rate_limit_retry", attempt=attempt + 1, wait_seconds=wait)
                await asyncio.sleep(wait)
            except (json.JSONDecodeError, Exception) as exc:
                log.warning("validator_llm_error", group=group_name, error=str(exc))
                break

        # LLM failed — fall back to deterministic
        log.warning("validator_llm_fallback_deterministic", group=group_name)
        return self._resolve_deterministic(conflicted, key_counts)

    def _resolve_deterministic(
        self,
        conflicted: list[list[str]],
        key_counts: Counter,
    ) -> dict[str, str]:
        """Fallback: rename every key in a cluster to the most-frequent one."""
        renames: dict[str, str] = {}
        for cluster in conflicted:
            canonical = max(cluster, key=lambda k: key_counts[k])
            for k in cluster:
                if k != canonical:
                    renames[k] = canonical
        return renames

    @staticmethod
    def _strip_fences(content: str) -> str:
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3].strip()
        if content.startswith("json"):
            content = content[4:].strip()
        return content
