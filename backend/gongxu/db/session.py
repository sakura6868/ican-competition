"""数据库引擎与会话。"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from gongxu.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_LEGACY_CREATE_ALL_REVISION = "b7e1c8a92d4f"


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{settings.db_path}",
            connect_args=settings.sqlite_connect_args,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(settings), expire_on_commit=False)
    return _session_factory


def get_db() -> Generator[Session, None, None]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


def _adopt_legacy_create_all_database(config: Config, settings: Settings | None) -> None:
    """接管旧版 ``create_all`` 生成、但没有 Alembic 版本的完整数据库。"""
    from gongxu.db import models  # noqa: F401  注册全部表元数据

    resolved = settings or get_settings()
    resolved.db_path.parent.mkdir(parents=True, exist_ok=True)
    inspection_engine = create_engine(
        f"sqlite:///{resolved.db_path}",
        connect_args=resolved.sqlite_connect_args,
    )
    try:
        with inspection_engine.connect() as connection:
            table_names = set(connection.dialect.get_table_names(connection))
            if not table_names:
                return
            if "alembic_version" in table_names:
                recorded_revision = connection.execute(
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                ).scalar_one_or_none()
                if recorded_revision:
                    return
            context = MigrationContext.configure(connection)
            differences: list[Any] = compare_metadata(context, Base.metadata)
            # Retired startup predates practice tables. Only additions for these exact
            # tables may differ; all historical tables must still match fully.
            practice_tables = {"practice_sessions", "practice_observations", "practice_events"}
            differences = [
                d
                for d in differences
                if not (
                    d[0] == "add_table"
                    and d[1].name in practice_tables
                    or d[0] == "add_index"
                    and d[1].table.name in practice_tables
                )
            ]
    finally:
        inspection_engine.dispose()

    if differences:
        raise RuntimeError(
            "检测到没有 Alembic 版本记录且结构不完整的旧数据库；"
            "为避免损坏数据，已拒绝自动登记，请先备份并执行兼容迁移。"
        )
    # Stamp the exact schema revision represented by the retired create_all
    # startup. Future data-only migrations must still execute normally.
    command.stamp(config, _LEGACY_CREATE_ALL_REVISION)


def init_db(settings: Settings | None = None) -> None:
    """将数据库迁移到当前代码所需的 Alembic 版本。

    这里保留 ``init_db`` 名称以兼容现有 CLI，但不再使用 ``create_all``。
    ``create_all`` 不会更新已有表，曾导致数据库实体与 Alembic 版本漂移。
    """

    alembic_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    config = Config(str(alembic_ini))
    if settings is not None:
        config.attributes["gongxu_settings"] = settings
    _adopt_legacy_create_all_database(config, settings)
    command.upgrade(config, "head")
