"""Application configuration.

Values come from environment variables (or a local .env file) so the same
code runs against SQLite in dev and Postgres in production.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET = "dev-insecure-change-me-0000000000000000"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "SCM Master"
    # Deployment environment. "prod" enforces the secure-config guard regardless
    # of database dialect (a Postgres URL also triggers it). Default: "dev".
    scm_env: str = "dev"
    # SQLite by default; swap to a postgresql:// URL via DATABASE_URL in prod.
    database_url: str = "sqlite:///./scm.db"

    # Auth. Override SECRET_KEY in any real deployment (env / .env).
    secret_key: str = _INSECURE_SECRET  # >=32 bytes; override in prod
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 8

    # Login rate limit — fixed-window, in-process (per client IP). Env-overridable.
    login_rate_limit: int = 10          # max attempts per window per IP
    login_rate_window_seconds: int = 300  # window length (5 minutes)

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


def validate_production() -> None:
    """Fail closed on insecure config. Called at startup (see app.main).

    A real deployment must override SECRET_KEY. We treat the app as "in
    production" when SCM_ENV=prod OR the database is Postgres — in either case
    a still-default or too-short secret key aborts boot. SQLite + dev is left
    alone so local development just works.
    """
    is_postgres = settings.database_url.startswith("postgresql")
    is_prod = settings.scm_env.lower() == "prod" or is_postgres
    if not is_prod:
        return
    if settings.secret_key == _INSECURE_SECRET:
        raise RuntimeError(
            "Refusing to boot: SECRET_KEY is still the insecure default. "
            "Set a strong SECRET_KEY (>=32 chars) via the environment."
        )
    if len(settings.secret_key) < 32:
        raise RuntimeError(
            "Refusing to boot: SECRET_KEY must be at least 32 characters "
            f"in production (got {len(settings.secret_key)})."
        )
