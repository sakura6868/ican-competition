"""FixtureVisionProvider：离线确定性视觉源（模型缺失时的工程链路）。

在没有手部/检测/TCN 模型时，根据 scenario 生成确定性的检测流，
模拟标准教学视频中「正确操作」「漏关键步」「错工具」三种情况。
接入真实模型后替换为本模块，接口不变。

scenario:
- pass: 全部 7 步正确完成
- missing_step_1: 漏掉关键步骤 1（断电）
- wrong_tool_step_5: 步骤 5 用错工具
"""

from __future__ import annotations

from gongxu.vision.facts import Detection

# step_index -> (hand, tool, object, action) 与 SOP fixture 对齐
_STEP_SIGNATURES: dict[int, tuple[str, str, str, str]] = {
    1: ("right", "power_plug", "power_outlet", "unplug"),
    2: ("right", "", "grinding_wheel", "wait"),
    3: ("both", "gloves", "", "wear"),
    4: ("right", "screwdriver", "guard", "remove"),
    5: ("right", "wrench", "locking_nut", "loosen"),
    6: ("right", "", "grinding_wheel", "remove"),
    7: ("right", "brush", "workbench", "clean"),
}

# The fixture is allowed to pass safety only because it explicitly emits the
# exact observable checks declared by the matching fixture SOP. These facts are
# test/demo evidence, never inferred by the scoring engine from the signatures.
_STEP_OBSERVABLE_CHECKS: dict[int, tuple[str, ...]] = {
    1: ("插头已从插座拔出", "电源线不在手上"),
    2: ("砂轮停止转动",),
    3: ("双手已戴手套", "护目镜已佩戴"),
    4: ("防护罩已取下并放稳",),
    5: ("锁紧螺母已松开",),
    6: ("砂轮已取下", "砂轮外观被检查"),
    7: ("工位无碎屑", "工具已归位"),
}


def fixture_detections(
    *, total_keyframes: int = 24, fps: float = 1.0, scenario: str = "pass"
) -> list[Detection]:
    """生成确定性检测流。每个关键帧产生一个检测，覆盖步骤签名。"""
    detections: list[Detection] = []
    steps_for_frame: list[int] = []
    for i in range(total_keyframes):
        # 每 3 帧切换一个步骤，步骤 1..7 循环
        step = (i // 3) + 1
        if step > 7:
            step = 7
        steps_for_frame.append(step)

    for i, step in enumerate(steps_for_frame):
        if scenario == "missing_step_1" and step == 1:
            continue
        hand, tool, object, action = _STEP_SIGNATURES[step]
        if scenario == "wrong_tool_step_5" and step == 5:
            tool = "hammer"  # 错工具
        detections.append(
            Detection(
                timestamp_ms=int(i * 1000 / fps),
                hand=hand,
                tool=tool,
                object=object,
                action=action,
                relation=f"{hand}_holds_{tool}" if tool else f"{hand}_operates_{object}",
                confidence=0.92,
                frame_hash=f"kf_{i:04d}",
                candidate_step_index=step,
                evidence_reason="演示夹具明确提供步骤语义与可观察检查事实",
                observable_check_results={
                    check: "passed" for check in _STEP_OBSERVABLE_CHECKS[step]
                },
            )
        )
    return detections
