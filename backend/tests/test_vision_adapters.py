"""视觉适配器测试：resolve_adapter 选择、Qwen/Onnx 解析与 fail-closed。

原则：不依赖真实模型文件或网络；用 monkeypatch 注入构造结果与适配器。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.sop.schema import SopStep
from gongxu.vision.adapters import (
    AdapterUnavailableError,
    NoVisionAdapter,
    VisionAdapter,
    resolve_adapter,
)
from gongxu.vision.facts import Detection, visible_frame_ratio


def _png_bytes(size: tuple[int, int] = (64, 48)) -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", size, (200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _clear_settings() -> None:
    get_settings.cache_clear()


def test_resolve_adapter_default_is_fail_closed(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vision_backend 默认 none → NoVisionAdapter（fail-closed）。"""
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "none")
    _clear_settings()
    try:
        adapter = resolve_adapter(db)
        assert isinstance(adapter, NoVisionAdapter)
    finally:
        _clear_settings()


def test_resolve_adapter_unknown_backend_fail_closed(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法 backend 值 → NoVisionAdapter。"""
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "bogus")
    _clear_settings()
    try:
        assert isinstance(resolve_adapter(db), NoVisionAdapter)
    finally:
        _clear_settings()


def test_resolve_adapter_qwen_without_creds_fail_closed(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qwen 后端但缺少 LLM 凭据 → NoVisionAdapter（空字符串覆盖 .env 配置）。"""
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "qwen")
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "")
    _clear_settings()
    try:
        assert isinstance(resolve_adapter(db), NoVisionAdapter)
    finally:
        _clear_settings()


def test_resolve_adapter_onnx_without_registered_models_fail_closed(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """onnx 后端但模型未登记 available → NoVisionAdapter。"""
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "onnx")
    monkeypatch.setenv("GONGXU_MODEL_ROOT", str(Path("/nonexistent")))
    _clear_settings()
    try:
        assert isinstance(resolve_adapter(db), NoVisionAdapter)
    finally:
        _clear_settings()


def test_resolve_adapter_qwen_uses_process_key_vocab(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qwen 后端按 process_key 选词表：齿轮箱工序不应回退到角磨机默认词表（修复词表绑定断点）。

    回归点：resolve_adapter(db) 旧实现不传 process_key，导致正式考评永远用默认
    角磨机词表，齿轮箱对象（gearbox_housing 等）被全部丢弃 → 0 步骤匹配。
    """
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "qwen")
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "test-model")
    _clear_settings()
    try:
        adapter = resolve_adapter(db, process_key="impact_gearbox_disassembly_demo")
        # 成功构造（非 NoVisionAdapter）即证明齿轮箱词表命中、未因词表缺失回退
        assert not isinstance(adapter, NoVisionAdapter)
        assert "gearbox_housing" in adapter._vocab["objects"]  # noqa: SLF001
        # 生产 resolver 缺少工序时绝不借用角磨机词表。
        missing_key = resolve_adapter(db)
        assert isinstance(missing_key, NoVisionAdapter)
        with pytest.raises(AdapterUnavailableError, match="缺少当前工序标识"):
            missing_key.infer_frame(_png_bytes(), timestamp_ms=0)
    finally:
        _clear_settings()


def test_resolve_adapter_qwen_unknown_process_requires_current_sop_vocab(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新工序只可使用当前 SOP 的结构化词表；没有 SOP 时 fail-closed。"""
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "qwen")
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "test-model")
    _clear_settings()
    try:
        unavailable = resolve_adapter(db, process_key="teacher_custom_process")
        assert isinstance(unavailable, NoVisionAdapter)
        with pytest.raises(AdapterUnavailableError, match="当前 SOP 未声明"):
            unavailable.infer_frame(_png_bytes(), timestamp_ms=0)

        step = SopStep(
            id="custom-1",
            index=1,
            name="锁紧定位环",
            tools=["Torque_Driver"],
            objects=["Retaining_Ring"],
            actions=["Secure"],
            observable_checks=["定位环固定"],
            weight=1.0,
        )
        adapter = resolve_adapter(
            db,
            process_key="teacher_custom_process",
            sop_steps=[step],
        )
        assert not isinstance(adapter, NoVisionAdapter)
        assert adapter._vocab == {  # noqa: SLF001
            "tools": {"torque_driver"},
            "objects": {"retaining_ring"},
            "actions": {"secure"},
        }

        fake = {
            "detections": [
                {
                    "tool": "torque_driver",
                    "object": "retaining_ring",
                    "action": "secure",
                    "confidence": 0.9,
                },
                {
                    "tool": "wrench",
                    "object": "locking_nut",
                    "action": "loosen",
                    "confidence": 0.99,
                },
            ]
        }
        monkeypatch.setattr(adapter._provider, "_chat", lambda *a, **k: fake)  # noqa: SLF001
        detections = adapter.infer_frame(_png_bytes(), timestamp_ms=1200)
        assert len(detections) == 1
        assert detections[0].tool == "torque_driver"
        assert detections[0].object == "retaining_ring"
        assert detections[0].action == "secure"
    finally:
        _clear_settings()


def test_onnx_adapter_missing_models_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型文件缺失 → OnnxVisionAdapter 构造抛 AdapterUnavailableError。"""
    monkeypatch.setenv("GONGXU_MODEL_ROOT", str(Path("/nonexistent/models")))
    _clear_settings()
    try:
        from gongxu.vision.adapters_onnx import OnnxVisionAdapter

        with pytest.raises(AdapterUnavailableError, match="模型缺失"):
            OnnxVisionAdapter()
    finally:
        _clear_settings()


def test_onnx_model_version_fingerprints_both_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both weight contents and the post-processing contract identify cached results."""
    hand_bytes = b"hand-landmarker-v1"
    yolo_bytes = b"yolov8n-v1"
    hand_path = tmp_path / "hand_landmarker.task"
    yolo_path = tmp_path / "yolov8n.onnx"
    hand_path.write_bytes(hand_bytes)
    yolo_path.write_bytes(yolo_bytes)
    monkeypatch.setenv("GONGXU_MODEL_ROOT", str(tmp_path))
    _clear_settings()
    try:
        from gongxu.vision.adapters_onnx import OnnxVisionAdapter

        first = OnnxVisionAdapter().model_version()
        hand_sha = hashlib.sha256(hand_bytes).hexdigest()[:12]
        yolo_sha = hashlib.sha256(yolo_bytes).hexdigest()[:12]
        assert first == f"onnx@h-{hand_sha}.y-{yolo_sha}.post-v1"
        assert len(first) <= 64
        assert hand_path.read_bytes() == hand_bytes
        assert yolo_path.read_bytes() == yolo_bytes

        hand_path.write_bytes(b"hand-landmarker-v2")
        second = OnnxVisionAdapter().model_version()
        assert second != first
        assert f".y-{yolo_sha}." in second
    finally:
        _clear_settings()


