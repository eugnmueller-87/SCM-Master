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

    # Agent / Anthropic. Set ANTHROPIC_API_KEY via env / .env to enable the copilot.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # Weekly purchasing automation — gates and defaults (all env-overridable).
    auto_place_spend_cap: float = 25000.0      # ACT bundles above this can't auto-place
    act_confidence_floor: float = 0.8          # min copilot confidence to auto-place
    escalate_spend_threshold: float = 50000.0  # bundle total at/above this -> escalate
    replace_ratio: float = 1.0                 # replacements per decommissioned unit
    default_reorder_floor: int = 0             # per-product floor when none is set

    # Requisition auto-place gate + outcome-feedback calibration.
    auto_place_confidence: float = 0.85        # calibrated confidence at/above which a PR auto-converts to a PO
    calibration_min_samples: int = 3           # min feedback rows before trust adjusts the bar
    calibration_max_delta: float = 0.10        # most the bar can move down (trusted) or up (risky)

    # Demand forecasting — usage-driven projection (all env-overridable).
    demand_horizon_days: int = 90              # how far ahead the forecast projects
    demand_window_days: int = 90               # trailing usage window for the rate
    demand_halflife_days: int = 30             # recency weighting (smaller = more reactive)
    asset_useful_life_days: int = 1095         # ~3y; deployed beyond this is a refresh candidate


settings = Settings()
