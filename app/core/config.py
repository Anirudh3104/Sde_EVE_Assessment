from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables (and `.env` if present)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    database_url: str = "postgresql+asyncpg://eve:eve@localhost:5432/eve"
    test_database_url: str = "postgresql+asyncpg://eve:eve@localhost:5432/eve_test"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    webhook_secret: str = "dev-only-webhook-secret"
    webhook_target_url: str = "http://localhost:8000/payments/webhook/"
    payment_success_rate: float = Field(default=0.8, ge=0.0, le=1.0)
    provider_delay_seconds: float = 1.0
    provider_max_attempts: int = 3
    simulate_duplicate_webhooks: bool = False

    cache_enabled: bool = True
    cache_ttl_seconds: int = 300

    rate_limit_enabled: bool = True
    login_rate_limit: str = "5/minute"
    signup_rate_limit: str = "5/minute"
    payment_rate_limit: str = "10/minute"

    log_level: str = "INFO"
    log_json: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
