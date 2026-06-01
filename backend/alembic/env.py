"""Alembic environment.

Wired to the application so migrations never drift from the code:
  - the database URL comes from ``app.core.config.settings`` (env / .env),
    NOT from a hardcoded value in alembic.ini;
  - ``target_metadata`` is the app's ``Base.metadata`` with every model
    imported, so ``--autogenerate`` sees the full schema.
"""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

from app.core.config import settings
from app.core.db import Base
import app.models  # noqa: F401 -- registers every table on Base.metadata

# Alembic Config object, providing access to values in alembic.ini.
config = context.config

# Override the .ini URL with the application's configured URL so the same
# DATABASE_URL drives both the app and migrations.
config.set_main_option("sqlalchemy.url", settings.database_url)

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# SQLite needs batch mode to ALTER tables (it has no native ALTER); harmless
# elsewhere, so we detect the dialect at runtime.
render_as_batch = settings.database_url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DBAPI needed)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=render_as_batch,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=render_as_batch,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