def test_qwen_model_version_includes_visual_contract_and_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt/parser changes cannot silently reuse a prior Qwen evidence cache."""
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        version = QwenVisionAdapter().model_version()
        assert version == "qwen@qwen3.8-max:prompt-v11-parser-v6"
        assert len(version) <= 64

        long_model = "provider/" + ("long-model-name-" * 10)
        monkeypatch.setenv("GONGXU_LLM_MODEL", long_model)
        _clear_settings()
        bounded = QwenVisionAdapter().model_version()
        expected_digest = hashlib.sha256(long_model.encode()).hexdigest()[:12]
        assert bounded == f"qwen@sha256-{expected_digest}:prompt-v11-parser-v6"
        assert len(bounded) <= 64
    finally:
        _clear_settings()


def test_qwen_adapter_parses_detections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Qwen 返回词表内 JSON → 正确解析为 Detection。"""
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        adapter = QwenVisionAdapter()
        fake = {
            "detections": [
                {
                    "hand": "left",
                    "tool": "wrench",
                    "object": "locking_nut",
                    "action": "loosen",
                    "confidence": 0.9,
                }
            ]
        }
        monkeypatch.setattr(adapter._provider, "_chat", lambda *a, **k: fake)
        dets = adapter.infer_frame(_png_bytes(), timestamp_ms=1000)
        assert len(dets) == 1
        d = dets[0]
        assert d.hand == "left"
        assert d.tool == "wrench"
        assert d.action == "loosen"
        assert d.relation == "left_holds_wrench"
        assert d.timestamp_ms == 1000
    finally:
        _clear_settings()


def test_qwen_adapter_treats_json_null_as_missing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nullable optional field must not invalidate otherwise valid evidence."""
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        step = SopStep(
            id="industreal-assy-01",
            index=1,
            name="安装前车架",
            tools=[],
            objects=["front_chassis"],
            actions=["install_front_chassis"],
            completion_checks=["已完成：安装前车架"],
            weight=1.0,
        )
        adapter = QwenVisionAdapter(sop_steps=[step])
        fake = {
            "detections": [
                {
                    "hand": "both",
                    "tool": None,
                    "object": "front_chassis",
                    "action": "install_front_chassis",
                    "relation": None,
                    "phase": "during",
                    "step_index": 1,
                    "confidence": 0.75,
                    "evidence_reason": None,
                }
            ]
        }
        monkeypatch.setattr(adapter._provider, "_chat", lambda *a, **k: fake)

        detections = adapter.infer_frame(_png_bytes(), timestamp_ms=1000)

        assert len(detections) == 1
        assert detections[0].hand == "both"
        assert detections[0].tool == ""
        assert detections[0].object == "front_chassis"
        assert detections[0].action == "install_front_chassis"
        assert detections[0].candidate_step_index == 1
        assert detections[0].evidence_reason == ""
    finally:
        _clear_settings()


def test_qwen_adapter_collects_job_local_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        adapter = QwenVisionAdapter()
        monkeypatch.setattr(
            adapter._provider,  # noqa: SLF001
            "_chat",
            lambda *args, **kwargs: {"detections": []},
        )
        monkeypatch.setattr(
            adapter._provider,  # noqa: SLF001
            "last_chat_metadata",
            lambda: {
                "input_tokens": 40,
                "output_tokens": 8,
                "total_tokens": 48,
                "request_id": "request-test",
            },
        )

        adapter.infer_frame(_png_bytes(), timestamp_ms=0)
        metrics = adapter.telemetry()

        assert metrics["calls"] == 1
        assert metrics["successful_calls"] == 1
        assert metrics["failed_calls"] == 0
        assert metrics["input_tokens"] == 40
        assert metrics["output_tokens"] == 8
        assert metrics["total_tokens"] == 48
        assert metrics["pricing_status"] == "not_configured"
    finally:
        _clear_settings()


def test_qwen_adapter_accepts_only_exact_step_check_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety evidence is constrained to exact checks on the selected SOP step."""
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.sop.fixtures import grinder_disassembly_sop
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        sop = grinder_disassembly_sop()
        adapter = QwenVisionAdapter(sop_steps=sop.steps)
        fake = {
            "detections": [
                {
                    "tool": "wrench",
                    "object": "locking_nut",
                    "action": "loosen",
                    "step_index": 5,
                    "confidence": 0.9,
                    "observable_check_results": {
                        "锁紧螺母已松开": "passed",
                        "双手已戴手套": "passed",
                        "不存在的检查": "passed",
                    },
                }
            ]
        }
        monkeypatch.setattr(adapter._provider, "_chat", lambda *a, **k: fake)

        detections = adapter.infer_frame(_png_bytes(), timestamp_ms=1000)

        assert detections[0].observable_check_results == {"锁紧螺母已松开": "passed"}
    finally:
        _clear_settings()


def test_qwen_adapter_filters_out_of_vocab_and_low_conf(monkeypatch: pytest.MonkeyPatch) -> None:
    """词表外 tool、低置信度 → 丢弃（不产生伪证据）。"""
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        adapter = QwenVisionAdapter()
        fake = {
            "detections": [
                {"tool": "vectorscope", "confidence": 0.9},  # 词表外
                {
                    "tool": "wrench",
                    "object": "locking_nut",
                    "action": "loosen",
                    "confidence": 0.1,  # 低置信
                },
                {"tool": "wrench", "object": "locking_nut", "action": "loosen", "confidence": 0.85},
            ]
        }
        monkeypatch.setattr(adapter._provider, "_chat", lambda *a, **k: fake)
        dets = adapter.infer_frame(_png_bytes(), timestamp_ms=0)
        assert len(dets) == 1
        assert dets[0].tool == "wrench"
    finally:
        _clear_settings()


