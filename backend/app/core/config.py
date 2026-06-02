"""Application configuration.

Values come from environment variables (or a local .env file) so the same
code runs against SQLite in dev and Postgres in production.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "SCM Master"
    # SQLite by default; swap to a postgresql:// URL via DATABASE_URL in prod.
    database_url: str = "sqlite:///./scm.db"

    # Auth. Override SECRET_KEY in any real deployment (env / .env).
    secret_key: str = "dev-insecure-change-me-0000000000000000"  # >=32 bytes; override in prod
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 8


settings = Settings()
