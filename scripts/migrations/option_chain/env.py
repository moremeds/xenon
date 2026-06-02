"""Alembic env for option_chain DB.

Separate alembic environment from xenon's main DB because:
  - Different owner (option_chain_writer, not xenon_prod)
  - Different DB (option_chain, not core_dev / core_test)
  - TimescaleDB-specific operations

Connection URL comes from OPTION_CHAIN_DATABASE_URL env var. The
sqlalchemy.url in alembic.ini is a placeholder only — `get_url()` below
is authoritative.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


def get_url() -> str:
    url = os.environ.get("OPTION_CHAIN_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "OPTION_CHAIN_DATABASE_URL not set. Example: "
            "postgresql://option_chain_writer:<pw>@127.0.0.1:5432/option_chain"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
