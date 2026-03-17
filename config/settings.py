from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- API keys ---
    openrouter_api_key: str = ""

    # --- Storage ---
    db_path: str = "frontier_dental.db"
    output_dir: str = "output"

    # --- Scraper behaviour ---
    request_delay: float = 0.2
    browser_concurrency: int = 5

    # --- LLM batch sizes ---
    normalizer_batch_size: int = 100
    validator_batch_size: int = 50

    # --- Target categories (comma-separated in env) ---
    target_categories: list[str] = Field(
        default=["sutures-surgical-products", "gloves"],
    )

    @field_validator("target_categories", mode="before")
    @classmethod
    def parse_categories(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v  # type: ignore[return-value]

    # --- Per-agent model selection ---
    extractor_model: str = "openrouter/anthropic/claude-sonnet-4-5"
    normalizer_model: str = "openrouter/anthropic/claude-haiku-4-5"
    validator_model: str = "openrouter/anthropic/claude-sonnet-4-5"
