"""多模态 Provider 接口（handoff §8）。

大模型只负责语义理解、SOP 编译和教学解释；分数由规则引擎计算，Provider 不得修改数值。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderReceipt:
    """一次 Provider 调用的完整记录（用于可复现性与审计）。

    禁止记录：API Key、完整私密提示、未脱敏的原始帧。
    """

    provider: str
    mode: str  # replay | live
    model_id: str
    prompt_version: str
    input_digest: str
    response_digest: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReferenceSegment:
    """标准教学视频片段的最小输入（禁止发送完整原视频）。"""

    segment_id: str
    keyframes: list[str]  # 去重关键帧（已模糊人脸、去 EXIF）
    asr_text: str = ""
    ocr_text: str = ""
    detection_summary: dict[str, Any] = field(default_factory=dict)
    rag_chunks: list[dict[str, Any]] = field(default_factory=list)  # 至多 6 个 RAG 片段
    start_ms: int = 0
    end_ms: int = 0
    # 与 keyframes 同序的源视频全局时间；放在末尾保持旧位置参数兼容。
    keyframe_timestamps_ms: list[int] = field(default_factory=list)


class ProviderError(Exception):
    """Provider 调用失败基类。"""


class ProviderTimeoutError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderBudgetExceededError(ProviderError):
    pass


class ProviderVisionUnsupportedError(ProviderError):
    """当前模型/端点不支持视觉输入（如 vision 能力未开通）。"""


class MultimodalProvider(ABC):
    """所有外部多模态模型的统一接口。"""

    name: str = "base"
    mode: str = "replay"  # 子类覆盖

    @abstractmethod
    def probe(self) -> dict[str, Any]:
        """能力探测：验证连接、模型可用性与视觉输入支持。"""

    @abstractmethod
    def analyze_reference_segment(
        self, segment: ReferenceSegment
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        """理解单个标准视频片段，返回结构化分析。"""

    @abstractmethod
    def compile_sop(
        self, analysis: dict[str, Any], rag_chunks: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        """将分析与手册证据编译为 SOP JSON。"""

    @abstractmethod
    def explain_assessment(
        self, step_results: list[dict[str, Any]], evidence: dict[str, Any]
    ) -> tuple[str, ProviderReceipt]:
        """生成扣分项的文本解释与复练建议；不得修改分数。"""

    @abstractmethod
    def generate_practice_plan(
        self, report: dict[str, Any]
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        """基于考评报告生成复练计划。"""
