"""Application settings, validated at startup (pydantic-settings). Values come from env or .env."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    git_sha: str = "dev"

    # Models ("provider:model" strings understood by app.llm.registry)
    primary_model: str = "ollama:qwen3.5:9b"
    fallback_model: str | None = None
    judge_model: str | None = None
    ollama_base_url: str = "http://127.0.0.1:11434"

    # Providers. "auto" picks the live provider when its key is present, else the keyless option.
    places_provider: Literal["auto", "google", "osm_snapshot", "fixture"] = "auto"
    events_provider: Literal["auto", "tavily", "fixture", "none"] = "auto"
    weather_provider: Literal["open_meteo", "fixture"] = "open_meteo"
    geocoder: Literal["open_meteo", "fixture"] = "open_meteo"
    google_maps_api_key: SecretStr | None = None
    tavily_api_key: SecretStr | None = None
    # Model-provider keys. LangChain reads these from the process environment; export_provider_env()
    # copies values that came from .env into os.environ so `uv run uvicorn` works without --env-file.
    anthropic_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    xai_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    langsmith_api_key: SecretStr | None = None
    fixture_weather: str = "clear"  # clear | rain_85pct | unavailable (fixture provider only)

    # Runtime limits
    run_deadline_s: int = 60
    max_agent_turns: int = 10
    max_submit_attempts: int = 3  # initial + 2 repairs
    daily_plan_cap: int = 200
    rate_limit_per_10min: int = 15  # new plans only; clarification resumes are not counted
    rate_limit_per_day: int = 60
    max_body_bytes: int = 16_384
    trust_proxy_headers: bool = False  # True behind Render/other proxies: client IP from X-Forwarded-For
    share_ttl_days: int = 30
    frontend_dist: str = "../frontend/dist"

    # Storage and observability
    database_url: str = "sqlite:///./data/app.db"
    sentry_dsn: SecretStr | None = None
    tracing_enabled: bool = False

    # Testing and evals only. Ignored when app_env == "prod".
    fixed_now: datetime | None = None
    fault: str | None = None

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    def resolved_places_provider(self) -> str:
        if self.places_provider != "auto":
            return self.places_provider
        return "google" if self.google_maps_api_key else "osm_snapshot"

    def resolved_events_provider(self) -> str:
        if self.events_provider != "auto":
            return self.events_provider
        return "tavily" if self.tavily_api_key else "none"

    def active_fault(self) -> str | None:
        return None if self.is_prod else self.fault

    def export_provider_env(self) -> None:
        """Expose provider keys loaded from .env to libraries that read os.environ. Never overwrites."""
        import os

        for env_name, value in (
            ("ANTHROPIC_API_KEY", self.anthropic_api_key),
            ("GOOGLE_API_KEY", self.google_api_key),
            ("XAI_API_KEY", self.xai_api_key),
            ("GROQ_API_KEY", self.groq_api_key),
            ("LANGSMITH_API_KEY", self.langsmith_api_key),
        ):
            if value is not None and value.get_secret_value() and not os.environ.get(env_name):
                os.environ[env_name] = value.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
