"""Runtime configuration, read from environment variables or a .env file."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # .env may live in the repo root (when running from backend/) or the cwd.
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_password: str | None = None
    api_cors_origins: str = "http://localhost:5173"
    gemini_api_key: str | None = None
    anthropic_api_key: str | None = None
    llm_provider: str | None = None       # "gemini" or "anthropic"; auto-detected if None
    llm_model: str | None = None
    llm_max_tokens: int = 1500
    demo_controls: bool = True            # lets the UI reset the synthetic demo data; set DEMO_CONTROLS=false to disable
    mcp_log_path: str = "logs/mcp.jsonl"   # JSON Lines audit log of MCP calls and handoffs; empty disables
    receipt_dir: str = "receipts"  # deletion receipts (JSON); empty string disables writing

    @property
    def resolved_provider(self) -> str:
        if self.llm_provider:
            return self.llm_provider.lower().strip()
        if self.gemini_api_key and not self.anthropic_api_key:
            return "gemini"
        if self.anthropic_api_key and not self.gemini_api_key:
            return "anthropic"
        if self.gemini_api_key:
            return "gemini"
        return "anthropic"

    @property
    def resolved_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        return "gemini-2.5-flash" if self.resolved_provider == "gemini" else "claude-sonnet-5-5"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
