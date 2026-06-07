"""Application configuration.

Values come from environment variables (or a local .env file) so the same
code runs against SQLite in dev and Postgres in production.
"""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET = "dev-insecure-change-me-0000000000000000"  # nosec B105 — sentinel default; validate_production() refuses to boot prod with it


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "SCM Master"
    # Deployment environment. "prod" enforces the secure-config guard regardless
    # of database dialect (a Postgres URL also triggers it). Default: "dev".
    scm_env: str = "dev"
    # SQLite by default; swap to a postgresql:// URL via DATABASE_URL in prod.
    database_url: str = "sqlite:///./scm.db"

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        """Make Railway's injected DATABASE_URL work as-is.

        Hosting providers expose Postgres as ``postgresql://…`` (and sometimes
        the legacy ``postgres://``). SQLAlchemy maps a bare ``postgresql://`` to
        the psycopg2 driver, which we don't ship — so we pin the psycopg (v3)
        driver we DO ship. Paste the provider's URL unchanged; this rewrites it.
        """
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

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


def is_production() -> bool:
    """Production is decided SOLELY by SCM_ENV=prod — never inferred.

    This is the one switch that forge-locks the environment: it blocks demo
    seeding and destructive helpers (see seed scripts / safety.py). It must NOT
    be inferred from the database dialect, because the DEMO also runs on
    Postgres — inferring prod from Postgres would wrongly lock the demo.
    """
    return settings.scm_env.strip().lower() == "prod"


def _uses_postgres() -> bool:
    return settings.database_url.startswith("postgresql")


def validate_production() -> None:
    """Fail closed on insecure config. Called at startup (see app.main).

    Any non-SQLite (i.e. real) deployment must override SECRET_KEY with a strong
    value, and an explicit SCM_ENV=prod always enforces it too. SQLite dev is
    left alone so local development just works.
    """
    if not (is_production() or _uses_postgres()):
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


def announce_startup() -> None:
    """Log the environment mode + DB at boot, and assert prod is on durable storage.

    Makes it impossible to be unsure which mode you're serving. In production we
    also refuse SQLite — prod must be on a persistent database, never an
    ephemeral in-container file that a redeploy would wipe.
    """
    mode = "PRODUCTION (forge-locked)" if is_production() else "DEMO/DEV"
    dialect = "postgres" if _uses_postgres() else (
        "sqlite" if settings.database_url.startswith("sqlite") else "other")
    print(f"[startup] mode={mode}  db={dialect}  scm_env={settings.scm_env!r}")
    if is_production() and not _uses_postgres():
        raise RuntimeError(
            "Refusing to boot PRODUCTION on non-persistent storage: SCM_ENV=prod "
            "requires a postgresql:// DATABASE_URL (SQLite resets on redeploy)."
        )
