"""Alembic 迁移环境：连接到 gongxu 的 settings 与 Base metadata。"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from gongxu.config import get_settings
from gongxu.db import models  # noqa: F401  注册全部模型
from gongxu.db.session import Base

config = context.config

if config.config_file_name is not None:
    # Alembic may run in the same process as tests or maintenance commands.
    # Preserve already-created application loggers so inference fallbacks and
    # privacy cleanup warnings remain observable after a migration.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _migration_url() -> str:
    settings = config.attributes.get("gongxu_settings") or get_settings()
    # 与运行时一致：使用解析后的绝对路径
    return f"sqlite:///{settings.db_path}"


def run_migrations_offline() -> None:
    context.configure(
        url=_migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _migration_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
