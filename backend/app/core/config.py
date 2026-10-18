from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./dev.db"
    github_app_id: str = ""
    github_app_private_key: str = ""  # PEM text or base64 of the PEM
    github_webhook_secret: str = ""
    cron_secret: str = ""  # protects /internal/jobs/run
    public_base_url: str = ""  # e.g. https://doctruth-ai.vercel.app
    job_max_attempts: int = 3
    jobs_per_invocation: int = 5

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
