"""迁移门禁测试：空库 upgrade、重复 upgrade（幂等）、downgrade/upgrade 往返。

验证 handoff 阶段 2 门禁「迁移可在空库和升级库运行」。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

import gongxu.db.session as session_module
from gongxu.config import get_settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _run_alembic(db_file: Path, *args: str) -> None:
    os.environ["GONGXU_DATABASE_URL"] = f"sqlite:///{db_file}"
    get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # 幂等：重复 upgrade 不报错
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")  # 往返后恢复


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_migrations_empty_and_upgraded_db(tmp_path: Path, attempt: int) -> None:
    """每次用全新空库跑迁移，验证可重复创建。"""
    db_file = tmp_path / f"migrate-{attempt}.db"
    _run_alembic(db_file)
    assert db_file.exists()


def test_migrations_on_upgraded_db(tmp_path: Path) -> None:
    """在已升级库上再次跑 upgrade 应为 no-op（幂等门禁）。"""
    db_file = tmp_path / "once.db"
    os.environ["GONGXU_DATABASE_URL"] = f"sqlite:///{db_file}"
    get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # 已在 head，再次升级不报错
    assert db_file.exists()


def test_migrations_preserve_application_loggers(tmp_path: Path) -> None:
    """Running Alembic in-process must not silence inference fallback warnings."""
    application_logger = logging.getLogger("gongxu.vision.pipeline")
    application_logger.disabled = False

    _run_alembic(tmp_path / "logging.db")

    assert application_logger.disabled is False


def test_observable_check_results_migration_backfills_and_round_trips(tmp_path: Path) -> None:
    db_file = tmp_path / "observable-check-results.db"
    os.environ["GONGXU_DATABASE_URL"] = f"sqlite:///{db_file}"
    get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "f014cb9142f8")

    with sqlite3.connect(db_file) as connection:
        connection.execute(
            """
            INSERT INTO assessment_sessions (
                id, mode, process_key, status, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("legacy-session", "recorded", "legacy-process", "completed", "2026-08-24"),
        )
        connection.execute(
            """
            INSERT INTO step_assessments (
                session_id, step_index, step_id, judgement, start_ms, end_ms,
                rule_code, reason, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-session",
                1,
                "legacy-step",
                "passed",
                0,
                1000,
                "observed_checks_passed",
                "legacy row",
                0.9,
                "2026-08-24",
            ),
        )
        connection.commit()

    command.upgrade(cfg, "head")
    with sqlite3.connect(db_file) as connection:
        raw = connection.execute(
            "SELECT observable_check_results FROM step_assessments WHERE step_id = ?",
            ("legacy-step",),
        ).fetchone()
    assert raw is not None
    assert json.loads(raw[0]) == {}

    command.downgrade(cfg, "f014cb9142f8")
    with sqlite3.connect(db_file) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(step_assessments)")}
    assert "observable_check_results" not in columns

    command.upgrade(cfg, "head")
    with sqlite3.connect(db_file) as connection:
        raw_after_round_trip = connection.execute(
            "SELECT observable_check_results FROM step_assessments WHERE step_id = ?",
            ("legacy-step",),
        ).fetchone()
    assert raw_after_round_trip is not None
    assert json.loads(raw_after_round_trip[0]) == {}


def test_upgrade_accepts_latest_metadata_created_by_legacy_startup(
    tmp_path: Path,
) -> None:
    """旧版 create_all 提前建新表时，迁移应验证结构并接管版本。"""
    db_file = tmp_path / "legacy-create-all-drift.db"
    os.environ["GONGXU_DATABASE_URL"] = f"sqlite:///{db_file}"
    get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "26e2aa3871e6")

    with sqlite3.connect(db_file) as connection:
        connection.execute(
            """
            CREATE TABLE process_vocabularies (
                process_key VARCHAR(128) NOT NULL PRIMARY KEY,
                tools JSON NOT NULL,
                objects JSON NOT NULL,
                actions JSON NOT NULL,
                approved BOOLEAN NOT NULL,
                source VARCHAR(32) NOT NULL,
                created_by VARCHAR(32),
                updated_by VARCHAR(32),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            """
            ALTER TABLE step_assessments
            ADD COLUMN observable_check_results JSON NOT NULL DEFAULT '{}'
            """
        )
        connection.commit()

    command.upgrade(cfg, "head")

    with sqlite3.connect(db_file) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(process_vocabularies)")}
        assessment_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(step_assessments)")
        }

    assert revision == (ScriptDirectory.from_config(cfg).get_current_head(),)
    assert "ix_process_vocabularies_approved" in indexes
    assert "observable_check_results" in assessment_columns


def test_init_db_uses_absolute_alembic_config_from_any_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gongxu.db.session import init_db

    db_file = tmp_path / "portable-init.db"
    monkeypatch.setenv("GONGXU_DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None

    init_db()

    with sqlite3.connect(db_file) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision == (ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head(),)


def test_init_db_adopts_only_complete_unversioned_create_all_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gongxu.db import models  # noqa: F401
    from gongxu.db.session import Base, init_db

    db_file = tmp_path / "legacy-complete.db"
    monkeypatch.setenv("GONGXU_DATABASE_URL", f"sqlite:///{db_file}")
    get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()

    init_db()

    with sqlite3.connect(db_file) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision == (ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head(),)


def test_init_db_adopts_complete_database_with_empty_version_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gongxu.db import models  # noqa: F401
    from gongxu.db.session import Base, init_db

    db_file = tmp_path / "legacy-empty-version.db"
    monkeypatch.setenv("GONGXU_DATABASE_URL", f"sqlite:///{db_file}")
    get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.commit()

    init_db()

    with sqlite3.connect(db_file) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision == (ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head(),)


def test_init_db_rejects_partial_unversioned_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gongxu.db.session import init_db

    db_file = tmp_path / "legacy-partial.db"
    with sqlite3.connect(db_file) as connection:
        connection.execute("CREATE TABLE users (id VARCHAR(32) PRIMARY KEY)")
        connection.commit()
    monkeypatch.setenv("GONGXU_DATABASE_URL", f"sqlite:///{db_file}")
    get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None

    with pytest.raises(RuntimeError, match="结构不完整"):
        init_db()

    with sqlite3.connect(db_file) as connection:
        assert "alembic_version" not in {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def test_init_db_rejects_partial_database_with_empty_version_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gongxu.db.session import init_db

    db_file = tmp_path / "legacy-partial-empty-version.db"
    with sqlite3.connect(db_file) as connection:
        connection.execute("CREATE TABLE users (id VARCHAR(32) PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.commit()
    monkeypatch.setenv("GONGXU_DATABASE_URL", f"sqlite:///{db_file}")
    get_settings.cache_clear()
    session_module._engine = None
    session_module._session_factory = None

    with pytest.raises(RuntimeError, match="结构不完整"):
        init_db()

    with sqlite3.connect(db_file) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == []
