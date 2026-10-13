"""Application settings, loaded from environment / .env with prefix BIDER_."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BIDER_")

    database_url: str
    fernet_key: str
    admin_username: str
    admin_password: str
    session_days: int = 30
    raw_log_days: int = 14
    run_collector: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
