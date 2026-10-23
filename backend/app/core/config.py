from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    database_url: str = "sqlite+aiosqlite:///./dev.db"
    github_app_id: str = ""
    github_app_private_key: str = ""  # PEM text or base64 of the PEM
    github_webhook_secret: str = ""
    cron_secret: str = ""  # protects /internal/jobs/run
    dashboard_token: str = ""  # password for /dashboard; empty = dashboard disabled
    public_base_url: str = ""  # e.g. https://doctruth-ai.vercel.app
    job_max_attempts: int = 3
    job_time_budget_s: float = 40.0  # stop claiming new jobs after this many seconds in one invocation
    max_webhook_bytes: int = 5_000_000
    dry_run: bool = False  # analyse and draft everything, write nothing to GitHub (kill switch for publishing)
    jobs_per_invocation: int = 5
    # LLM (OpenAI-compatible; Groq by default). Without a key the pipeline still runs and asks questions instead.
    llm_api_key: str = Field("", validation_alias=AliasChoices("LLM_API_KEY", "GROQ_API_KEY"))
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"
    llm_fallback_model: str = "openai/gpt-oss-20b"
    llm_max_calls: int = 12  # per run: bounds cost and serverless time
    confidence_threshold: float = 0.8  # below this an edit becomes a question to the PR author

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://") :]
        if url.startswith("postgresql+asyncpg://") and "?" in url:
            url = url.split("?", 1)[0]  # asyncpg rejects libpq query params like sslmode
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
