"""ReplayProvider：离线确定性 Provider。

所有方法返回固定 fixture，输入摘要与响应摘要确定性计算，仅供显式契约测试与
工程回放。正常视频到 SOP 路径禁止使用本 Provider。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from gongxu.providers.base import (
    MultimodalProvider,
    ProviderReceipt,
    ReferenceSegment,
)
from gongxu.sop.fixtures import PROCESS_KEY, PROMPT_VERSION, grinder_disassembly_sop

REPLAY_MODEL_ID = "replay-fixture-1"


def _digest(data: object) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


class ReplayProvider(MultimodalProvider):
    name = "replay"
    mode = "replay"

    def probe(self) -> dict[str, Any]:
        return {
            "ok": True,
            "provider": self.name,
            "mode": self.mode,
            "model_id": REPLAY_MODEL_ID,
            "supports_vision": True,
            "note": "离线 fixture，无真实模型调用",
        }

    def analyze_reference_segment(
        self, segment: ReferenceSegment
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        start = time.perf_counter()
        analysis = {
            "segment_id": segment.segment_id,
            "process_key": PROCESS_KEY,
            "steps_found": 7,
            "key_tools": ["wrench", "screwdriver", "grinding_wheel"],
            "safety_requirements": ["unplug_power", "wait_wheel_stop", "wear_ppe"],
            "keyframe_timestamps_ms": list(segment.keyframe_timestamps_ms),
        }
        receipt = ProviderReceipt(
            provider=self.name,
            mode=self.mode,
            model_id=REPLAY_MODEL_ID,
            prompt_version=PROMPT_VERSION,
            input_digest=_digest(
                {
                    "kf": segment.keyframes,
                    "timestamps_ms": segment.keyframe_timestamps_ms,
                    "asr": segment.asr_text,
                }
            ),
            response_digest=_digest(analysis),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return analysis, receipt

    def compile_sop(
        self, analysis: dict[str, Any], rag_chunks: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        start = time.perf_counter()
        sop = grinder_disassembly_sop()
        result = sop.model_dump(mode="json")
        receipt = ProviderReceipt(
            provider=self.name,
            mode=self.mode,
            model_id=REPLAY_MODEL_ID,
            prompt_version=PROMPT_VERSION,
            input_digest=_digest({"analysis": analysis, "rag": rag_chunks}),
            response_digest=_digest(result),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return result, receipt

    def explain_assessment(
        self, step_results: list[dict[str, Any]], evidence: dict[str, Any]
    ) -> tuple[str, ProviderReceipt]:
        start = time.perf_counter()
        explanation = (
            "（Replay 解释）漏项与错序扣分已绑定证据时间戳；复练建议：重新按标准视频"
            "重点练习前置步骤与工具使用。该解释仅用于演示，不代表真实模型输出。"
        )
        receipt = ProviderReceipt(
            provider=self.name,
            mode=self.mode,
            model_id=REPLAY_MODEL_ID,
            prompt_version=PROMPT_VERSION,
            input_digest=_digest({"steps": step_results, "evidence": evidence}),
            response_digest=_digest(explanation),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return explanation, receipt

    def generate_practice_plan(
        self, report: dict[str, Any]
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        start = time.perf_counter()
        plan = {
            "focus_steps": ["step_05"],
            "repeat_count": 3,
            "note": "Replay 生成的复练计划，仅用于流程验证",
        }
        receipt = ProviderReceipt(
            provider=self.name,
            mode=self.mode,
            model_id=REPLAY_MODEL_ID,
            prompt_version=PROMPT_VERSION,
            input_digest=_digest(report),
            response_digest=_digest(plan),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return plan, receipt
