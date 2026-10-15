"""C 路径：Qwen vision 语义检测适配器（外部多模态模型）。

- 复用 Qwen38MaxProvider._chat（json_object + enable_thinking:false，已验证）。
- 受控词表驱动：tool/object/action 仅允许工序词表内值，避免幻觉与无关检测。
- 多帧批处理 `infer_batch`：一次发送多帧，调用次数 /4，显著加速预录关键帧。
- hand_present 兜底：画面有手在操作时可报低级证据，但保留模型原置信度；
  低于可见门槛时不计作可见帧，且不携带 tool/object/action、不影响步骤对齐。
- 发送前降分辨率（宽 ≤640、JPEG q70），控制出站量与耗时。
- LLM 自报 confidence 仅作 quality_gate 过滤参考；分数仍由确定性规则引擎决定。
- 网络/限流/非 JSON → AdapterUnavailableError（fail-closed，不产生伪事实）。
"""

from __future__ import annotations

import base64
import hashlib
import io
import statistics
import time
from collections.abc import Iterable, Sequence
from typing import Any

from PIL import Image

from gongxu.config import get_settings
from gongxu.providers.base import ProviderBudgetExceededError, ProviderError
from gongxu.providers.qwen import Qwen38MaxProvider
from gongxu.sop.schema import SopStep
from gongxu.sop.vocab import PROCESS_VOCABULARIES
from gongxu.vision.adapters import AdapterTransientError, AdapterUnavailableError, VisionAdapter
from gongxu.vision.facts import Detection, hash_frame

# 默认工序词表（向后兼容）；演示/其他工序通过构造参数显式指定
_DEFAULT_PROCESS_KEY = "impact_grinder_safe_subassembly"
_MIN_CONFIDENCE = 0.35
_MAX_BATCH = 4
_MAX_SEQUENCE_BATCH = 16
_MAX_CALLS = 32
_HANDS = {"left", "right", "both"}
_PHASES = {"before", "during", "after", "idle"}
_VOCAB_KEYS = ("tools", "objects", "actions")
_PROMPT_CONTRACT_VERSION = "prompt-v11"
_PARSER_CONTRACT_VERSION = "parser-v6"
_MODEL_VERSION_MAX_LENGTH = 64


def _normalise_vocab_value(value: object) -> str:
    """Normalise a SOP vocabulary token exactly as detector output is parsed."""
    if not isinstance(value, str):
        return ""
    token = value.strip().lower()
    if not token or len(token) > 64 or any(ord(char) < 32 for char in token):
        return ""
    return token


