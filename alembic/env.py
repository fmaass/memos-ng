import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.database import Base

# Import models so their tables register on Base.metadata for autogenerate.
# (Models land in T1.1; this import is a no-op until then.)
try:
    import app.models  # noqa: F401
except ModuleNotFoundError:
    pass

config = context.config

if config.config_file_name is not None:
    # env.py also loads inside the app process, whose loggers exist by then and carry the privacy
    # filters (app/logging_privacy.py). fileConfig's default would disable every one of them.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

database_url = os.environ.get("DATABASE_URL_SYNC", config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = database_url
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
