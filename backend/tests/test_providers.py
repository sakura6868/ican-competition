"""Provider 契约测试：Replay 离线可用、确定性；Qwen 错误映射。"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from gongxu.config import get_settings
from gongxu.providers import factory
from gongxu.providers.base import (
    ProviderBudgetExceededError,
    ProviderError,
    ProviderRateLimitError,
    ReferenceSegment,
)
from gongxu.providers.replay import ReplayProvider


def _segment() -> ReferenceSegment:
    return ReferenceSegment(
        segment_id="seg_1",
        keyframes=["kf_a.jpg", "kf_b.jpg"],
        asr_text="先断电，再等待停转",
        ocr_text="注意：佩戴护目镜",
        detection_summary={"hands": 2, "tools": ["wrench"]},
        rag_chunks=[{"page": "p12", "text": "安全规范"}],
    )


def test_replay_probe() -> None:
    provider = ReplayProvider()
    result = provider.probe()
    assert result["ok"] is True
    assert result["mode"] == "replay"


def test_replay_compile_sop_offline() -> None:
    provider = ReplayProvider()
    sop, receipt = provider.compile_sop({"steps_found": 7}, [])
    assert sop["process_key"] == "impact_grinder_safe_subassembly"
    assert len(sop["steps"]) == 7
    assert receipt.provider == "replay"
    assert receipt.mode == "replay"
    assert receipt.response_digest


def test_replay_deterministic_same_input() -> None:
    """相同输入必须产生完全相同的输出（handoff §3.8）。"""
    provider = ReplayProvider()
    seg = _segment()
    a1, r1 = provider.analyze_reference_segment(seg)
    a2, r2 = provider.analyze_reference_segment(seg)
    assert a1 == a2
    assert r1.response_digest == r2.response_digest

    sop1, rp1 = provider.compile_sop(a1, [{"page": "p12"}])
    sop2, rp2 = provider.compile_sop(a2, [{"page": "p12"}])
    assert sop1 == sop2
    assert rp1.response_digest == rp2.response_digest


def test_factory_replay_requires_explicit_opt_in(monkeypatch) -> None:
    # 产品路径不得把固定 fixture 当作真实视频分析结果。
    monkeypatch.setenv("GONGXU_LLM_MODE", "replay")
    get_settings.cache_clear()
    with pytest.raises(ProviderError, match="工程回放模式"):
        factory.get_provider()
    provider = factory.get_provider(allow_replay=True)
    assert isinstance(provider, ReplayProvider)
    get_settings.cache_clear()


def _install_fake_httpx(monkeypatch, *, status: int, body: dict):
    """把 httpx.post 替换为返回固定响应的假实现。"""
    calls: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None, trust_env=None):
        assert trust_env is False
        calls.append(json)
        resp = httpx.Response(status, json=body, request=httpx.Request("POST", url))
        return resp

    monkeypatch.setattr("gongxu.providers.qwen.httpx.post", fake_post)
    return calls


def test_qwen_analyze_sends_images_as_data_uri(monkeypatch, tmp_path) -> None:
    """多模态请求必须真正发送关键帧图像（base64 data URI），不是路径字符串（P1-A.5）。"""
    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen38max")
    get_settings.cache_clear()
    calls = _install_fake_httpx(
        monkeypatch,
        status=200,
        body={"choices": [{"message": {"content": '{"steps_found": 7}'}}]},
    )
    frame = tmp_path / "kf_0001.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # 伪 JPEG 头
    provider = Qwen38MaxProvider()
    segment = ReferenceSegment(segment_id="seg_1", keyframes=[str(frame)], asr_text="先断电")
    analysis, receipt = provider.analyze_reference_segment(segment)
    assert analysis["steps_found"] == 7
    content = calls[0]["messages"][0]["content"]
    assert isinstance(content, list)
    image_part = next(p for p in content if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "kf_0001.jpg" not in image_part["image_url"]["url"]  # 不是路径字符串
    assert receipt.extra["keyframes_sent"] == 1
    get_settings.cache_clear()


def test_qwen_analyze_batches_all_short_video_frames(monkeypatch, tmp_path) -> None:
    """不足 24 帧时全部按连续时间批次发送，并保留真实全局时间。"""
    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen38max")
    get_settings.cache_clear()
    calls = _install_fake_httpx(
        monkeypatch,
        status=200,
        body={"choices": [{"message": {"content": '{"steps_found": 3}'}}]},
    )
    paths: list[str] = []
    for index in range(10):
        frame = tmp_path / f"kf_{index:04d}.jpg"
        frame.write_bytes(f"frame-{index}".encode())
        paths.append(str(frame))
    segment = ReferenceSegment(
        segment_id="full-span",
        keyframes=paths,
        keyframe_timestamps_ms=[index * 10_000 for index in range(10)],
        start_ms=0,
        end_ms=90_000,
    )

    _analysis, receipt = Qwen38MaxProvider().analyze_reference_segment(segment)

    assert len(calls) == 2
    contents = [call["messages"][0]["content"] for call in calls]
    image_parts = [part for content in contents for part in content if part["type"] == "image_url"]
    sent_payloads = [
        base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) for part in image_parts
    ]
    assert sent_payloads == [f"frame-{index}".encode() for index in range(10)]
    prompts = [json.loads(content[0]["text"]) for content in contents]
    sent_metadata = [item for prompt in prompts for item in prompt["keyframes_sent"]]
    assert [item["source_frame_index"] for item in sent_metadata] == list(range(10))
    assert [item["timestamp_ms"] for item in sent_metadata] == [
        index * 10_000 for index in range(10)
    ]
    assert [prompt["batch_index"] for prompt in prompts] == [0, 1]
    assert all(prompt["batch_count"] == 2 for prompt in prompts)
    assert receipt.extra["batch_count"] == 2
    assert receipt.extra["selected_frame_indices"] == list(range(10))
    assert receipt.extra["selected_timestamps_ms"][-1] == 90_000
    get_settings.cache_clear()


def test_qwen_analyze_24_frames_uses_four_batches_and_stable_aggregate(
    monkeypatch, tmp_path
) -> None:
    """24 帧必须分四次发送；首尾覆盖且跨批候选按源时间稳定聚合。"""
    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen38max")
    get_settings.cache_clear()
    calls: list[dict] = []
    progress_updates: list[tuple[int, int]] = []
    json_module = json

    def fake_post(url, json=None, headers=None, timeout=None, trust_env=None):
        assert trust_env is False
        calls.append(json)
        content = json["messages"][0]["content"]
        prompt = json_module.loads(content[0]["text"])
        batch_index = prompt["batch_index"]
        first_timestamp = prompt["keyframes_sent"][0]["timestamp_ms"]
        # Model output deliberately uses aliases and reverses candidates inside each batch.
        result = {
            "steps": [
                {"name": f"步骤-{batch_index}-后", "timestamp_ms": first_timestamp + 5000},
                {"name": f"步骤-{batch_index}-前", "timestamp_ms": first_timestamp},
            ],
            "key_tools": [f"工具-{batch_index}"],
            "objects": [f"对象-{batch_index}"],
            "actions": [f"动作-{batch_index}"],
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json_module.dumps(result)}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("gongxu.providers.qwen.httpx.post", fake_post)
    paths: list[str] = []
    for index in range(24):
        frame = tmp_path / f"kf_{index:04d}.jpg"
        frame.write_bytes(f"frame-{index}".encode())
        paths.append(str(frame))
    segment = ReferenceSegment(
        segment_id="four-batches",
        keyframes=paths,
        keyframe_timestamps_ms=[index * 1000 for index in range(24)],
        start_ms=0,
        end_ms=23_000,
    )

    provider = Qwen38MaxProvider()
    provider.set_reference_progress_callback(
        lambda completed, total: progress_updates.append((completed, total))
    )
    analysis, receipt = provider.analyze_reference_segment(segment)

    assert len(calls) == 4
    prompts = [json.loads(call["messages"][0]["content"][0]["text"]) for call in calls]
    sent_indices = [
        frame["source_frame_index"] for prompt in prompts for frame in prompt["keyframes_sent"]
    ]
    assert sent_indices == list(range(24))
    assert sent_indices[0] == 0
    assert sent_indices[-1] == 23
    assert all(len(prompt["keyframes_sent"]) == 6 for prompt in prompts)
    assert [prompt["batch_index"] for prompt in prompts] == [0, 1, 2, 3]

    assert analysis["analysis_format"] == "timeline_batches_v1"
    assert len(analysis["batch_analyses"]) == 4
    assert [item["name"] for item in analysis["step_candidates"]] == [
        "步骤-0-前",
        "步骤-0-后",
        "步骤-1-前",
        "步骤-1-后",
        "步骤-2-前",
        "步骤-2-后",
        "步骤-3-前",
        "步骤-3-后",
    ]
    assert [item["name"] for item in analysis["tool_candidates"]] == [
        f"工具-{index}" for index in range(4)
    ]
    assert receipt.extra["batch_count"] == 4
    assert progress_updates == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert receipt.extra["keyframes_sent"] == 24
    assert receipt.extra["selected_frame_indices"] == list(range(24))
    assert receipt.extra["selected_timestamps_ms"][-1] == 23_000
    get_settings.cache_clear()


def test_qwen_coaching_prompts_are_structured_and_score_guarded(monkeypatch) -> None:
    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen38max")
    get_settings.cache_clear()
    responses = [
        {"explanation": "先复练工具选择。"},
        {"focus_steps": ["step_02"], "repeat_count": 3, "note": "连续完成三次。"},
    ]
    calls: list[dict] = []
    json_module = json

    def fake_post(url, json=None, headers=None, timeout=None, trust_env=None):
        del headers, timeout
        assert trust_env is False
        calls.append(json)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json_module.dumps(responses.pop(0))}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("gongxu.providers.qwen.httpx.post", fake_post)
    provider = Qwen38MaxProvider()
    explanation, _ = provider.explain_assessment(
        [{"step_id": "step_02", "judgement": "failed", "reason": "工具错误"}],
        {"result": "fail", "evidence_coverage": 1.0},
    )
    plan, _ = provider.generate_practice_plan(
        {"result": "fail", "steps": [{"step_id": "step_02", "judgement": "failed"}]}
    )

    assert explanation == "先复练工具选择。"
    assert plan["focus_steps"] == ["step_02"]
    prompts = [json.loads(call["messages"][0]["content"]) for call in calls]
    assert prompts[0]["response_schema"] == {"explanation": "string, 800字以内"}
    assert prompts[1]["response_schema"]["repeat_count"] == 3
    assert all("不得输出、推测或修改任何数值分数" in prompt["constraints"] for prompt in prompts)
    get_settings.cache_clear()


def test_qwen_requires_credentials(monkeypatch) -> None:
    # 清空凭据 → 构造失败

    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "")
    get_settings.cache_clear()
    with pytest.raises(ProviderError):
        Qwen38MaxProvider()
    get_settings.cache_clear()


def test_teaching_compaction_keeps_facts_and_multi_batch_evidence():
    from copy import deepcopy

    from gongxu.providers.qwen import _compact_teaching_analysis

    facts = {
        "step_candidates": [{"name": "穿绳", "timestamp_ms": 1500}],
        "uncertainties": ["穿绕位置被手遮挡"],
    }
    single = {**facts, "batch_count": 1, "batch_analyses": [{"analysis": facts}]}
    multi = {
        "batch_count": 2,
        "batch_analyses": [{"analysis": facts}, {"analysis": {"last_inspection": "展示完成绳结"}}],
    }
    analysis = {
        "analysis_format": "teaching_windows_v1",
        "duration_ms": 5000,
        "window_analyses": [
            {"start_ms": 0, "analysis": single},
            {"start_ms": 2500, "analysis": multi},
        ],
    }
    before = deepcopy(analysis)
    result = _compact_teaching_analysis(analysis)
    assert analysis == before
    assert result["window_analyses"][0]["analysis"] == {**facts, "batch_count": 1}
    assert result["window_analyses"][1]["analysis"] == multi
    assert result["duration_ms"] == 5000


def test_teaching_repairs_missing_per_step_ranges_once_and_records_both_calls(monkeypatch):
    from copy import deepcopy

    from gongxu.providers.qwen import Qwen38MaxProvider, _ChatMetadata
    from gongxu.sop.teaching import enrich_steps

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "test-only")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "test")
    get_settings.cache_clear()
    outputs = [
        {"steps": [{"name": "拉直绳索"}], "teaching": {"start_ms": 0, "end_ms": 900}},
        {
            "steps": [
                {
                    "name": "拉直绳索",
                    "completion_checks": ["绳索呈直线"],
                    "teaching": {"start_ms": 0, "end_ms": 900},
                }
            ]
        },
    ]
    calls = []

    def chat(messages, **kwargs):
        calls.append(messages)
        return deepcopy(outputs[len(calls) - 1]), _ChatMetadata(
            input_tokens=10, output_tokens=5, total_tokens=15
        )

    provider = Qwen38MaxProvider()
    monkeypatch.setattr(provider, "_chat_with_metadata", chat)
    raw, receipt = provider.compile_sop(
        {
            "analysis_format": "teaching_windows_v1",
            "source_asset_id": "source",
            "duration_ms": 1000,
            "window_analyses": [],
        },
        [],
    )
    enrich_steps(deepcopy(raw), "source", 1000)
    assert len(calls) == 2
    assert "validation_error" in calls[1][-1]["content"]
    assert receipt.input_tokens == 20 and receipt.output_tokens == 10
    assert receipt.extra["compile_attempts"] == 2 and receipt.extra["total_tokens"] == 30


def test_qwen_probe_success(monkeypatch) -> None:
    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen38max")
    get_settings.cache_clear()
    calls = _install_fake_httpx(
        monkeypatch,
        status=200,
        body={
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"ok": true, "image_visible": true, "dominant_color": "green"}'
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30},
        },
    )
    provider = Qwen38MaxProvider()
    result = provider.probe()
    assert result["ok"] is True
    assert result["vision_ok"] is True
    assert result["usage"] == {"input_tokens": 21, "output_tokens": 9, "total_tokens": 30}
    assert calls[0]["temperature"] == 0.0
    content = calls[0]["messages"][0]["content"]
    assert any(part.get("type") == "image_url" for part in content)
    get_settings.cache_clear()


def test_qwen_receipt_records_usage_without_inventing_price(monkeypatch) -> None:
    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen38max")
    get_settings.cache_clear()
    _install_fake_httpx(
        monkeypatch,
        status=200,
        body={
            "choices": [{"message": {"content": '{"steps": []}'}}],
            "usage": {"input_tokens": 12, "output_tokens": 4},
        },
    )

    _sop, receipt = Qwen38MaxProvider().compile_sop({}, [])

    assert receipt.input_tokens == 12
    assert receipt.output_tokens == 4
    assert receipt.cost_usd == 0.0
    assert receipt.extra["pricing_status"] == "not_configured"
    get_settings.cache_clear()


def test_qwen_rate_limit_maps(monkeypatch) -> None:
    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen38max")
    get_settings.cache_clear()
    _install_fake_httpx(monkeypatch, status=429, body={})
    with pytest.raises(ProviderRateLimitError):
        Qwen38MaxProvider().probe()
    get_settings.cache_clear()


def test_qwen_budget_maps(monkeypatch) -> None:
    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen38max")
    get_settings.cache_clear()
    _install_fake_httpx(monkeypatch, status=402, body={})
    with pytest.raises(ProviderBudgetExceededError):
        Qwen38MaxProvider().probe()
    get_settings.cache_clear()


def test_qwen_other_client_error_maps_without_json_retry(monkeypatch) -> None:
    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen38max")
    get_settings.cache_clear()
    calls = _install_fake_httpx(
        monkeypatch,
        status=400,
        body={"error": {"message": "unsupported image payload"}},
    )
    with pytest.raises(ProviderError, match=r"拒绝请求\(400\)"):
        Qwen38MaxProvider().probe()
    assert len(calls) == 1
    get_settings.cache_clear()


def test_qwen_retries_then_fails_on_non_json(monkeypatch) -> None:
    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen38max")
    get_settings.cache_clear()
    # 每次都返回非 JSON 内容，重试 3 次后失败
    _install_fake_httpx(
        monkeypatch, status=200, body={"choices": [{"message": {"content": "not-json"}}]}
    )
    with pytest.raises(ProviderError, match="重试 3 次"):
        Qwen38MaxProvider().probe()
    get_settings.cache_clear()


def test_qwen_duplicate_response_ok(monkeypatch) -> None:
    """相同请求返回相同响应（确定性依赖 prompt/temperature=0）。"""
    from gongxu.providers.qwen import Qwen38MaxProvider

    monkeypatch.setenv("GONGXU_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("GONGXU_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("GONGXU_LLM_MODEL", "qwen38max")
    get_settings.cache_clear()
    calls = _install_fake_httpx(
        monkeypatch,
        status=200,
        body={"choices": [{"message": {"content": '{"steps": [{"id": "s1", "name": "拔插头"}]}'}}]},
    )
    provider = Qwen38MaxProvider()
    r1 = provider.compile_sop({"a": 1}, [])
    r2 = provider.compile_sop({"a": 1}, [])
    # 两次请求的 prompt 完全相同（确定性输入）
    assert calls[0] == calls[1]
    assert r1 == r2
    get_settings.cache_clear()
