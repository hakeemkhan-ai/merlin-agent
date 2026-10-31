"""Service configuration; sensitive values are never included in model context."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MERLIN_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    database_url: str = "sqlite+pysqlite:///.merlin/dev.db"
    owner_token: SecretStr = SecretStr("")
    api_url: str = "http://127.0.0.1:8000"
    artifact_dir: Path = Path(".merlin/artifacts")
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.8-flash"
    input_cost_per_million: float = Field(default=1.0, ge=0)
    output_cost_per_million: float = Field(default=10.0, ge=0)
    max_input_tokens: int = Field(default=65536, ge=8192, le=1000000)
    max_output_tokens: int = Field(default=8192, ge=1, le=65536)
    model_first_chunk_timeout_seconds: float = Field(default=60, gt=0)
    model_idle_timeout_seconds: float = Field(default=60, gt=0)
    model_total_timeout_seconds: float = Field(default=300, gt=0)
    model_stream_flush_seconds: float = Field(default=0.15, gt=0)
    model_token_cache_size: int = Field(default=64, ge=0, le=1024)
    cached_input_cost_per_million: float | None = Field(default=None, ge=0)
    workspace_setup_timeout_seconds: float = Field(default=180, gt=0)
    checkpoint_timeout_seconds: float = Field(default=120, gt=0)
    cleanup_timeout_seconds: float = Field(default=15, gt=0)
    tool_timeout_seconds: float = Field(default=660, gt=0)
    parallel_read_tools: int = Field(default=4, ge=1, le=16)
    context_cache_entries: int = Field(default=128, ge=1, le=4096)
    instruction_ttl_seconds: float = Field(default=30, ge=0)
    max_request_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    max_event_streams: int = Field(default=32, ge=1, le=1024)
    max_queued_tasks: int = Field(default=100, ge=1, le=10000)
    readiness_worker_max_age_seconds: int = Field(default=30, ge=10, le=300)
    e2b_api_key: SecretStr = SecretStr("")
    e2b_template: str = "merlin"
    github_app_id: str = ""
    github_private_key_file: Path | None = None
    github_installation_id: int | None = None
    github_webhook_secret: SecretStr = SecretStr("")
    github_client_id: str = ""
    github_client_secret: SecretStr = SecretStr("")
    github_callback_url: str = "http://127.0.0.1:8000/v1/github/callback"
    otlp_endpoint: str = ""
    artifact_retention_days: int = Field(default=30, ge=1, le=3650)
