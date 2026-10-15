"""Provider factory with an explicit boundary between real and fixture modes."""

from __future__ import annotations

from gongxu.config import get_settings
from gongxu.providers.base import MultimodalProvider, ProviderError
from gongxu.providers.replay import ReplayProvider


def get_provider(*, allow_replay: bool = False) -> MultimodalProvider:
    """Return the configured Provider.

    Replay emits a fixed engineering fixture and must never be selected by a
    normal video-to-SOP request.  Callers such as the contract-test CLI have to
    opt in explicitly and must label the result as replay data.
    """
    settings = get_settings()
    if settings.llm_mode == "live":
        from gongxu.providers.qwen import Qwen38MaxProvider

        return Qwen38MaxProvider()
    if settings.llm_mode == "replay" and allow_replay:
        return ReplayProvider()
    if settings.llm_mode == "replay":
        raise ProviderError(
            "当前为 replay 工程回放模式，不能分析真实标准视频；"
            "请配置 live 多模态 Provider 后再生成 SOP"
        )
    raise ProviderError(f"不支持的 Provider 模式: {settings.llm_mode}")
