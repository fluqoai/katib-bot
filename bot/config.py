"""Centralised settings, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs in one place. Loaded from `.env` at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str
    telegram_allowed_chat_ids: str = ""  # comma-separated; empty = allow all

    # Supabase — prefer the legacy JWT (works with supabase-py 2.10), fall
    # back to the new sb_secret_* key (requires supabase-py >= 2.20).
    supabase_url: str
    supabase_secret_key: str = ""
    supabase_service_role_key: str = ""
    supabase_publishable_key: str = ""
    supabase_project_ref: str = ""

    def resolved_service_key(self) -> str:
        """Return the key to use for the service role. Prefers the legacy
        JWT-style key because supabase-py 2.10 doesn't accept sb_secret_."""
        if self.supabase_service_role_key:
            return self.supabase_service_role_key
        return self.supabase_secret_key

    # Google Drive (OPTIONAL — only used by `scripts/ingest_drive.py`)
    google_service_account_json: str = ""
    google_drive_root_folder_id: str = ""

    # LLM
    openrouter_api_key: str
    openrouter_model: str = "openai/gpt-4o"

    # Embeddings
    embedding_provider: str = "openai"
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    google_api_key: str = ""
    google_embedding_model: str = "text-embedding-004"
    # OpenRouter embeddings (used when EMBEDDING_PROVIDER=openrouter)
    openrouter_embedding_model: str = "liquid/lfm-2.5-embedding-350m:free"
    openrouter_embedding_dim: int = 1024
    # Optional: enable the model's reasoning feature (only some models support it)
    openrouter_reasoning: bool = False

    # App
    kateb_skill_path: str = (
        "C:/Users/khayrat/.minimax/skills/arabic-official-correspondence"
    )
    app_env: str = "dev"
    log_level: str = "INFO"

    # RAG knobs
    match_threshold: float = 0.65
    match_count: int = 5

    @property
    def allowed_chat_ids(self) -> set[int]:
        if not self.telegram_allowed_chat_ids.strip():
            return set()
        return {
            int(x.strip())
            for x in self.telegram_allowed_chat_ids.split(",")
            if x.strip()
        }

    @property
    def service_account_path(self) -> Path:
        if not self.google_service_account_json:
            raise FileNotFoundError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is not set. Drive sync is "
                "optional — use `python scripts/add_doc.py` instead."
            )
        p = Path(self.google_service_account_json)
        return p if p.is_absolute() else (Path.cwd() / p)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Raises on first call if `.env` is missing."""
    return Settings()  # type: ignore[call-arg]