def _model_token(value: object) -> str:
    """Normalize an optional model token without turning JSON null into ``"none"``."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def vocabulary_from_sop_steps(steps: Sequence[SopStep] | None) -> dict[str, set[str]]:
    """Derive the controlled visual vocabulary from an immutable SOP snapshot.

    This is intentionally limited to the structured ``tools``, ``objects`` and
    ``actions`` fields. Step names, free-form checks and model output never
    expand the allow-list.
    """
    vocabulary: dict[str, set[str]] = {key: set() for key in _VOCAB_KEYS}
    for step in steps or ():
        values_by_key: dict[str, Iterable[object]] = {
            "tools": step.tools,
            "objects": step.objects,
            "actions": step.actions,
        }
        for key, values in values_by_key.items():
            vocabulary[key].update(
                token for value in values if (token := _normalise_vocab_value(value))
            )
    return vocabulary


def _merged_vocabulary(
    process_key: str, sop_steps: Sequence[SopStep] | None
) -> dict[str, set[str]]:
    registered = PROCESS_VOCABULARIES.get(process_key)
    dynamic = vocabulary_from_sop_steps(sop_steps)
    vocabulary = {key: set(dynamic[key]) for key in _VOCAB_KEYS}
    for key in _VOCAB_KEYS:
        for value in (registered or {}).get(key, set()):
            token = _normalise_vocab_value(value)
            if token:
                vocabulary[key].add(token)
    if not any(vocabulary.values()):
        raise AdapterUnavailableError(
            f"工序 {process_key} 未登记词表，且当前 SOP 未声明工具、对象或动作，无法约束输出"
        )
    return vocabulary


def _prepare_image(image_bytes: bytes, *, max_width: int = 640) -> bytes:
    """降分辨率 + JPEG 压缩，减小出站量与推理耗时。"""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if img.width > max_width:
        h = round(img.height * max_width / img.width)
        img = img.resize((max_width, h), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def _bytes_to_data_uri(image_bytes: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")


class QwenVisionAdapter(VisionAdapter):
    """外部 LLM 视觉语义检测：受控词表约束的帧/批 JSON 输出。"""

    name = "qwen"

    def __init__(
        self,
        *,
        process_key: str = _DEFAULT_PROCESS_KEY,
        sop_steps: Sequence[SopStep] | None = None,
        max_calls: int = _MAX_CALLS,
    ) -> None:
        settings = get_settings()
        if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
            raise AdapterUnavailableError("缺少 GONGXU_LLM_BASE_URL/API_KEY/MODEL 配置")
        self._provider = Qwen38MaxProvider()
        self._model = settings.llm_model
        self._process_key = process_key
        self._sop_steps = tuple(sop_steps or ())
        self._reference_frames: dict[int, tuple[bytes, ...]] = {}
        self._vocab = _merged_vocabulary(process_key, self._sop_steps)
        self._calls = 0
        self._successful_calls = 0
        self._failed_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._latency_ms = 0
        self._max_calls = max_calls

    def set_sop_steps(self, steps: Sequence[SopStep]) -> None:
        """注入当前发布的 SOP 摘要，仅作视觉候选步骤提示。"""
        self._sop_steps = tuple(steps)

    def set_reference_frames(self, frames: dict[int, bytes | Sequence[bytes]]) -> None:
        """Attach ephemeral standard-video context frames; never persist bytes."""
        allowed_indices = {step.index for step in self._sop_steps}
        normalised: dict[int, tuple[bytes, ...]] = {}
        for step_index, value in frames.items():
            if step_index not in allowed_indices:
                continue
            candidates = (value,) if isinstance(value, bytes) else tuple(value)
            valid = tuple(item for item in candidates if isinstance(item, bytes) and item)
            if valid:
                normalised[step_index] = valid
        self._reference_frames = normalised

    def reference_step_indices(self) -> set[int]:
        return set(self._reference_frames)

    def model_version(self) -> str:
        contract = f"{_PROMPT_CONTRACT_VERSION}-{_PARSER_CONTRACT_VERSION}"
        version = f"qwen@{self._model}:{contract}"
        if len(version) <= _MODEL_VERSION_MAX_LENGTH:
            return version

        # Provider model identifiers are user-configurable and may be arbitrarily
        # long. Keep the persisted cache key bounded without losing model identity.
        model_digest = hashlib.sha256(self._model.encode("utf-8")).hexdigest()[:12]
        return f"qwen@sha256-{model_digest}:{contract}"

    def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
        prepared = _prepare_image(image_bytes)
        content: list[dict[str, object]] = [
            {"type": "text", "text": self._prompt()},
            {"type": "image_url", "image_url": {"url": _bytes_to_data_uri(prepared)}},
        ]
        try:
            response = self._call_provider([{"role": "user", "content": content}], max_tokens=800)
        except ProviderBudgetExceededError as exc:
            raise AdapterUnavailableError(f"Qwen 授权或预算不可用: {exc}") from exc
        except ProviderError as exc:
            raise AdapterTransientError(f"Qwen vision 暂时不可用: {exc}") from exc
        return self._parse(
            response.get("detections", []),
            timestamp_ms=timestamp_ms,
            frame_hash=hash_frame(image_bytes),
            frame_index=0,
        )

    def infer_batch(self, frames: list[tuple[bytes, int]]) -> list[list[Detection]]:
        """一次发送多帧，返回按序每帧的检测列表（加速预录关键帧）。"""
        if not frames:
            return []
        if len(frames) == 1:
            img, ts = frames[0]
            return [self.infer_frame(img, timestamp_ms=ts)]
        if len(frames) > _MAX_BATCH:
            raise AdapterUnavailableError(f"批处理帧数超过上限 {_MAX_BATCH}")

        content: list[dict[str, object]] = [
            {"type": "text", "text": self._prompt(multi=len(frames))},
        ]
        for img, _ts in frames:
            prepared = _prepare_image(img)
            content.append(
                {"type": "image_url", "image_url": {"url": _bytes_to_data_uri(prepared)}}
            )
        try:
            response = self._call_provider([{"role": "user", "content": content}], max_tokens=2000)
        except ProviderError as batch_exc:
            # 批处理失败（外部服务不稳定/大 payload 断连）→ 降级逐帧推理。
            # 单帧局部失败保留为空证据；全部失败才拒判。
            results: list[list[Detection]] = []
            successful = 0
            transient_failures = 0
            for img, ts in frames:
                try:
                    results.append(self.infer_frame(img, timestamp_ms=ts))
                    successful += 1
                except AdapterTransientError:
                    transient_failures += 1
                    results.append([])
                except AdapterUnavailableError:
                    results.append([])
            if successful == 0:
                if transient_failures:
                    raise AdapterTransientError(
                        f"Qwen 批处理与逐帧降级均暂时失败: {batch_exc}"
                    ) from batch_exc
                raise AdapterUnavailableError(
                    f"Qwen 批处理与逐帧降级均失败: {batch_exc}"
                ) from batch_exc
            return results

        grouped: list[list[dict[str, object]]] = [[] for _ in frames]
        for item in self._response_items(response):
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item["frame_index"])
            except (TypeError, ValueError):
                continue
            except KeyError:
                continue
            if not 0 <= idx < len(frames):
                continue
            grouped[idx].append(item)

        return [
            self._parse(
                group,
                timestamp_ms=frames[i][1],
                frame_hash=hash_frame(frames[i][0]),
                frame_index=i,
            )
            for i, group in enumerate(grouped)
        ]

    def infer_sequence(self, frames: list[tuple[bytes, int]]) -> list[list[Detection]]:
        """Send an ordered image list as video so Qwen can compare state changes."""
        if not frames:
            return []
        if len(frames) == 1:
            return [self.infer_frame(frames[0][0], timestamp_ms=frames[0][1])]
        if len(frames) > _MAX_SEQUENCE_BATCH:
            raise AdapterUnavailableError(f"时序批处理帧数超过上限 {_MAX_SEQUENCE_BATCH}")

        prepared_urls = [
            _bytes_to_data_uri(_prepare_image(image_bytes)) for image_bytes, _timestamp_ms in frames
        ]
        fps = self._sequence_fps([timestamp_ms for _image, timestamp_ms in frames])
        content: list[dict[str, object]] = [
            {
                "type": "video",
                "video": prepared_urls,
                "fps": fps,
            },
            {
                "type": "text",
                "text": self._prompt(
                    multi=len(frames),
                    temporal=True,
                    timestamps_ms=[timestamp_ms for _image, timestamp_ms in frames],
                ),
            },
        ]
        try:
            response = self._call_provider(
                [{"role": "user", "content": content}],
                max_tokens=6000,
            )
        except ProviderError as sequence_exc:
            # Some compatible endpoints may not support image-list video input.
            # Preserve availability by falling back to the proven four-frame path.
            results: list[list[Detection]] = []
            try:
                for start in range(0, len(frames), _MAX_BATCH):
                    results.extend(self.infer_batch(frames[start : start + _MAX_BATCH]))
            except (AdapterTransientError, AdapterUnavailableError) as fallback_exc:
                raise AdapterTransientError(
                    f"Qwen 时序输入与静态批处理降级均失败: {sequence_exc}"
                ) from fallback_exc
            return results

        grouped: list[list[dict[str, object]]] = [[] for _ in frames]
        for item in self._response_items(response):
            if not isinstance(item, dict):
                continue
            try:
                frame_index = int(item["frame_index"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= frame_index < len(frames):
                grouped[frame_index].append(item)
        return [
            self._parse(
                group,
                timestamp_ms=frames[index][1],
                frame_hash=hash_frame(frames[index][0]),
                frame_index=index,
            )
            for index, group in enumerate(grouped)
        ]

    def infer_step_candidates(
        self,
        step_index: int,
        frames: list[tuple[bytes, int]],
    ) -> list[list[Detection]]:
        """Compare candidate frames with one standard-video frame for a missing step."""
        if not frames:
            return []
        if len(frames) > _MAX_BATCH:
            raise AdapterUnavailableError(f"参考对比候选帧数超过上限 {_MAX_BATCH}")
        references = self._reference_frames.get(step_index)
        step = next((item for item in self._sop_steps if item.index == step_index), None)
        if not references or step is None:
            return [[] for _ in frames]

        timestamp_text = ", ".join(
            f"{index}={timestamp_ms}ms" for index, (_image, timestamp_ms) in enumerate(frames)
        )
        visual_checks = [*step.completion_checks, *step.observable_checks]
        check_instruction = (
            "候选帧若清楚显示目标动作已发生或目标完成状态仍成立，可将逐字一致的检查项"
            "写入 observable_check_results 并标为 passed。只有清楚看到相反物理事实时才可"
            "标为 failed；后续步骤导致整体外观变化、与参考帧构图不同、目标零件被遮挡，"
            "都不能单独作为 failed 依据。before/during 阶段尚未完成绝不是 failed；只有"
            "目标动作结束后的最终错误状态才可使用 phase=after 和 failed。看不清必须省略。"
            if visual_checks
            else "observable_check_results 必须为空对象。"
        )
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    f"前 {len(references)} 张图片是标准教学视频中该步骤按时间排列的上下文"
                    "参考帧，之后的图片才是待评估视频候选帧。参考帧不是待评估证据，"
                    "不得为它们输出 detection。\n"
                    f"目标步骤 {step.index}: {step.name}; tools={step.tools}; "
                    f"objects={step.objects}; actions={step.actions}; "
                    f"completion_checks={step.completion_checks}; "
                    f"observable_safety_checks={step.observable_checks}\n"
                    f"候选帧时间映射: {timestamp_text}\n"
                    "参考序列用于理解动作和状态变化，不是要求候选画面像素或整体构图完全"
                    "相同。比较手、目标部件位置和目标状态。只有候选帧明确显示目标动作正在"
                    "发生或目标完成状态时，才输出目标 tool/object/action、"
                    "对安装步骤必须比较目标部件的插入侧、朝向、孔位、垫圈和紧固件相对"
                    "关系；仅看到零件存在不能证明安装正确。"
                    f"step_index={step.index} 和实际置信度。仅看到清晰手部但无法判断目标"
                    "步骤时，可输出 hand_present；模糊或无证据则不输出。install/remove"
                    "必须依据状态变化或明确手部关系，不得仅凭零件静态外观猜测。"
                    f"{check_instruction}"
                    '输出 JSON: {"detections": [{"frame_index": 0, "hand": "both", '
                    '"tool": null, "object": "...", "action": "...", "phase": "during", '
                    '"step_index": 1, "confidence": 0.8, "evidence_reason": "...", '
                    '"observable_check_results": {"逐字一致检查项": "passed"}}]}。'
                    "frame_index 只对应候选帧 0 起编号。"
                ),
            },
        ]
        for reference in references:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _bytes_to_data_uri(_prepare_image(reference)),
                    },
                }
            )
        for image_bytes, _timestamp_ms in frames:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _bytes_to_data_uri(_prepare_image(image_bytes)),
                    },
                }
            )
        try:
            response = self._call_provider(
                [{"role": "user", "content": content}],
                max_tokens=2400,
            )
        except ProviderError as exc:
            raise AdapterTransientError(f"Qwen 参考帧对比失败: {exc}") from exc

        grouped: list[list[dict[str, object]]] = [[] for _ in frames]
        for item in self._response_items(response):
            if not isinstance(item, dict):
                continue
            try:
                frame_index = int(item["frame_index"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= frame_index < len(frames):
                grouped[frame_index].append(item)
        return [
            self._parse(
                group,
                timestamp_ms=frames[index][1],
                frame_hash=hash_frame(frames[index][0]),
                frame_index=index,
            )
            for index, group in enumerate(grouped)
        ]

    @staticmethod
    def _sequence_fps(timestamps_ms: list[int]) -> float:
        positive_gaps = [
            current - previous
            for previous, current in zip(timestamps_ms, timestamps_ms[1:], strict=False)
            if current > previous
        ]
        if not positive_gaps:
            return 1.0
        fps = 1000.0 / statistics.median(positive_gaps)
        return round(min(10.0, max(0.1, fps)), 4)

    def _call_provider(self, messages: list[dict[str, Any]], *, max_tokens: int) -> dict[str, Any]:
        if self._calls >= self._max_calls:
            raise AdapterUnavailableError(f"Qwen vision 调用超过单任务上限 {self._max_calls}")
        self._calls += 1
        started = time.perf_counter()
        try:
            response = self._provider._chat(messages, max_tokens=max_tokens)
        except Exception:
            self._failed_calls += 1
            raise
        finally:
            self._latency_ms += int((time.perf_counter() - started) * 1000)
        self._successful_calls += 1
        metadata = self._provider.last_chat_metadata()
        self._input_tokens += int(metadata.get("input_tokens", 0) or 0)
        self._output_tokens += int(metadata.get("output_tokens", 0) or 0)
        return response

    def telemetry(self) -> dict[str, int | str]:
        """Job-local Qwen usage; never contains prompts, images, paths, or API keys."""

        return {
            "provider": "qwen",
            "model_id": self._provider.model,
            "calls": self._calls,
            "successful_calls": self._successful_calls,
            "failed_calls": self._failed_calls,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "total_tokens": self._input_tokens + self._output_tokens,
            "latency_ms": self._latency_ms,
            "pricing_status": "not_configured",
        }

    @staticmethod
    def _response_items(response: object) -> list[object]:
        """容忍扁平 detections/events 和按帧 frames 三种结构。"""
        if not isinstance(response, dict):
            return []
        if "detections" in response or "events" in response:
            raw = response.get("detections", response.get("events", []))
            return list(raw) if isinstance(raw, list) else []
        frames = response.get("frames", [])
        if not isinstance(frames, list):
            return []
        flattened: list[object] = []
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            try:
                frame_index = int(frame.get("frame_index", -1))
            except (TypeError, ValueError):
                continue
            events = frame.get("detections", frame.get("events", []))
            if not isinstance(events, list):
                continue
            for event in events:
                if isinstance(event, dict):
                    flattened.append({**event, "frame_index": frame_index})
        return flattened

    def _prompt(
        self,
        *,
        multi: int = 0,
        temporal: bool = False,
        timestamps_ms: list[int] | None = None,
    ) -> str:
        tools = "、".join(sorted(self._vocab["tools"]))
        objects = "、".join(sorted(self._vocab["objects"]))
        actions = "、".join(sorted(self._vocab["actions"]))
        frame_intro = (
            f"你会收到 {multi} 帧画面（按顺序）。对每一帧，用 frame_index（0~{multi - 1}）"
            "标注属于哪帧。\n"
            if multi > 1
            else "你收到 1 帧画面。\n"
        )
        temporal_intro = ""
        if temporal:
            timestamp_text = ", ".join(
                f"{index}={timestamp_ms}ms"
                for index, timestamp_ms in enumerate(timestamps_ms or [])
            )
            temporal_intro = (
                "这些画面来自同一视频并按时间递增排列。必须比较相邻画面的手、零件位置和"
                "装配状态变化来区分 install/remove；静态外观相似而没有前后变化证据时不得猜测。\n"
                f"帧时间映射: {timestamp_text}\n"
            )
        step_summary = self._step_summary()
        return (
            "你在判断工业装配操作视频的画面帧。严格遵守以下受控词表：\n"
            f"工具: {tools}\n"
            f"对象: {objects}\n"
            f"动作: {actions}\n"
            f"手: {'、'.join(sorted(_HANDS))}\n"
            f"当前 SOP 步骤（仅用于生成候选，最终由本地规则对齐）:\n{step_summary}\n"
            f"{frame_intro}{temporal_intro}"
            "规则：\n"
            "1. 有明确证据才报告 tool/object/action；模糊或看不到就留空，禁止猜测、禁止杜撰。\n"
            "2. 画面中有手在操作但无法确定具体工具/对象/动作时，可以返回一条 "
            '{"hand": "left", "relation": "hand_present", '
            '"confidence": 0.7}。此处 confidence 只表示“手是否清晰可见”：手部清晰、'
            "无遮挡时应按实际证据给出 >=0.6；模糊、严重遮挡或只露出很小部分时必须 <0.6。"
            "不得因为动作类别不确定而把清晰的手部可见度固定写成 0.4，也禁止为了通过门槛抬高。\n"
            "3. step_index 只能从上述 SOP 步骤选择；不确定时为 null。phase 只能是 "
            "before/during/after/idle。evidence_reason 用一句短句说明可见依据。\n"
            "4. observable_check_results 只能使用该 step_index 下逐字一致的完成状态或安全检查项，"
            "状态只能是 passed 或 failed。passed 必须明确看到检查已满足，failed 必须明确"
            "看到相反事实；看不清、未出现在画面或只能由动作猜测时必须省略，禁止默认通过。\n"
            '输出 JSON: {"detections": [{"frame_index": 0, "hand": "left", "tool": "wrench", '
            '"object": "locking_nut", "action": "loosen", "phase": "during", '
            '"step_index": 5, "confidence": 0.85, "evidence_reason": "手持扳手接触锁紧螺母", '
            '"observable_check_results": {"锁紧螺母已松开": "passed"}}]}\n'
            "confidence 反映证据确定性（0-1），明确看到才给 >0.6。"
        )

    def _step_summary(self) -> str:
        if not self._sop_steps:
            return "未提供步骤摘要，step_index 必须为 null"
        lines = []
        for step in sorted(self._sop_steps, key=lambda item: item.index):
            lines.append(
                f"{step.index}. {step.name}; tools={step.tools}; objects={step.objects}; "
                f"actions={step.actions}; completion_checks={step.completion_checks}; "
                f"observable_safety_checks={step.observable_checks}; "
                f"predecessors={step.predecessors}"
            )
        return "\n".join(lines)

    def _parse(
        self,
        raw: object,
        *,
        timestamp_ms: int,
        frame_hash: str,
        frame_index: int,
    ) -> list[Detection]:
        if not isinstance(raw, list):
            raise AdapterUnavailableError("Qwen 返回 detections 非数组")
        detections: list[Detection] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                hand = _model_token(item.get("hand"))
                tool = _model_token(item.get("tool"))
                obj = _model_token(item.get("object"))
                action = _model_token(item.get("action"))
                relation = _model_token(item.get("relation"))
                confidence = float(item.get("confidence", 0.0))
                phase = _model_token(item.get("phase"))
                evidence_reason = (
                    item.get("evidence_reason", "").strip()[:240]
                    if isinstance(item.get("evidence_reason"), str)
                    else ""
                )
            except (TypeError, ValueError):
                # 单个 item 坏掉不丢弃整批的其他有效事件。
                continue
            if not 0.0 <= confidence <= 1.0:
                continue
            if phase not in _PHASES:
                phase = ""
            candidate_step_index = self._candidate_step(item.get("step_index"))
            observable_check_results = self._observable_check_results(
                item.get("observable_check_results"), candidate_step_index, phase=phase
            )

            # hand_present 兜底：只有手存在证据，不携带语义字段
            if (
                relation == "hand_present"
                and hand
                and not (tool or obj or action or observable_check_results)
            ):
                if hand not in _HANDS:
                    continue
                detections.append(
                    Detection(
                        timestamp_ms=timestamp_ms,
                        hand=hand,
                        relation="hand_present",
                        confidence=confidence,
                        frame_hash=frame_hash,
                        frame_index=frame_index,
                        phase=phase,
                        candidate_step_index=candidate_step_index,
                        evidence_reason=evidence_reason,
                    )
                )
                continue

            # 词表一致性：词表外或明显低置信 → 丢弃（不产生伪证据）
            if hand and hand not in _HANDS:
                continue
            if tool and tool not in self._vocab["tools"]:
                continue
            if obj and obj not in self._vocab["objects"]:
                continue
            if action and action not in self._vocab["actions"]:
                continue
            if confidence < _MIN_CONFIDENCE:
                continue
            if not (hand or tool or obj or action or observable_check_results):
                continue
            if tool:
                relation = f"{hand}_holds_{tool}" if hand else "holds"
            elif obj:
                relation = f"{hand}_operates_{obj}" if hand else "operates"
            elif hand:
                relation = "hand_present"
            else:
                relation = "observable_check"
            detections.append(
                Detection(
                    timestamp_ms=timestamp_ms,
                    hand=hand,
                    tool=tool,
                    object=obj,
                    action=action,
                    relation=relation,
                    confidence=confidence,
                    frame_hash=frame_hash,
                    frame_index=frame_index,
                    phase=phase,
                    candidate_step_index=candidate_step_index,
                    evidence_reason=evidence_reason,
                    observable_check_results=observable_check_results,
                )
            )
        return detections

    def _observable_check_results(
        self,
        raw: object,
        candidate_step_index: int | None,
        *,
        phase: str,
    ) -> dict[str, str]:
        """Accept only exact checks declared by the explicitly selected SOP step."""
        if candidate_step_index is None or not isinstance(raw, dict):
            return {}
        step = next(
            (item for item in self._sop_steps if item.index == candidate_step_index),
            None,
        )
        if step is None:
            return {}
        allowed = set(step.completion_checks) | set(step.observable_checks)
        parsed: dict[str, str] = {}
        for check, status in raw.items():
            if not isinstance(check, str) or check not in allowed or not isinstance(status, str):
                continue
            normalised_status = status.strip().lower()
            if normalised_status == "failed" and phase != "after":
                continue
            if normalised_status in {"passed", "failed"}:
                parsed[check] = normalised_status
        return parsed

    def _candidate_step(self, raw: object) -> int | None:
        if raw in (None, ""):
            return None
        if not isinstance(raw, (str, int, float)):
            return None
        try:
            candidate = int(raw)
        except (TypeError, ValueError):
            return None
        valid = {step.index for step in self._sop_steps}
        return candidate if candidate in valid else None
