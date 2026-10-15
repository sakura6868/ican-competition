"""视觉推理适配器协议（整改 R5/R7）。

实时与预录共用同一适配器，避免两套判断逻辑（P1-C.4）。
真实 ONNX 模型未安装时，`infer_frame` 抛 `AdapterUnavailableError`，
上层据此结构化拒判，绝不伪造事实。

预训练视觉组件登记（许可/版本/SHA256）见 gongxu/vision/models.py。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.vision.facts import Detection

if TYPE_CHECKING:
    from gongxu.sop.schema import SopStep


class AdapterUnavailableError(Exception):
    """视觉适配器不可用（模型缺失或推理未实现）。"""


class AdapterTransientError(Exception):
    """视觉 Provider 暂时不可用；上层应保留任务并自动重试。"""


class VisionAdapter(ABC):
    """统一视觉推理适配器：帧 → 检测事件。"""

    name: str = "base"

    @abstractmethod
    def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
        """对单帧 RGB 图像做推理，返回检测事件（模型版本由调用方登记）。"""

    def model_version(self) -> str:
        return f"{self.name}@unknown"


class NoVisionAdapter(VisionAdapter):
    """无真实模型时的占位适配器：任何推理请求都明确拒判（fail-closed）。"""

    name = "no-vision-adapter"

    def __init__(self, reason: str = "视觉适配器未实现推理，无法判分（fail-closed）") -> None:
        self.reason = reason

    def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
        raise AdapterUnavailableError(self.reason)


def resolve_adapter(
    db: Session,
    *,
    process_key: str | None = None,
    sop_steps: Sequence[SopStep] | None = None,
) -> VisionAdapter:
    """按 GONGXU_VISION_BACKEND 选择视觉适配器；模型缺失/非法值回退 NoVisionAdapter。

    - onnx：要求 hand + tool_detector 在 model_registry 登记为 available（不依赖词表）。
    - qwen：要求 GONGXU_LLM_* 配置齐全；process_key 决定受控词表。
      已登记工序使用静态词表，教师创建的新工序可从当前 SopVersion.steps
      派生词表。缺少 process_key、未登记且无 SOP 词表时必须 fail-closed，
      绝不回退到另一工序的词表。
    其余值默认 none，保持 fail-closed。
    """
    backend = get_settings().vision_backend
    if backend == "qwen":
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        if not process_key:
            return NoVisionAdapter("缺少当前工序标识，无法加载受控视觉词表（fail-closed）")
        try:
            return QwenVisionAdapter(process_key=process_key, sop_steps=sop_steps)
        except AdapterUnavailableError as exc:
            return NoVisionAdapter(str(exc))
    if backend == "onnx":
        from gongxu.vision.adapters_onnx import OnnxVisionAdapter
        from gongxu.vision.models import list_models

        available = {m.name for m in list_models(db) if m.status.value == "available"}
        if "hand" not in available or "tool_detector" not in available:
            return NoVisionAdapter()
        try:
            return OnnxVisionAdapter()
        except AdapterUnavailableError as exc:
            return NoVisionAdapter(str(exc))
    return NoVisionAdapter()
