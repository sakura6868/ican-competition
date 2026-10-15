"""应用配置：全部来自环境变量，禁止硬编码密钥。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根：backend/ 的上一级
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GONGXU_",
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 服务
    app_name: str = "gongxu-zhichuan"
    api_prefix: str = "/api/v1"
    host: str = "127.0.0.1"
    port: int = 8011
    log_level: str = "INFO"

    # 数据库
    database_url: str = "sqlite:///data/gongxu.db"
    db_pool_size: int = 5

    # 会话与安全（生产必须由环境变量覆盖）
    session_secret: str = "dev-only-secret-change-me"  # noqa: S105
    session_ttl_seconds: int = 8 * 3600
    password_min_length: int = 8
    cookie_secure: bool = False  # 生产反向代理终止 TLS 后应设为 True
    login_max_attempts: int = 5
    login_window_seconds: int = 300
    csrf_enabled: bool = True

    # 演示模式（仅 GONGXU_DEMO_FIXTURES=true 时启用 Fixture 演示通道）
    demo_fixtures: bool = False

    # 加密主密钥（生产必须由系统密钥库提供）
    master_key_hex: str = ""

    # 大模型 Provider
    llm_mode: str = "replay"  # replay | live
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # 视觉推理后端（none | onnx | qwen）；默认 none 保持 fail-closed
    vision_backend: str = "none"
    # 实验能力：仅在当前兼容端点通过 image-list video 探测后启用。
    qwen_sequence_input_enabled: bool = False
    # A dedicated in-process practice worker; disable when running practice_worker separately.
    practice_worker_enabled: bool = True
    practice_timeout_seconds: int = 25
    practice_observation_ttl_seconds: int = 45
    practice_max_calls: int = 300
    teaching_window_seconds: int = 30
    teaching_max_windows: int = 40
    asr_model: str = ""  # optional faster-whisper model; empty means visual-only preparation

    # 存储根
    data_root: Path = PROJECT_ROOT / "data"
    model_root: Path = PROJECT_ROOT / "models"

    # 隐私保留
    evidence_retention_days: int = 7
    max_upload_bytes: int = 512 * 1024 * 1024  # 默认 512 MiB

    @property
    def db_path(self) -> Path:
        url = self.database_url
        if not url.startswith("sqlite:///"):
            raise ValueError("仅支持 sqlite 文件库")
        return PROJECT_ROOT / url.removeprefix("sqlite:///")

    @property
    def sqlite_connect_args(self) -> dict[str, Any]:
        return {"check_same_thread": False, "timeout": 30}


@lru_cache
def get_settings() -> Settings:
    return Settings()
