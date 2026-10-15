"""pytest 全局 fixture：隔离的 SQLite 测试库。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

import gongxu.db.session as session_module
from gongxu.config import get_settings
from gongxu.db import models  # noqa: F401
from gongxu.db.session import Base, get_session_factory

# Historical dataset-ingestion tests were shipped without their corresponding
# gongxu.data implementations. They are retained as design evidence but are not
# part of the current teacher-to-learner web product test suite.
collect_ignore = [
    "test_assembly101.py",
    "test_card.py",
    "test_data.py",
    "test_data_audit.py",
    "test_data_prepare.py",
    "test_impact_adapter.py",
    "test_industreal.py",
    "test_industreal_adapter.py",
    "test_industreal_benchmark.py",
    "test_trials.py",
]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """所有测试自动获得隔离环境变量（主密钥、数据根、安全配置）。

    CSRF 在大部分测试中关闭，由 tests/security/test_csrf.py 单独开启验证。
    """
    monkeypatch.setenv("GONGXU_MASTER_KEY_HEX", "00" * 32)
    monkeypatch.setenv("GONGXU_DATA_ROOT", str(tmp_path / "gxdata"))
    monkeypatch.setenv("GONGXU_CSRF_ENABLED", "false")
    monkeypatch.setenv("GONGXU_COOKIE_SECURE", "false")
    monkeypatch.setenv("GONGXU_DEMO_FIXTURES", "false")
    monkeypatch.setenv("GONGXU_LLM_MODE", "replay")  # 测试不依赖开发 .env 的 live 配置
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "none")
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """每个测试前清空登录限流状态（进程内全局 dict 跨测试泄漏）。"""
    from gongxu.security.ratelimit import _attempts

    _attempts.clear()
    yield


@pytest.fixture()
def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """为每个测试重建独立数据库，避免状态泄漏。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("GONGXU_DATABASE_URL", f"sqlite:///{db_file}")

    # 重置全局引擎/会话工厂，使下一次取值使用新配置
    session_module._engine = None
    session_module._session_factory = None
    get_settings.cache_clear()

    factory = get_session_factory()
    Base.metadata.create_all(bind=session_module.get_engine())
    yield factory
    get_settings.cache_clear()


@pytest.fixture()
def db(test_db) -> Session:
    session = test_db()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def video_asset(db: Session) -> str:
    """注册一个真实可抽帧的小视频资产（供 SOP 编译/考评测试绑定）。"""
    import subprocess

    from gongxu.assets import service as asset_service
    from gongxu.media.video import FFmpegNotFoundError, _ffmpeg_bin

    try:
        ffmpeg = _ffmpeg_bin()
    except FFmpegNotFoundError:
        pytest.skip("无 ffmpeg，SOP 编译测试需要真实视频")
    video = Path(get_settings().data_root) / "runtime" / "ref.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(  # noqa: S603  # 固定 ffmpeg 参数
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=160x120:rate=5",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(video),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not video.exists():
        pytest.skip("无法生成测试视频")
    return asset_service.register_asset(
        db,
        kind="video",
        filename="ref.mp4",
        temp_path=video,
        license_note="test",
        allow_provider_export=True,  # SOP 编译测试需显式外发授权（整改 P1-1）
    ).id
