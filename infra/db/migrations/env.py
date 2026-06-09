"""Alembic migration environment."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Load .env so that running `alembic upgrade head` directly from the shell
# (without manually exporting DATABASE_URL) works out of the box.
# dotenv searches the current directory and all parents, so this works
# whether you run alembic from the repo root or any subdirectory.
load_dotenv()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the dummy sqlalchemy.url in alembic.ini with the real value
# from the environment. Fail loudly if it's absent rather than producing
# a confusing SQLAlchemy error later in the migration.
database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is not set. "
        "Copy .env.example to .env, fill in POSTGRES_PASSWORD, and ensure "
        "DATABASE_URL matches. Then re-run: alembic upgrade head"
    )
config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