def test_qwen_adapter_non_array_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """detections 非数组 → AdapterUnavailableError（fail-closed）。"""
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        adapter = QwenVisionAdapter()
        monkeypatch.setattr(adapter._provider, "_chat", lambda *a, **k: {"detections": "oops"})
        with pytest.raises(AdapterUnavailableError, match="非数组"):
            adapter.infer_frame(_png_bytes(), timestamp_ms=0)
    finally:
        _clear_settings()


def test_qwen_adapter_batch_groups_by_frame_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """infer_batch 按 frame_index 分组返回每帧检测。"""
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        adapter = QwenVisionAdapter()
        fake = {
            "detections": [
                {"frame_index": 0, "hand": "left", "relation": "hand_present", "confidence": 0.6},
                {
                    "frame_index": 2,
                    "tool": "wrench",
                    "object": "locking_nut",
                    "action": "loosen",
                    "confidence": 0.85,
                },
            ]
        }
        monkeypatch.setattr(adapter._provider, "_chat", lambda *a, **k: fake)
        frames = [(_png_bytes(), 1000), (_png_bytes(), 2000), (_png_bytes(), 3000)]
        per_frame = adapter.infer_batch(frames)
        assert len(per_frame) == 3
        assert len(per_frame[0]) == 1
        assert per_frame[0][0].relation == "hand_present"
        assert per_frame[0][0].timestamp_ms == 1000
        assert len(per_frame[1]) == 0
        assert len(per_frame[2]) == 1
        assert per_frame[2][0].tool == "wrench"
        assert per_frame[2][0].timestamp_ms == 3000
    finally:
        _clear_settings()


