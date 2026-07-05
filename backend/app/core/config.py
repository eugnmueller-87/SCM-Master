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
    # POLICY (2026-06-09, pending management sign-off on the €200k figure): trust
    # the DETERMINISTIC evidence and auto-place when confidence ≥ 0.90 AND the
    # order total < €200k; otherwise escalate to a human. The €200k spend ceiling
    # is the real brake (the LLM is advisory only). See the auto-place-threshold
    # policy note / docs/forecast-engine-decision.md sibling docs.
    auto_place_spend_cap: float = 200000.0     # ACT bundles at/above this can't auto-place
    act_confidence_floor: float = 0.90         # min deterministic confidence to auto-place
    escalate_spend_threshold: float = 200000.0 # bundle total at/above this -> escalate
    replace_ratio: float = 1.0                 # replacements per decommissioned unit
    default_reorder_floor: int = 0             # per-product floor when none is set

    # Requisition auto-place gate + outcome-feedback calibration.
    auto_place_confidence: float = 0.90        # calibrated confidence at/above which a PR auto-converts to a PO
    calibration_min_samples: int = 3           # min feedback rows before trust adjusts the bar
    calibration_max_delta: float = 0.10        # most the bar can move down (trusted) or up (risky)
    # Shadow-mode ML calibrator (LightGBM + SHAP). OFF by default and advisory
    # only: when on, it logs what it WOULD advise next to the rule's decision —
    # the rule still decides. Needs >= this many feedback rows to train, else it
    # declines (an undertrained model is worse than the rule). See
    # services/calibration_ml.py + docs/autonomy-and-learning.md.
    ml_calibration_shadow: bool = False
    ml_calibration_min_samples: int = 20

    # Product codes whose deployed assets are ANALYTICS-ONLY fixtures (synthetic
    # TCO / should-cost datasets) and must never drive procurement demand. They
    # own real cost layers for the TCO / should-cost pages, but they have no
    # sourcing story, so if they leaked into the demand forecast they'd surface as
    # zero-stock, no-lead-time "phantom" requisition lines. Matched by prefix, so
    # the whole synthetic family (TCO-STORAGE, TCO-GPU, …) is excluded at once.
    # Comma-separated + env-overridable so an operator can add a family without a
    # code change. The buyer catalog stays real; analytics tables are untouched.
    procurement_excluded_code_prefixes: str = "TCO-"

    # Demand forecasting — usage-driven projection (all env-overridable).
    demand_horizon_days: int = 90              # how far ahead the forecast projects
    demand_window_days: int = 90               # trailing usage window for the rate
    demand_halflife_days: int = 30             # recency weighting (smaller = more reactive)
    asset_useful_life_days: int = 1095         # ~3y; deployed beyond this is a refresh candidate
    # Forecast estimator: "run_rate" (incumbent), "tsb" (intermittent), or "auto"
    # (classify each SKU and route lumpy ones to TSB). Default stays run_rate
    # until the backtest proves a method wins — see docs/forecast-backtest.md.
    forecast_method: str = "run_rate"
    forecast_tsb_alpha: float = 0.1            # TSB demand-probability smoothing
    forecast_tsb_beta: float = 0.1             # TSB demand-size smoothing
    # Estimator ENGINE for intermittent/lumpy SKUs (those the route sends to TSB):
    #   "builtin"       -> our pure-Python tsb_daily_rate (fast, zero deps, default);
    #   "statsforecast" -> Nixtla CrostonSBA via app/services/forecasting_sf.py
    #                      (~24% lower error on the lumpy tail at scale + conformal
    #                      prediction intervals; CPU-only, ZERO LLM tokens).
    # Smooth/erratic SKUs use run_rate regardless. statsforecast is inert unless
    # selected here. Evidence + rationale: docs/forecast-engine-decision.md.
    forecast_engine: str = "builtin"
    # statsforecast model key for the intermittent route (see forecasting_sf.SF_MODELS).
    forecast_sf_model: str = "croston_sba"

    # Contract document repository (optional per-supplier PDF uploads). Storage is
    # pluggable behind app/services/contract_store.py:
    #   "local" -> filesystem at contract_storage_dir (a mounted Railway volume in
    #              prod). A future client's "s3"/"sap" backend drops in at the same
    #              factory. On prod the dir MUST be persistent or uploads vanish on
    #              redeploy — announce_startup() warns when it can't guarantee that.
    contract_storage_backend: str = "local"
    contract_storage_dir: str = "./var/contracts"
    contract_max_bytes: int = 10 * 1024 * 1024  # 10 MB per contract PDF

    # Service-level safety stock (replaces the burn×lead/2 heuristic).
    service_level: float = 0.95                # default/fallback cycle service level (z≈1.645)
    # ABC classification (Pareto by annualised value) → per-class service level:
    # A items (the vital few by spend) get a higher service level than C items.
    abc_a_threshold: float = 0.80              # top 80% of cumulative value = class A
    abc_b_threshold: float = 0.95              # next 15% = B; remainder = C
    abc_service_level_a: float = 0.98          # protect the high-value few hardest
    abc_service_level_b: float = 0.95
    abc_service_level_c: float = 0.90          # let the trivial many run leaner

    # Demand-recovery policy — sizing a bridge buy / scoring recovery levers when a
    # line will stock out BEFORE its inbound lands. Synthetic defaults for the demo;
    # real per-source values (expedite SLAs, alternate prices) flow in later via the
    # ProductSupplier rows + env, and the policy degrades gracefully when missing.
    recovery_service_level: float = 0.90       # service level for the buffer-rebuild component
    expedite_lead_compression: float = 0.5     # expedite cuts a source's lead time to this ×
    expedite_premium_pct: float = 0.25         # +25% unit cost to expedite the existing PO
    landed_cost_adder_pct: float = 0.12        # duties/freight/insurance on a bridge/alt buy


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
    # Contract uploads need durable storage too. We do NOT hard-fail (the backend
    # boots and creates the dir on demand), but on prod with the local backend the
    # dir must be a mounted persistent volume or uploaded PDFs are lost on redeploy.
    if is_production() and settings.contract_storage_backend == "local":
        print(
            f"[startup] WARNING contract uploads use local storage at "
            f"{settings.contract_storage_dir!r} — ensure this is a PERSISTENT "
            f"Railway volume, or uploaded contracts will be lost on redeploy."
        )
