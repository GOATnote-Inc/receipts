"""Alembic environment for the receipts ledger.

Resolves the database URL from DATABASE_URL (matching db.get_database_url) so
tests, dev, and prod stay aligned. Supports both online and offline migration.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make src/ importable when alembic is invoked from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from receipts.ledger import Base  # noqa: E402  (path manipulation must precede import)
from receipts.ledger.db import DEFAULT_DATABASE_URL  # noqa: E402

config = context.config

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        # Some test invocations construct Config(ini_path) but the logging
        # section may be absent; fail open rather than break migrations.
        pass


def _resolve_url() -> str:
    cfg_url = config.get_main_option("sqlalchemy.url") or ""
    if cfg_url and "${DATABASE_URL}" not in cfg_url and cfg_url.strip():
        return cfg_url
    return os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _resolve_url()
    config.set_main_option("sqlalchemy.url", url)
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