def test_qwen_sequence_uses_video_input_and_preserves_global_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        adapter = QwenVisionAdapter()
        captured: dict[str, object] = {}

        def _chat(messages: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
            captured["messages"] = messages
            captured["max_tokens"] = kwargs.get("max_tokens")
            return {
                "detections": [
                    {
                        "frame_index": 0,
                        "hand": "left",
                        "relation": "hand_present",
                        "confidence": 0.7,
                    },
                    {
                        "frame_index": 2,
                        "tool": "wrench",
                        "object": "locking_nut",
                        "action": "loosen",
                        "confidence": 0.9,
                    },
                ]
            }

        monkeypatch.setattr(adapter._provider, "_chat", _chat)
        frames = [(_png_bytes(), 0), (_png_bytes(), 2000), (_png_bytes(), 4000)]

        result = adapter.infer_sequence(frames)

        assert [len(items) for items in result] == [1, 0, 1]
        assert result[0][0].timestamp_ms == 0
        assert result[2][0].timestamp_ms == 4000
        assert captured["max_tokens"] == 6000
        messages = captured["messages"]
        assert isinstance(messages, list)
        content = messages[0]["content"]
        assert isinstance(content, list)
        video_part = content[0]
        assert video_part["type"] == "video"
        assert video_part["fps"] == 0.5
        assert len(video_part["video"]) == 3
        assert all(str(url).startswith("data:image/jpeg;base64,") for url in video_part["video"])
        prompt = str(content[1]["text"])
        assert "比较相邻画面" in prompt
        assert "0=0ms, 1=2000ms, 2=4000ms" in prompt
    finally:
        _clear_settings()


def test_qwen_reference_comparison_uses_standard_frame_only_as_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        step = SopStep(
            id="step-1",
            index=1,
            name="安装前车架",
            objects=["front_chassis"],
            actions=["install_front_chassis"],
            completion_checks=["已完成：安装前车架"],
            weight=1.0,
        )
        adapter = QwenVisionAdapter(sop_steps=[step])
        adapter.set_reference_frames({1: _png_bytes()})
        captured: dict[str, object] = {}

        def _chat(messages: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
            captured["messages"] = messages
            return {
                "detections": [
                    {
                        "frame_index": 1,
                        "hand": "both",
                        "tool": None,
                        "object": "front_chassis",
                        "action": "install_front_chassis",
                        "step_index": 1,
                        "confidence": 0.85,
                        "observable_check_results": {"已完成：安装前车架": "passed"},
                    }
                ]
            }

        monkeypatch.setattr(adapter._provider, "_chat", _chat)
        candidates = [(_png_bytes(), 30_000), (_png_bytes(), 35_000)]

        result = adapter.infer_step_candidates(1, candidates)

        assert adapter.reference_step_indices() == {1}
        assert [len(items) for items in result] == [0, 1]
        assert result[1][0].timestamp_ms == 35_000
        assert result[1][0].observable_check_results == {"已完成：安装前车架": "passed"}
        messages = captured["messages"]
        assert isinstance(messages, list)
        content = messages[0]["content"]
        assert isinstance(content, list)
        assert len(content) == 4  # prompt + one reference + two candidates
        prompt = str(content[0]["text"])
        assert "参考帧不是待评估证据" in prompt
        assert "整体外观变化" in prompt
        assert "completion_checks=['已完成：安装前车架']" in prompt
    finally:
        _clear_settings()


def test_qwen_rejects_failed_check_before_post_action_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        check = "已完成：安装前车架"
        step = SopStep(
            id="step-1",
            index=1,
            name="安装前车架",
            objects=["front_chassis"],
            actions=["install_front_chassis"],
            observable_checks=[check],
            weight=1.0,
        )
        adapter = QwenVisionAdapter(sop_steps=[step])
        raw = [
            {
                "object": "front_chassis",
                "action": "install_front_chassis",
                "step_index": 1,
                "phase": "during",
                "confidence": 0.9,
                "observable_check_results": {check: "failed"},
            },
            {
                "object": "front_chassis",
                "action": "install_front_chassis",
                "step_index": 1,
                "phase": "after",
                "confidence": 0.9,
                "observable_check_results": {check: "failed"},
            },
        ]

        parsed = adapter._parse(raw, timestamp_ms=1000, frame_hash="frame", frame_index=0)

        assert parsed[0].observable_check_results == {}
        assert parsed[1].observable_check_results == {check: "failed"}
    finally:
        _clear_settings()


def test_qwen_batch_keeps_valid_items_when_one_item_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个错误 item 不应让整批有效事件丢失。"""
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.sop.fixtures import grinder_disassembly_sop
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        adapter = QwenVisionAdapter(sop_steps=grinder_disassembly_sop().steps)
        fake = {
            "detections": [
                {
                    "frame_index": 0,
                    "tool": "wrench",
                    "object": "locking_nut",
                    "action": "loosen",
                    "step_index": 5,
                    "phase": "during",
                    "confidence": "not-a-number",
                },
                {
                    "frame_index": 1,
                    "tool": "wrench",
                    "object": "locking_nut",
                    "action": "loosen",
                    "step_index": 5,
                    "phase": "during",
                    "confidence": 0.88,
                    "evidence_reason": "扳手与锁紧螺母接触",
                },
                {"frame_index": 99, "confidence": 0.99},
            ]
        }
        monkeypatch.setattr(adapter._provider, "_chat", lambda *a, **k: fake)
        per_frame = adapter.infer_batch([(_png_bytes(), 1000), (_png_bytes(), 5000)])
        assert per_frame[0] == []
        assert len(per_frame[1]) == 1
        assert per_frame[1][0].candidate_step_index == 5
        assert per_frame[1][0].phase == "during"
        assert per_frame[1][0].evidence_reason
    finally:
        _clear_settings()


def test_qwen_batch_accepts_nested_frames_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        adapter = QwenVisionAdapter()
        fake = {
            "frames": [
                {
                    "frame_index": 1,
                    "events": [
                        {
                            "hand": "right",
                            "relation": "hand_present",
                            "confidence": 0.6,
                        }
                    ],
                }
            ]
        }
        monkeypatch.setattr(adapter._provider, "_chat", lambda *a, **k: fake)
        result = adapter.infer_batch([(_png_bytes(), 0), (_png_bytes(), 3000)])
        assert result[0] == []
        assert result[1][0].timestamp_ms == 3000
    finally:
        _clear_settings()


def test_qwen_adapter_hand_present_preserves_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """hand_present 保留模型置信度；低置信存在事实不能伪装成可见证据。"""
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        adapter = QwenVisionAdapter()
        fake = {"detections": [{"hand": "right", "relation": "hand_present", "confidence": 0.4}]}
        monkeypatch.setattr(adapter._provider, "_chat", lambda *a, **k: fake)
        dets = adapter.infer_frame(_png_bytes(), timestamp_ms=0)
        assert len(dets) == 1
        assert dets[0].relation == "hand_present"
        assert dets[0].confidence == 0.4
        assert dets[0].tool == "" and dets[0].action == ""
        assert visible_frame_ratio(dets, total_frames=1) == 0.0
    finally:
        _clear_settings()


def test_qwen_adapter_process_key_drives_vocab(monkeypatch: pytest.MonkeyPatch) -> None:
    """不同 process_key 注入不同词表；词表外对象被丢弃。"""
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen3.8-max")
    _clear_settings()
    try:
        from gongxu.vision.adapters_qwen import QwenVisionAdapter

        # 齿轮箱词表：grinding_wheel 不在其中 → 丢弃
        adapter = QwenVisionAdapter(process_key="impact_gearbox_disassembly_demo")
        fake = {
            "detections": [
                {"tool": "grinding_wheel", "confidence": 0.9},  # 词表外（grinder 对象）
                {"object": "gearbox_housing", "action": "hold", "confidence": 0.85},
            ]
        }
        monkeypatch.setattr(adapter._provider, "_chat", lambda *a, **k: fake)
        dets = adapter.infer_frame(_png_bytes(), timestamp_ms=0)
        assert len(dets) == 1
        assert dets[0].object == "gearbox_housing"
    finally:
        _clear_settings()


def test_qwen_adapter_prepare_image_downscales() -> None:
    """降分辨率：大图被缩到 ≤640 宽并压缩。"""
    import io

    from gongxu.vision.adapters_qwen import _prepare_image

    big = Image.new("RGB", (1920, 1080), (10, 20, 30))
    buf = io.BytesIO()
    big.save(buf, format="JPEG", quality=95)
    prepared = _prepare_image(buf.getvalue())
    assert len(prepared) < len(buf.getvalue())  # 变小
    out = Image.open(io.BytesIO(prepared))
    assert out.width <= 640
    assert out.height <= 640


def test_gearbox_sop_aligns_observations() -> None:
    """构造齿轮箱检测流 → step1/step2 判定 passed（演示 SOP 对齐）。"""
    from gongxu.sop.fixtures import gearbox_disassembly_sop
    from gongxu.vision import align, facts

    sop = gearbox_disassembly_sop()
    obs = [
        facts.Observation(
            timestamp_ms=2000,
            hand="both",
            tool="",
            object="anti_vibration_handle",
            action="hold",
            relation="both_operates_anti_vibration_handle",
            confidence=0.85,
            frame_hash="a",
        ),
        facts.Observation(
            timestamp_ms=8000,
            hand="both",
            tool="",
            object="gearbox_housing",
            action="hold",
            relation="both_operates_gearbox_housing",
            confidence=0.75,
            frame_hash="b",
        ),
    ]
    al = align.align_observations(sop.steps, obs)
    by_index = {r.step.index: r.judgement for r in al.step_results}
    assert by_index[1] == "passed"
    assert by_index[2] == "passed"
    assert by_index[3] == "insufficient_evidence"


class _FakeAdapter(VisionAdapter):
    name = "fake"

    def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
        return [
            Detection(
                timestamp_ms=timestamp_ms,
                hand="right",
                tool="wrench",
                object="locking_nut",
                action="loosen",
                relation="right_holds_wrench",
                confidence=0.9,
                frame_hash="fake",
            )
        ]

    def model_version(self) -> str:
        return "fake@v1"


class _FakeBatchAdapter(_FakeAdapter):
    def __init__(self) -> None:
        self.timestamps: list[int] = []

    def infer_batch(self, frames: list[tuple[bytes, int]]) -> list[list[Detection]]:
        self.timestamps.extend(timestamp_ms for _image, timestamp_ms in frames)
        return [
            self.infer_frame(image, timestamp_ms=timestamp_ms) for image, timestamp_ms in frames
        ]


class _TransitionLocalAdapter(VisionAdapter):
    name = "transition-local"

    def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
        step_index = 1 if timestamp_ms <= 4_000 else 2
        return [
            Detection(
                timestamp_ms=timestamp_ms,
                hand="right",
                tool="wrench",
                object="locking_nut",
                action="loosen",
                relation="right_holds_wrench",
                confidence=0.9,
                frame_hash=f"frame-{timestamp_ms}",
                phase="during",
                candidate_step_index=step_index,
            )
        ]


class _TransitionBatchAdapter(_TransitionLocalAdapter):
    def infer_batch(self, frames: list[tuple[bytes, int]]) -> list[list[Detection]]:
        return [
            self.infer_frame(image, timestamp_ms=timestamp_ms) for image, timestamp_ms in frames
        ]


def _write_fake_frames(video_path: Path, frames_dir: Path, **kwargs: object) -> None:
    frames_dir.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (frames_dir / f"kf_{i:04d}.jpg").write_bytes(_png_bytes())


def test_pipeline_infer_detections_uses_adapter(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预录管线接真适配器：抽帧 → adapter 逐帧推理 → 收集 Detection。"""
    from gongxu.assets import service as asset_service
    from gongxu.db.models import AssessmentSession, AssessmentStatus
    from gongxu.vision import pipeline

    fake_video = Path(get_settings().data_root) / "runtime" / "input.mp4"
    fake_video.parent.mkdir(parents=True, exist_ok=True)
    fake_video.write_bytes(b"\x00" * 100)
    asset_id = asset_service.register_asset(
        db, kind="video", filename="input.mp4", temp_path=fake_video, license_note="test"
    ).id
    session = AssessmentSession(
        process_key="impact_grinder_safe_subassembly",
        mode="recorded",
        status=AssessmentStatus.created,
        input_asset_id=asset_id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    monkeypatch.setattr(pipeline.asset_service, "decrypt_to_temp", lambda *a, **k: fake_video)
    monkeypatch.setattr(pipeline, "extract_keyframes_uniform", _write_fake_frames)
    monkeypatch.setattr(pipeline, "resolve_adapter", lambda _db, **_kw: _FakeAdapter())

    detections, total = pipeline._infer_detections(db, session, work_dir=fake_video.parent)
    assert total == 3
    assert all(d.tool == "wrench" for d in detections)


def test_pipeline_batch_timestamps_remain_global_across_batches(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gongxu.assets import service as asset_service
    from gongxu.db.models import AssessmentSession, AssessmentStatus
    from gongxu.media.video import SampledFrame
    from gongxu.vision import pipeline

    fake_video = Path(get_settings().data_root) / "runtime" / "timestamp-input.mp4"
    fake_video.parent.mkdir(parents=True, exist_ok=True)
    fake_video.write_bytes(b"\x00" * 100)
    asset_id = asset_service.register_asset(
        db, kind="video", filename="input.mp4", temp_path=fake_video, license_note="test"
    ).id
    session = AssessmentSession(
        process_key="impact_grinder_safe_subassembly",
        mode="recorded",
        status=AssessmentStatus.created,
        input_asset_id=asset_id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    def _sampled(_video: Path, frames_dir: Path, **_kwargs: object) -> list[SampledFrame]:
        frames_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for i in range(10):
            path = frames_dir / f"kf_{i:04d}.jpg"
            path.write_bytes(_png_bytes())
            result.append(
                SampledFrame(
                    path=path,
                    frame_index=i,
                    timestamp_ms=i * 2500,
                    source_pts=i * 75,
                )
            )
        return result

    adapter = _FakeBatchAdapter()
    progress_updates: list[tuple[int, int]] = []
    monkeypatch.setattr(pipeline.asset_service, "decrypt_to_temp", lambda *a, **k: fake_video)
    monkeypatch.setattr(pipeline, "extract_keyframes_uniform", _sampled)
    monkeypatch.setattr(pipeline, "resolve_adapter", lambda _db, **_kw: adapter)

    detections, total = pipeline._infer_detections(
        db,
        session,
        work_dir=fake_video.parent,
        inference_progress=lambda analyzed, frame_total: progress_updates.append(
            (analyzed, frame_total)
        ),
    )

    assert total == 10
    assert adapter.timestamps == [i * 2500 for i in range(10)]
    assert [d.timestamp_ms for d in detections] == adapter.timestamps
    assert [d.frame_index for d in detections] == list(range(10))
    assert progress_updates == [(4, 10), (8, 10), (10, 10)]


def test_adaptive_timestamps_never_exceed_combined_48_frame_limit(tmp_path: Path) -> None:
    from gongxu.media.video import SampledFrame
    from gongxu.vision import pipeline

    first_frames = [
        SampledFrame(
            path=tmp_path / f"first-{index}.jpg",
            frame_index=index,
            timestamp_ms=index * 4_000,
        )
        for index in range(24)
    ]
    detections = [
        Detection(
            timestamp_ms=frame.timestamp_ms,
            tool="wrench" if index % 2 else "",
            object="locking_nut" if index % 2 == 0 else "guard_nut",
            action="loosen" if index % 2 else "remove",
            confidence=0.9,
            candidate_step_index=1 if index % 2 else 2,
            phase="during",
        )
        for index, frame in enumerate(first_frames)
    ]

    adaptive = pipeline._adaptive_sample_timestamps(first_frames, detections)

    assert len(adaptive) == 24
    assert len(first_frames) + len(adaptive) == 48
    assert adaptive == sorted(adaptive)
    assert len(set(adaptive)) == len(adaptive)
    assert set(adaptive).isdisjoint({frame.timestamp_ms for frame in first_frames})


def test_coverage_timestamps_target_only_steps_without_semantic_evidence(
    tmp_path: Path,
) -> None:
    from gongxu.media.video import SampledFrame
    from gongxu.vision import pipeline

    first_frames = [
        SampledFrame(
            path=tmp_path / f"first-{index}.jpg",
            frame_index=index,
            timestamp_ms=index * 30_000,
        )
        for index in range(4)
    ]
    steps = [
        SopStep(
            id=f"step-{index}",
            index=index,
            name=f"步骤 {index}",
            predecessors=[index - 1] if index > 1 else [],
            objects=[f"part_{index}"],
            actions=[f"action_{index}"],
            observable_checks=[f"步骤 {index} 完成"],
            expected_duration_s=30,
            weight=1.0,
        )
        for index in range(1, 4)
    ]
    detections = [
        Detection(
            timestamp_ms=0,
            object="part_1",
            action="action_1",
            confidence=0.9,
            candidate_step_index=1,
        ),
        Detection(
            timestamp_ms=90_000,
            object="part_3",
            action="action_3",
            confidence=0.9,
            candidate_step_index=3,
        ),
    ]

    timestamps = pipeline._coverage_sample_timestamps(
        first_frames,
        detections,
        steps,
        existing_timestamps={frame.timestamp_ms for frame in first_frames},
        capacity=12,
    )

    assert timestamps == [36_000, 42_000, 48_000, 54_000]


def test_coverage_timestamps_allocate_one_round_before_densifying(
    tmp_path: Path,
) -> None:
    from gongxu.media.video import SampledFrame
    from gongxu.vision import pipeline

    first_frames = [
        SampledFrame(tmp_path / "start.jpg", 0, 0),
        SampledFrame(tmp_path / "end.jpg", 1, 80_000),
    ]
    steps = [
        SopStep(
            id=f"step-{index}",
            index=index,
            name=f"步骤 {index}",
            objects=[f"part_{index}"],
            actions=[f"action_{index}"],
            observable_checks=[f"步骤 {index} 完成"],
            expected_duration_s=20,
            weight=1.0,
        )
        for index in range(1, 5)
    ]

    timestamps = pipeline._coverage_sample_timestamps(
        first_frames,
        [],
        steps,
        existing_timestamps={0, 80_000},
        capacity=4,
    )

    assert timestamps == [4_000, 24_000, 44_000, 64_000]


def test_reference_timeline_slots_preserve_standard_video_rhythm() -> None:
    from gongxu.sop.schema import EvidenceRef
    from gongxu.vision import pipeline

    reference_timestamps = (10_000, 20_000, 80_000)
    steps = [
        SopStep(
            id=f"step-{index}",
            index=index,
            name=f"步骤 {index}",
            objects=[f"part_{index}"],
            actions=[f"action_{index}"],
            observable_checks=[f"步骤 {index} 完成"],
            expected_duration_s=20,
            weight=1.0,
            evidence_refs=[
                EvidenceRef(
                    kind="video",
                    ref=f"{reference_timestamps[index - 1]}ms",
                    timestamp_ms=reference_timestamps[index - 1],
                )
            ],
        )
        for index in range(1, 4)
    ]

    slots = pipeline._reference_timeline_slots(
        steps,
        timeline_start=0,
        timeline_end=160_000,
    )

    assert slots == {
        1: (0, 30_000),
        2: (30_000, 100_000),
        3: (100_000, 160_000),
    }


def test_reference_plan_rechecks_steps_that_have_generic_candidates(
    tmp_path: Path,
) -> None:
    from gongxu.media.video import SampledFrame
    from gongxu.vision import pipeline

    frames = [
        SampledFrame(tmp_path / "start.jpg", 0, 0),
        SampledFrame(tmp_path / "end.jpg", 1, 40_000),
    ]
    steps = [
        SopStep(
            id=f"step-{index}",
            index=index,
            name=f"步骤 {index}",
            objects=[f"part_{index}"],
            actions=[f"action_{index}"],
            observable_checks=[f"步骤 {index} 完成"],
            expected_duration_s=20,
            weight=1.0,
        )
        for index in (1, 2)
    ]
    generic = [
        Detection(
            timestamp_ms=2_000,
            object="part_1",
            action="action_1",
            candidate_step_index=1,
            confidence=0.9,
        )
    ]

    plan = pipeline._coverage_sample_plan(
        frames,
        generic,
        steps,
        existing_timestamps={0, 40_000},
        capacity=2,
        eligible_step_indices={1, 2},
        include_observed_steps=True,
    )

    assert {step_index for step_index, _timestamp in plan} == {1, 2}


def test_reference_frames_with_shared_timestamp_are_checked_for_every_step(
    tmp_path: Path,
) -> None:
    from gongxu.media.video import SampledFrame
    from gongxu.vision import pipeline

    shared = SampledFrame(tmp_path / "shared.jpg", 0, 19_800)
    later = SampledFrame(tmp_path / "later.jpg", 1, 55_700)

    grouped = pipeline._coverage_frames_by_step(
        [(1, 19_800), (2, 19_800), (3, 55_700)],
        [shared, later],
    )

    assert grouped == {1: [shared], 2: [shared], 3: [later]}


def test_reference_plan_counts_shared_timestamp_as_one_frame(
    tmp_path: Path,
) -> None:
    from gongxu.media.video import SampledFrame
    from gongxu.sop.schema import EvidenceRef
    from gongxu.vision import pipeline

    frames = [
        SampledFrame(tmp_path / "start.jpg", 0, 0),
        SampledFrame(tmp_path / "end.jpg", 1, 100_000),
    ]
    steps = [
        SopStep(
            id=f"step-{index}",
            index=index,
            name=f"步骤 {index}",
            objects=[f"part_{index}"],
            actions=[f"action_{index}"],
            observable_checks=[f"步骤 {index} 完成"],
            expected_duration_s=20,
            weight=1.0,
            evidence_refs=[EvidenceRef(kind="video", ref="20000ms", timestamp_ms=20_000)],
        )
        for index in (1, 2)
    ]

    plan = pipeline._coverage_sample_plan(
        frames,
        [],
        steps,
        existing_timestamps={0, 100_000},
        capacity=1,
        eligible_step_indices={1, 2},
        include_observed_steps=True,
    )

    assert plan == [(1, 20_000), (2, 20_000)]


def test_reference_plan_prefers_observed_action_timing_over_global_video_ratio(
    tmp_path: Path,
) -> None:
    from gongxu.media.video import SampledFrame
    from gongxu.sop.schema import EvidenceRef
    from gongxu.vision import pipeline

    frames = [
        SampledFrame(tmp_path / "start.jpg", 0, 0),
        SampledFrame(tmp_path / "end.jpg", 1, 160_000),
    ]
    step = SopStep(
        id="step-7",
        index=7,
        name="安装后侧后车架销",
        objects=["rear_rear_chassis_pin"],
        actions=["install_rear_rear_chassis_pin"],
        observable_checks=["安装正确"],
        expected_duration_s=20,
        weight=1.0,
        evidence_refs=[EvidenceRef(kind="video", ref="84000ms", timestamp_ms=84_000)],
    )
    observed = Detection(
        timestamp_ms=85_000,
        object="rear_rear_chassis_pin",
        action="install_rear_rear_chassis_pin",
        candidate_step_index=7,
        confidence=0.9,
    )

    plan = pipeline._coverage_sample_plan(
        frames,
        [observed],
        [step],
        existing_timestamps={0, 160_000},
        capacity=4,
        eligible_step_indices={7},
        include_observed_steps=True,
    )

    timestamps = [timestamp for _step_index, timestamp in plan]
    assert timestamps == [77_800, 82_600, 87_400, 92_200]
    assert max(timestamps) < 100_000


def test_two_pass_batch_sampling_adds_ordered_frames_around_transition(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gongxu.assets import service as asset_service
    from gongxu.db.models import AssessmentSession, AssessmentStatus
    from gongxu.media.video import SampledFrame
    from gongxu.vision import pipeline

    fake_video = tmp_path / "batch-adaptive.mp4"
    fake_video.write_bytes(b"video")
    asset_id = asset_service.register_asset(
        db, kind="video", filename=fake_video.name, temp_path=fake_video, license_note="test"
    ).id
    session = AssessmentSession(
        process_key="impact_grinder_safe_subassembly",
        mode="recorded",
        status=AssessmentStatus.created,
        input_asset_id=asset_id,
    )
    db.add(session)
    db.commit()

    first_timestamps = [0, 4_000, 8_000, 12_000]

    def _first_pass(_video: Path, frames_dir: Path, **_kwargs: object) -> list[SampledFrame]:
        frames_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index, timestamp_ms in enumerate(first_timestamps):
            path = frames_dir / f"first-{index}.jpg"
            path.write_bytes(_png_bytes())
            frames.append(SampledFrame(path, index, timestamp_ms))
        return frames

    requested: list[int] = []

    def _second_pass(
        _video: Path,
        frames_dir: Path,
        *,
        timestamps_ms: list[int],
        frame_index_start: int,
        **_kwargs: object,
    ) -> list[SampledFrame]:
        requested.extend(timestamps_ms)
        frames_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for offset, timestamp_ms in enumerate(timestamps_ms):
            path = frames_dir / f"adaptive-{offset}.jpg"
            path.write_bytes(_png_bytes())
            frames.append(SampledFrame(path, frame_index_start + offset, timestamp_ms))
        return frames

    progress_updates: list[tuple[int, int]] = []
    monkeypatch.setattr(pipeline.asset_service, "decrypt_to_temp", lambda *a, **k: fake_video)
    monkeypatch.setattr(pipeline, "extract_keyframes_uniform", _first_pass)
    monkeypatch.setattr(pipeline, "extract_frames_at", _second_pass)
    monkeypatch.setattr(pipeline, "resolve_adapter", lambda _db, **_kw: _TransitionBatchAdapter())

    detections, total = pipeline._infer_detections(
        db,
        session,
        work_dir=tmp_path,
        inference_progress=lambda processed, frame_total: progress_updates.append(
            (processed, frame_total)
        ),
    )

    assert requested == [5_000, 5_500, 6_500, 7_000]
    assert total == 8
    assert [d.timestamp_ms for d in detections] == [
        0,
        4_000,
        5_000,
        5_500,
        6_500,
        7_000,
        8_000,
        12_000,
    ]
    assert [d.frame_index for d in detections] == list(range(8))
    assert progress_updates == [(4, 8), (8, 8)]


def test_two_pass_local_sampling_uses_same_global_timeline(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gongxu.assets import service as asset_service
    from gongxu.db.models import AssessmentSession, AssessmentStatus
    from gongxu.media.video import SampledFrame
    from gongxu.vision import pipeline

    fake_video = tmp_path / "local-adaptive.mp4"
    fake_video.write_bytes(b"video")
    asset_id = asset_service.register_asset(
        db, kind="video", filename=fake_video.name, temp_path=fake_video, license_note="test"
    ).id
    session = AssessmentSession(
        process_key="impact_grinder_safe_subassembly",
        mode="recorded",
        status=AssessmentStatus.created,
        input_asset_id=asset_id,
    )
    db.add(session)
    db.commit()

    def _first_pass(_video: Path, frames_dir: Path, **_kwargs: object) -> list[SampledFrame]:
        frames_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for index, timestamp_ms in enumerate((0, 4_000, 8_000)):
            path = frames_dir / f"first-{index}.jpg"
            path.write_bytes(_png_bytes())
            result.append(SampledFrame(path, index, timestamp_ms))
        return result

    def _second_pass(
        _video: Path,
        frames_dir: Path,
        *,
        timestamps_ms: list[int],
        frame_index_start: int,
        **_kwargs: object,
    ) -> list[SampledFrame]:
        frames_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for offset, timestamp_ms in enumerate(timestamps_ms):
            path = frames_dir / f"adaptive-{offset}.jpg"
            path.write_bytes(_png_bytes())
            result.append(SampledFrame(path, frame_index_start + offset, timestamp_ms))
        return result

    monkeypatch.setattr(pipeline.asset_service, "decrypt_to_temp", lambda *a, **k: fake_video)
    monkeypatch.setattr(pipeline, "extract_keyframes_uniform", _first_pass)
    monkeypatch.setattr(pipeline, "extract_frames_at", _second_pass)
    monkeypatch.setattr(pipeline, "resolve_adapter", lambda _db, **_kw: _TransitionLocalAdapter())

    detections, total = pipeline._infer_detections(db, session, work_dir=tmp_path)

    assert total == 7
    assert [d.timestamp_ms for d in detections] == [
        0,
        4_000,
        5_000,
        5_500,
        6_500,
        7_000,
        8_000,
    ]
    assert [d.frame_index for d in detections] == list(range(7))


def test_second_pass_failure_keeps_first_pass_results(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gongxu.assets import service as asset_service
    from gongxu.db.models import AssessmentSession, AssessmentStatus
    from gongxu.media.video import SampledFrame
    from gongxu.vision import pipeline
    from gongxu.vision.adapters import AdapterUnavailableError

    fake_video = tmp_path / "adaptive-failure.mp4"
    fake_video.write_bytes(b"video")
    asset_id = asset_service.register_asset(
        db, kind="video", filename=fake_video.name, temp_path=fake_video, license_note="test"
    ).id
    session = AssessmentSession(
        process_key="impact_grinder_safe_subassembly",
        mode="recorded",
        status=AssessmentStatus.created,
        input_asset_id=asset_id,
    )
    db.add(session)
    db.commit()

    first_timestamps = {0, 4_000, 8_000}

    class _FailAdaptiveAdapter(_TransitionLocalAdapter):
        def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
            if timestamp_ms not in first_timestamps:
                raise AdapterUnavailableError("模拟第二遍推理失败")
            return super().infer_frame(image_bytes, timestamp_ms=timestamp_ms)

    def _first_pass(_video: Path, frames_dir: Path, **_kwargs: object) -> list[SampledFrame]:
        frames_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for index, timestamp_ms in enumerate(sorted(first_timestamps)):
            path = frames_dir / f"first-{index}.jpg"
            path.write_bytes(_png_bytes())
            result.append(SampledFrame(path, index, timestamp_ms))
        return result

    def _second_pass(
        _video: Path,
        frames_dir: Path,
        *,
        timestamps_ms: list[int],
        frame_index_start: int,
        **_kwargs: object,
    ) -> list[SampledFrame]:
        frames_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for offset, timestamp_ms in enumerate(timestamps_ms):
            path = frames_dir / f"adaptive-{offset}.jpg"
            path.write_bytes(_png_bytes())
            result.append(SampledFrame(path, frame_index_start + offset, timestamp_ms))
        return result

    progress_updates: list[tuple[int, int]] = []
    monkeypatch.setattr(pipeline.asset_service, "decrypt_to_temp", lambda *a, **k: fake_video)
    monkeypatch.setattr(pipeline, "extract_keyframes_uniform", _first_pass)
    monkeypatch.setattr(pipeline, "extract_frames_at", _second_pass)
    monkeypatch.setattr(pipeline, "resolve_adapter", lambda _db, **_kw: _FailAdaptiveAdapter())

    with caplog.at_level("WARNING"):
        detections, total = pipeline._infer_detections(
            db,
            session,
            work_dir=tmp_path,
            inference_progress=lambda processed, frame_total: progress_updates.append(
                (processed, frame_total)
            ),
        )

    assert total == 3
    assert [d.timestamp_ms for d in detections] == [0, 4_000, 8_000]
    assert progress_updates == [(1, 3), (2, 3), (3, 3)]
    assert "retaining first pass" in caplog.text


def test_pipeline_unknown_process_fails_closed(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gongxu.assets import service as asset_service
    from gongxu.db.models import AssessmentSession, AssessmentStatus
    from gongxu.vision import pipeline

    fake_video = Path(get_settings().data_root) / "runtime" / "unknown-input.mp4"
    fake_video.parent.mkdir(parents=True, exist_ok=True)
    fake_video.write_bytes(b"\x00" * 100)
    asset_id = asset_service.register_asset(
        db, kind="video", filename="input.mp4", temp_path=fake_video, license_note="test"
    ).id
    session = AssessmentSession(
        process_key="unregistered_process",
        mode="recorded",
        status=AssessmentStatus.created,
        input_asset_id=asset_id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    monkeypatch.setenv("GONGXU_VISION_BACKEND", "qwen")
    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "test-model")
    _clear_settings()
    monkeypatch.setattr(pipeline.asset_service, "decrypt_to_temp", lambda *a, **k: fake_video)
    monkeypatch.setattr(pipeline, "extract_keyframes_uniform", _write_fake_frames)

    try:
        with pytest.raises(pipeline.RejectError) as exc_info:
            pipeline._infer_detections(db, session, work_dir=fake_video.parent)
        assert exc_info.value.reason_code == "adapter_unimplemented"
        assert "当前 SOP 未声明" in exc_info.value.reason
    finally:
        _clear_settings()


def test_pipeline_infer_detections_adapter_error_rejects(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """adapter 抛 AdapterUnavailableError → 结构化拒判（不产生伪分数）。"""
    from gongxu.assets import service as asset_service
    from gongxu.db.models import AssessmentSession, AssessmentStatus
    from gongxu.vision import pipeline
    from gongxu.vision.adapters import AdapterUnavailableError

    fake_video = Path(get_settings().data_root) / "runtime" / "input.mp4"
    fake_video.parent.mkdir(parents=True, exist_ok=True)
    fake_video.write_bytes(b"\x00" * 100)
    asset_id = asset_service.register_asset(
        db, kind="video", filename="input.mp4", temp_path=fake_video, license_note="test"
    ).id
    session = AssessmentSession(
        process_key="impact_grinder_safe_subassembly",
        mode="recorded",
        status=AssessmentStatus.created,
        input_asset_id=asset_id,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    class _FailingAdapter(VisionAdapter):
        name = "failing"

        def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
            raise AdapterUnavailableError("模拟模型失败")

    monkeypatch.setattr(pipeline.asset_service, "decrypt_to_temp", lambda *a, **k: fake_video)
    monkeypatch.setattr(pipeline, "extract_keyframes_uniform", _write_fake_frames)
    monkeypatch.setattr(pipeline, "resolve_adapter", lambda _db, **_kw: _FailingAdapter())

    with pytest.raises(pipeline.RejectError) as exc_info:
        pipeline._infer_detections(db, session, work_dir=fake_video.parent)
    assert exc_info.value.reason_code == "adapter_unavailable"


def test_pipeline_transient_adapter_error_requests_job_retry(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider 网络抖动不得固化为一次已完成的 inconclusive 报告。"""
    from gongxu.assets import service as asset_service
    from gongxu.db.models import AssessmentSession, AssessmentStatus
    from gongxu.vision import pipeline
    from gongxu.vision.adapters import AdapterTransientError

    fake_video = Path(get_settings().data_root) / "runtime" / "transient.mp4"
    fake_video.parent.mkdir(parents=True, exist_ok=True)
    fake_video.write_bytes(b"\x00" * 100)
    asset_id = asset_service.register_asset(
        db, kind="video", filename="transient.mp4", temp_path=fake_video, license_note="test"
    ).id
    session = AssessmentSession(
        process_key="impact_grinder_safe_subassembly",
        mode="recorded",
        status=AssessmentStatus.created,
        input_asset_id=asset_id,
    )
    db.add(session)
    db.commit()

    class _TransientAdapter(VisionAdapter):
        name = "transient"

        def infer_frame(self, image_bytes: bytes, *, timestamp_ms: int) -> list[Detection]:
            raise AdapterTransientError("模拟 Provider 超时")

    monkeypatch.setattr(pipeline.asset_service, "decrypt_to_temp", lambda *a, **k: fake_video)
    monkeypatch.setattr(pipeline, "extract_keyframes_uniform", _write_fake_frames)
    monkeypatch.setattr(pipeline, "resolve_adapter", lambda _db, **_kw: _TransientAdapter())

    with pytest.raises(pipeline.VisionPipelineError, match="模拟 Provider 超时"):
        pipeline._infer_detections(db, session, work_dir=fake_video.parent)
