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
    anthropic_api_key: str | None = None
    llm_model: str = "claude-sonnet-5-5"
    llm_max_tokens: int = 1500
    demo_controls: bool = True            # lets the UI reset the synthetic demo data; set DEMO_CONTROLS=false to disable
    mcp_log_path: str = "logs/mcp.jsonl"   # JSON Lines audit log of MCP calls and handoffs; empty disables
    receipt_dir: str = "receipts"  # deletion receipts (JSON); empty string disables writing

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
