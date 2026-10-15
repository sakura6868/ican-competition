"""Qwen38MaxProvider：外部多模态模型（OpenAI 兼容接口）。

凭据来自环境变量（GONGXU_LLM_*），禁止硬编码。真实 URL/模型/Key 由用户提供；
缺失时正常业务可信拒绝，不会静默切换到固定 Replay 输出。

注意：不预设供应商一定完全兼容 OpenAI 字段；结构化输出需在业务侧再做 Schema 校验。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

import httpx

from gongxu.config import get_settings
from gongxu.providers.base import (
    MultimodalProvider,
    ProviderBudgetExceededError,
    ProviderError,
    ProviderRateLimitError,
    ProviderReceipt,
    ProviderTimeoutError,
    ReferenceSegment,
)

_PROMPT_VERSION = "sop-compile-v3"
_MAX_RETRIES = 3
_MAX_REFERENCE_FRAMES = 24
_MAX_FRAMES_PER_BATCH = 6

_CANDIDATE_KEYS: dict[str, tuple[str, ...]] = {
    "step_candidates": ("step_candidates", "steps"),
    "tool_candidates": ("tool_candidates", "tools", "key_tools"),
    "object_candidates": ("object_candidates", "objects", "key_objects"),
    "action_candidates": ("action_candidates", "actions", "key_actions"),
}
ReferenceProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class _ChatMetadata:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    request_id: str = ""


def _usage_count(usage: object, *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _uniform_sample_indices(total: int, limit: int) -> list[int]:
    """Select an ordered full-span subset, always retaining first and last."""
    if total <= 0 or limit <= 0:
        return []
    if total <= limit:
        return list(range(total))
    if limit == 1:
        return [0]
    return [round(i * (total - 1) / (limit - 1)) for i in range(limit)]


def _frame_timestamp_ms(segment: ReferenceSegment, frame_index: int) -> int | None:
    if frame_index < len(segment.keyframe_timestamps_ms):
        return max(0, int(segment.keyframe_timestamps_ms[frame_index]))
    if segment.end_ms > segment.start_ms and len(segment.keyframes) > 1:
        fraction = frame_index / (len(segment.keyframes) - 1)
        return round(segment.start_ms + fraction * (segment.end_ms - segment.start_ms))
    return None


def _chunks[T](items: list[T], size: int) -> list[list[T]]:
    return [items[offset : offset + size] for offset in range(0, len(items), size)]


def _first_candidate_list(analysis: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    """Read the first supported candidate field without duplicating model aliases."""
    for key in keys:
        value = analysis.get(key)
        if isinstance(value, list):
            return value
    return []


def _candidate_frame(
    frames: list[dict[str, int | None]], candidate_index: int, candidate_count: int
) -> dict[str, int | None]:
    if not frames:
        return {"source_frame_index": None, "timestamp_ms": None}
    if candidate_count <= 1:
        return frames[0]
    frame_index = round(candidate_index * (len(frames) - 1) / (candidate_count - 1))
    return frames[frame_index]


def _normalize_candidate(
    value: Any,
    *,
    batch_index: int,
    candidate_index: int,
    candidate_count: int,
    frames: list[dict[str, int | None]],
) -> dict[str, Any]:
    candidate = dict(value) if isinstance(value, dict) else {"name": str(value)}
    fallback = _candidate_frame(frames, candidate_index, candidate_count)

    timestamp = candidate.get("timestamp_ms")
    if timestamp is None:
        for key in ("start_ms", "time_ms", "evidence_timestamp_ms"):
            if candidate.get(key) is not None:
                timestamp = candidate[key]
                break
    if timestamp is None:
        timestamp = fallback["timestamp_ms"]

    candidate.setdefault("batch_index", batch_index)
    candidate.setdefault("source_order", candidate_index)
    candidate.setdefault("source_frame_index", fallback["source_frame_index"])
    candidate.setdefault("timestamp_ms", timestamp)
    return candidate


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, int]:
    timestamp = candidate.get("timestamp_ms")
    try:
        timestamp_value = int(timestamp) if timestamp is not None else 2**63 - 1
    except (TypeError, ValueError):
        timestamp_value = 2**63 - 1
    return (
        timestamp_value,
        int(candidate.get("batch_index", 0)),
        int(candidate.get("source_order", 0)),
    )


def _aggregate_batch_analyses(
    segment: ReferenceSegment,
    results: list[tuple[list[dict[str, int | None]], dict[str, Any], int]],
) -> dict[str, Any]:
    """Preserve every batch and expose deterministic time-ordered candidates."""
    batch_analyses: list[dict[str, Any]] = []
    aggregated_candidates: dict[str, list[dict[str, Any]]] = {name: [] for name in _CANDIDATE_KEYS}

    for batch_index, (frames, analysis, latency_ms) in enumerate(results):
        batch_analyses.append(
            {
                "batch_index": batch_index,
                "batch_number": batch_index + 1,
                "frames": frames,
                "latency_ms": latency_ms,
                "analysis": analysis,
            }
        )
        for output_key, aliases in _CANDIDATE_KEYS.items():
            values = _first_candidate_list(analysis, aliases)
            aggregated_candidates[output_key].extend(
                _normalize_candidate(
                    value,
                    batch_index=batch_index,
                    candidate_index=candidate_index,
                    candidate_count=len(values),
                    frames=frames,
                )
                for candidate_index, value in enumerate(values)
            )

    # Keep the one-call response backwards compatible while adding the aggregate contract.
    aggregate = dict(results[0][1]) if len(results) == 1 else {}
    aggregate.update(
        {
            "segment_id": segment.segment_id,
            "analysis_format": "timeline_batches_v1",
            "batch_count": len(results),
            "batch_analyses": batch_analyses,
        }
    )
    for output_key, candidates in aggregated_candidates.items():
        aggregate[output_key] = sorted(candidates, key=_candidate_sort_key)
    return aggregate


def _compact_teaching_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """Omit the duplicate raw batch only when its facts are already at the top level."""
    if analysis.get("analysis_format") != "teaching_windows_v1":
        return analysis
    windows = []
    for window in analysis.get("window_analyses", []):
        facts = window.get("analysis", {})
        if facts.get("batch_count") == 1:
            facts = {key: value for key, value in facts.items() if key != "batch_analyses"}
        windows.append({**window, "analysis": facts})
    return {**analysis, "window_analyses": windows}


class Qwen38MaxProvider(MultimodalProvider):
    name = "qwen38max"
    mode = "live"

    def __init__(self) -> None:
        settings = get_settings()
        if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
            raise ProviderError("缺少 GONGXU_LLM_BASE_URL/API_KEY/MODEL 配置")
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model
        self._reference_progress: ReferenceProgressCallback | None = None
        self._last_chat_metadata = _ChatMetadata()

    def set_reference_progress_callback(self, callback: ReferenceProgressCallback | None) -> None:
        """Attach a job-local batch progress sink; it never changes model output."""
        self._reference_progress = callback

    def _chat_with_metadata(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 2000,
        timeout_seconds: float = 90.0,
        attempts: int = _MAX_RETRIES,
    ) -> tuple[dict[str, Any], _ChatMetadata]:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            # qwen3.8-max 为推理模型：关闭 thinking 才能直接输出结构化 JSON
            "enable_thinking": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_error: Exception | None = None
        for _attempt in range(attempts):
            try:
                resp = httpx.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=httpx.Timeout(timeout_seconds),
                    trust_env=False,
                )
            except httpx.TimeoutException:
                last_error = ProviderTimeoutError("Provider 请求超时")
                continue
            except httpx.HTTPError as exc:
                last_error = ProviderError(f"Provider 连接失败: {exc}")
                continue
            if resp.status_code == 429:
                raise ProviderRateLimitError("Provider 限流")
            if resp.status_code == 402 or resp.status_code == 403:
                raise ProviderBudgetExceededError("Provider 预算或授权不足")
            if resp.status_code >= 500:
                last_error = ProviderError(f"Provider 服务端错误 {resp.status_code}")
                continue
            if resp.status_code == 400:
                # 模型能力不支持（如 vision 未开通）等客户端错误：带响应体诊断
                body = resp.text[:400]
                raise ProviderError(f"Provider 拒绝请求(400): {body}")
            if 400 <= resp.status_code < 500:
                body = resp.text[:400]
                raise ProviderError(f"Provider 请求被拒绝({resp.status_code}): {body}")
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            try:
                result = cast(dict[str, Any], json.loads(content))
            except json.JSONDecodeError:
                last_error = ProviderError("Provider 返回非 JSON")
                continue
            usage = data.get("usage", {})
            input_tokens = _usage_count(usage, "prompt_tokens", "input_tokens")
            output_tokens = _usage_count(usage, "completion_tokens", "output_tokens")
            total_tokens = _usage_count(usage, "total_tokens") or input_tokens + output_tokens
            return result, _ChatMetadata(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                request_id=resp.headers.get("x-request-id", "")[:160],
            )
        raise ProviderError(f"Provider 重试 {attempts} 次后仍失败: {last_error}")

    def _chat(self, messages: list[dict[str, Any]], *, max_tokens: int = 2000) -> dict[str, Any]:
        result, metadata = self._chat_with_metadata(messages, max_tokens=max_tokens)
        self._last_chat_metadata = metadata
        return result

    def last_chat_metadata(self) -> dict[str, int | str]:
        """Return non-secret telemetry for the most recent successful call."""

        return {
            "input_tokens": self._last_chat_metadata.input_tokens,
            "output_tokens": self._last_chat_metadata.output_tokens,
            "total_tokens": self._last_chat_metadata.total_tokens,
            "request_id": self._last_chat_metadata.request_id,
        }

    def probe(self) -> dict[str, Any]:
        """能力探测：验证连接与模型可用性（真实凭据下运行）。"""
        from PIL import Image

        image = Image.new("RGB", (32, 24), (24, 176, 88))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=80)
        image_uri = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        result, metadata = self._chat_with_metadata(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "这是多模态能力探测。只根据图片回答 JSON："
                                '{"ok":true,"image_visible":true,'
                                '"dominant_color":"green|red|blue|unknown"}'
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": image_uri}},
                    ],
                }
            ],
            max_tokens=80,
        )
        vision_ok = bool(result.get("image_visible")) and result.get("dominant_color") == "green"
        return {
            "ok": bool(result.get("ok")) and vision_ok,
            "vision_ok": vision_ok,
            "provider": self.name,
            "mode": self.mode,
            "model_id": self.model,
            "base_url": self.base_url,
            "usage": {
                "input_tokens": metadata.input_tokens,
                "output_tokens": metadata.output_tokens,
                "total_tokens": metadata.total_tokens,
            },
        }

    def analyze_reference_segment(
        self, segment: ReferenceSegment
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        start = time.perf_counter()
        # 出站内容最小化 + 真正发送关键帧图像（整改报告 P1-A.5）：
        # keyframes 为已授权标准教学资产的关键帧（出站最小化，见 sop/service 出站门禁）；
        # 转 base64 data URI。说明：本版本不做面部检测，仅保证最小化+不保留 EXIF，
        # 人脸/铭牌脱敏依赖调用方在授权登记时显式同意（allow_provider_export）。
        selected_indices = _uniform_sample_indices(len(segment.keyframes), _MAX_REFERENCE_FRAMES)
        prepared_frames: list[tuple[int, int | None, str]] = []
        for frame_index in selected_indices:
            # 降分辨率出站最小化（避免大 payload 连接中断，亦为隐私最佳实践）
            uri = _frame_to_data_uri(
                segment.keyframes[frame_index],
                max_width=960 if segment.detection_summary.get("teaching_window") else 480,
            )
            if uri:
                prepared_frames.append(
                    (frame_index, _frame_timestamp_ms(segment, frame_index), uri)
                )

        frame_batches = _chunks(prepared_frames, _MAX_FRAMES_PER_BATCH) or [[]]
        batch_results: list[tuple[list[dict[str, int | None]], dict[str, Any], int]] = []
        input_tokens = 0
        output_tokens = 0
        request_ids: list[str] = []
        for batch_index, frame_batch in enumerate(frame_batches):
            frame_metadata = [
                {
                    "image_order": image_order,
                    "source_frame_index": frame_index,
                    "timestamp_ms": timestamp_ms,
                }
                for image_order, (frame_index, timestamp_ms, _uri) in enumerate(frame_batch)
            ]
            content: list[dict[str, Any]] = []
            text_part = json.dumps(
                {
                    "task": "analyze_reference_segment",
                    "segment_id": segment.segment_id,
                    "batch_index": batch_index,
                    "batch_number": batch_index + 1,
                    "batch_count": len(frame_batches),
                    "keyframe_count": len(segment.keyframes),
                    "keyframes_sent": frame_metadata,
                    "timeline_instruction": (
                        "本批图像是整段视频中连续的时间片，按 image_order 排列；"
                        "source_frame_index 和 timestamp_ms 均来自源视频。"
                        "识别步骤时必须引用真实 timestamp_ms，不能把图像顺序号当秒数。"
                        "只报告本批可见事实，不补写其他批次内容。"
                    ),
                    "asr": segment.asr_text[:2000],
                    "window_range": {"start_ms": segment.start_ms, "end_ms": segment.end_ms},
                    "teaching_rule": (
                        "若 detection.teaching_window=true，"
                        "识别本窗口中的动作、前后状态及步骤边界。"
                        "step_candidates 额外返回 start_ms/end_ms/instruction/completion_checks。"
                        "标注讲解、重复示范和未确定动作，不猜测隐藏的完成质量。"
                        "完成条件应描述被操作对象的结果，不得要求模仿讲解员的蹲姿、"
                        "面向镜头、视线或讲解手势。"
                    ),
                    "ocr": segment.ocr_text[:2000],
                    "detection": segment.detection_summary,
                    "rag": segment.rag_chunks[:6],
                    # response_format=json_object 要求 prompt 含 json 字样
                    "output_schema": {
                        "format": "JSON",
                        "step_candidates": [
                            {
                                "name": "步骤名",
                                "timestamp_ms": "引用本批真实时间，整数",
                                "evidence_refs": ["video@<毫秒>ms"],
                            }
                        ],
                        "tool_candidates": [],
                        "object_candidates": [],
                        "action_candidates": [],
                        "safety_requirements": [],
                    },
                },
                ensure_ascii=False,
            )
            content.append({"type": "text", "text": text_part})
            for _frame_index, _timestamp_ms, uri in frame_batch:
                content.append({"type": "image_url", "image_url": {"url": uri}})

            batch_start = time.perf_counter()
            result, metadata = self._chat_with_metadata(
                [{"role": "user", "content": content}], max_tokens=3000
            )
            input_tokens += metadata.input_tokens
            output_tokens += metadata.output_tokens
            if metadata.request_id:
                request_ids.append(metadata.request_id)
            batch_results.append(
                (frame_metadata, result, int((time.perf_counter() - batch_start) * 1000))
            )
            if self._reference_progress is not None:
                self._reference_progress(batch_index + 1, len(frame_batches))

        analysis = _aggregate_batch_analyses(segment, batch_results)
        receipt = ProviderReceipt(
            provider=self.name,
            mode=self.mode,
            model_id=self.model,
            prompt_version=_PROMPT_VERSION,
            input_digest=_digest(
                {
                    "keyframes": segment.keyframes,
                    "timestamps_ms": segment.keyframe_timestamps_ms,
                    "asr": segment.asr_text,
                }
            ),
            response_digest=_digest(analysis),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=int((time.perf_counter() - start) * 1000),
            extra={
                "keyframes_sent": len(prepared_frames),
                "batch_count": len(batch_results),
                "selected_frame_indices": [item[0] for item in prepared_frames],
                "selected_timestamps_ms": [item[1] for item in prepared_frames],
                "total_tokens": input_tokens + output_tokens,
                "pricing_status": "not_configured",
                "request_ids": request_ids,
                "batches": [
                    {
                        "batch_index": batch_index,
                        "latency_ms": latency_ms,
                        "source_frame_indices": [frame["source_frame_index"] for frame in frames],
                        "timestamps_ms": [frame["timestamp_ms"] for frame in frames],
                    }
                    for batch_index, (frames, _result, latency_ms) in enumerate(batch_results)
                ],
            },
        )
        return analysis, receipt

    def compile_sop(
        self, analysis: dict[str, Any], rag_chunks: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        start = time.perf_counter()
        prompt: dict[str, Any] = {
            "task": "compile_sop",
            "analysis": _compact_teaching_analysis(analysis),
            "teaching_rule": (
                "若 analysis_format=teaching_windows_v1，综合全部 window_analyses，"
                "合并重叠窗口的同一动作，"
                "只编排学员要执行的操作；开场介绍和成品预览不作为前置操作。"
                "同一动作的重复演示合并，选动作最清楚的连续示范作为教学片段。"
                "保留必要操作、前后关系及最后自检，不把每个时间窗口当成一个步骤。"
                "每步额外输出 teaching:{source_asset_id,start_ms,end_ms,instruction}。"
                "使用整数毫秒时间，必须满足 0 <= start_ms < end_ms <= duration_ms；"
                "开始时间和结束时间不能相等。只有单个瞬间证据时合并至相关动作，"
                "不要生成零时长步骤或编造周围动作。"
                "没有依据的内容标为待确认，不编造步骤。"
                "检查 coverage 对应的全片过程，不遗漏较早窗口。"
            ),
            "rag_chunks": rag_chunks[:6],
            "batched_analysis_rule": (
                "若 analysis_format=timeline_batches_v1，batch_analyses 保存每批原始分析，"
                "step/tool/object/action_candidates 是跨批次按 timestamp_ms 稳定排序后的候选。"
                "必须综合所有批次形成完整 SOP，不得只采用首批或末批，也不得覆盖较早事实。"
            ),
            "schema_hint": (
                "输出 JSON：steps 数组，每步含 name(步骤中文名)/index/predecessors/"
                "tools/objects/actions/completion_checks/observable_checks/unobservable_checks/weight/"
                "evidence_refs。evidence_refs 必须是对象数组，每项含 kind/ref/description；"
                "video 还要 timestamp_ms，manual 还要 chunk_id"
            ),
            "visual_check_rule": (
                "completion_checks 只写动作结束后可从画面核实的工件/装配最终状态，"
                "例如“滤芯已就位”；observable_checks 只写独立可观察安全规范，"
                "例如“绝缘手套已佩戴”。不得把‘执行了动作’当作 completion_checks，"
                "也不得把扭矩、电压、内部质量等不可见事实写入任一视觉检查。"
                "不得把讲解员的姿态、面向镜头、视线或讲解手势作为学员完成条件。"
                "绳结穿绕关系等关键细节看不清时，必须列为人工确认条件，不能仅凭"
                "存在绳圈就声称正确完成。所有步骤权重按相对重要性分配，总和为100。"
            ),
            "evidence_rule": (
                "证据只允许两种真实引用：视频证据使用"
                "{kind:'video',ref:'video@<毫秒>ms',timestamp_ms:<毫秒>}，毫秒值必须精确来自"
                "analysis.reference_timeline；手册证据使用"
                "{kind:'manual',ref:'manual chunk',chunk_id:<整数>}，chunk_id 必须精确来自"
                "rag_chunks。若步骤依据了手册，必须同时保留 manual 证据，不能一律改写成 video。"
                "禁止引用 analysis.steps[n]、keyframe-i 或自造页码；无证据时不要生成该步骤。"
            ),
            "source_grade_rule": (
                "只要任一步引用 manual chunk，source_grade 输出 manual_backed；否则输出"
                "video_reference。服务端会用真实资产与 chunk 哈希复核。"
            ),
        }
        # 受控词表约束（整改：编译输出必须可发布，禁止自由中文/新造词）。
        # 词表由 service 层从 process_key 注入 analysis.controlled_vocab。
        vocab = analysis.get("controlled_vocab") if isinstance(analysis, dict) else None
        if vocab and isinstance(vocab, dict):
            prompt["controlled_vocab_rule"] = {
                "rule": (
                    "steps 中 tools 只能从「工具词表」取值，objects 只能从「对象词表」取值，"
                    "actions 只能从「动作词表」取值。禁止串位（工具不能放 objects，反之亦然）；"
                    "禁止使用词表外的中文描述或自创新词；分析中的中文表述需映射到最接近的词表项。"
                    "词表项语义：工具=手持的器具（如扳手/螺丝刀/插头），对象=被操作的零件（如螺母/砂轮）"
                ),
                "tools": sorted(vocab.get("tools", [])),
                "objects": sorted(vocab.get("objects", [])),
                "actions": sorted(vocab.get("actions", [])),
            }
        teaching_mode = analysis.get("analysis_format") == "teaching_windows_v1"
        if teaching_mode:
            prompt["teaching_schema"] = {
                "rule": "teaching 必须嵌套在 steps 的每一项内，不能仅放在根对象上。",
                "example": {
                    "steps": [
                        {
                            "name": "步骤名称",
                            "index": 1,
                            "predecessors": [],
                            "tools": [],
                            "objects": ["操作对象"],
                            "actions": ["操作动作"],
                            "completion_checks": ["可见的最终状态"],
                            "observable_checks": [],
                            "unobservable_checks": [],
                            "weight": 100,
                            "evidence_refs": [
                                {
                                    "kind": "video",
                                    "timestamp_ms": "从 reference_timeline 选择真实整数毫秒",
                                }
                            ],
                            "teaching": {
                                "source_asset_id": analysis.get("source_asset_id"),
                                "start_ms": "整数",
                                "end_ms": "严格大于start_ms的整数",
                                "instruction": "学员要执行的操作",
                            },
                        }
                    ],
                },
            }
        message = {
            "role": "user",
            "content": json.dumps(prompt, ensure_ascii=False),
        }
        sop, metadata = self._chat_with_metadata(
            [message],
            max_tokens=6000 if teaching_mode else 2000,
        )
        metadata_calls = [metadata]
        if teaching_mode:
            from gongxu.sop.teaching import enrich_steps

            try:
                enrich_steps(deepcopy(sop), analysis["source_asset_id"], analysis["duration_ms"])
            except ProviderError as exc:
                # One bounded repair: ask for actual ranges; never fabricate or pad them.
                sop, metadata = self._chat_with_metadata(
                    [
                        message,
                        {"role": "assistant", "content": json.dumps(sop, ensure_ascii=False)},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "validation_error": str(exc),
                                    "instruction": (
                                        "重新返回完整 JSON。严格遵守 teaching_schema；"
                                        "每步必须有独立的有效教学时间段。"
                                        "只有瞬间依据的步骤应合并，不编造时间。"
                                    ),
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    max_tokens=6000,
                )
                metadata_calls.append(metadata)
        receipt = ProviderReceipt(
            provider=self.name,
            mode=self.mode,
            model_id=self.model,
            prompt_version=_PROMPT_VERSION,
            input_digest=_digest({"a": analysis, "r": rag_chunks}),
            response_digest=_digest(sop),
            input_tokens=sum(call.input_tokens for call in metadata_calls),
            output_tokens=sum(call.output_tokens for call in metadata_calls),
            latency_ms=int((time.perf_counter() - start) * 1000),
            extra={
                "total_tokens": sum(call.total_tokens for call in metadata_calls),
                "compile_attempts": len(metadata_calls),
                "pricing_status": "not_configured",
                "request_id": metadata.request_id,
            },
        )
        return sop, receipt

    def explain_assessment(
        self, step_results: list[dict[str, Any]], evidence: dict[str, Any]
    ) -> tuple[str, ProviderReceipt]:
        start = time.perf_counter()
        message = {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "explain_assessment",
                    "language": "zh-CN",
                    "steps": step_results,
                    "evidence": evidence,
                    "constraints": [
                        "只解释输入中已经存在的步骤判定和证据，不补写不可见事实",
                        "不得输出、推测或修改任何数值分数",
                        "证据不足时明确建议调整机位或重新录制",
                        "只返回符合 response_schema 的 JSON 对象",
                    ],
                    "response_schema": {"explanation": "string, 800字以内"},
                },
                ensure_ascii=False,
            ),
        }
        response, metadata = self._chat_with_metadata([message], max_tokens=800)
        explanation = response.get("explanation", "")
        receipt = ProviderReceipt(
            provider=self.name,
            mode=self.mode,
            model_id=self.model,
            prompt_version=_PROMPT_VERSION,
            input_digest=_digest({"s": step_results, "e": evidence}),
            response_digest=_digest(explanation),
            input_tokens=metadata.input_tokens,
            output_tokens=metadata.output_tokens,
            latency_ms=int((time.perf_counter() - start) * 1000),
            extra={
                "total_tokens": metadata.total_tokens,
                "pricing_status": "not_configured",
                "request_id": metadata.request_id,
            },
        )
        return explanation, receipt

    def generate_practice_plan(
        self, report: dict[str, Any]
    ) -> tuple[dict[str, Any], ProviderReceipt]:
        start = time.perf_counter()
        message = {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "generate_practice_plan",
                    "language": "zh-CN",
                    "report": report,
                    "constraints": [
                        "只根据既有失败、不可观察或证据不足步骤安排复练",
                        "不得输出、推测或修改任何数值分数",
                        "focus_steps 最多 5 项，repeat_count 必须为 1 到 10 的整数",
                        "只返回符合 response_schema 的 JSON 对象",
                    ],
                    "response_schema": {
                        "focus_steps": ["step_id"],
                        "repeat_count": 3,
                        "note": "string, 400字以内",
                    },
                },
                ensure_ascii=False,
            ),
        }
        plan, metadata = self._chat_with_metadata([message])
        receipt = ProviderReceipt(
            provider=self.name,
            mode=self.mode,
            model_id=self.model,
            prompt_version=_PROMPT_VERSION,
            input_digest=_digest(report),
            response_digest=_digest(plan),
            input_tokens=metadata.input_tokens,
            output_tokens=metadata.output_tokens,
            latency_ms=int((time.perf_counter() - start) * 1000),
            extra={
                "total_tokens": metadata.total_tokens,
                "pricing_status": "not_configured",
                "request_id": metadata.request_id,
            },
        )
        return plan, receipt


def _digest(data: object) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _frame_to_data_uri(path_or_uri: str, *, max_bytes: int = 800 * 1024, max_width: int = 0) -> str:
    """将关键帧转为 base64 data URI；已是 data URI 则原样返回。

    max_width>0 时先降分辨率（缩小出站 payload，避免大图导致连接中断，
    同时符合出站最小化隐私原则）。
    """
    import base64
    import io
    from pathlib import Path

    from PIL import Image

    if path_or_uri.startswith("data:"):
        return path_or_uri

    path = Path(path_or_uri)
    if not path.exists():
        return ""
    data = path.read_bytes()
    if max_width:
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            if img.width > max_width:
                h = round(img.height * max_width / img.width)
                img = img.resize((max_width, h), Image.Resampling.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            data = buf.getvalue()
        except OSError:
            pass  # 无法解析的图像原样发送（不降分辨率）
    if len(data) > max_bytes:
        return ""  # 出站最小化：超限帧跳过
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"
