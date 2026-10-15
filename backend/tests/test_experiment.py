"""实验框架测试：指标纯函数、fixture 实验归档、模型卡生成。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gongxu.ml.experiment import run_fixture_experiment, save_experiment
from gongxu.ml.metrics import edit_distance, evaluate_sequences
from gongxu.ml.model_card import render_model_card, write_model_card


def test_edit_distance() -> None:
    assert edit_distance([], [1, 2]) == 2
    assert edit_distance([1, 2, 3], [1, 2, 3]) == 0
    assert edit_distance([1, 2, 3], [1, 3]) == 1  # 漏 2


def test_evaluate_sequences_identical() -> None:
    m = evaluate_sequences([1, 2, 3], [1, 2, 3])
    assert m["step_macro_f1"] == 1.0
    assert m["miss_recall"] == 1.0
    assert m["edit_distance"] == 0


def test_evaluate_sequences_missing_step() -> None:
    m = evaluate_sequences([1, 2, 3], [1, 3])
    assert m["miss_recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["step_recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["step_macro_f1"] < 1.0


def test_fixture_experiment_archives_real_metrics() -> None:
    """归档实验必须是确定性的（固定种子）。"""
    exp = run_fixture_experiment(seed=42)
    exp2 = run_fixture_experiment(seed=42)
    assert exp["aggregate"] == exp2["aggregate"]  # 可复现
    assert exp["validation"] == "experimental_unvalidated"
    assert "pass" in {s["scenario"] for s in exp["per_scenario"]}
    # 目标不达标时不得篡改（本实验门槛是计划目标，不代表 fixture 成绩）
    assert "targets" in exp


def test_save_and_load_experiment(tmp_path: Path) -> None:
    exp = run_fixture_experiment()
    path = save_experiment(exp, target=tmp_path / "training-metrics.json")
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["aggregate"] == exp["aggregate"]


def test_model_card_render() -> None:
    exp = run_fixture_experiment()
    card = render_model_card(exp)
    assert "计划门槛" in card
    assert "experimental_unvalidated" in card
    assert "诚实边界" in card
    assert "missing_step_1" in card


def test_model_card_write(tmp_path: Path) -> None:
    exp = run_fixture_experiment()
    dest = write_model_card(exp, target=tmp_path / "MODEL_CARD.md")
    assert dest.exists()
    assert "模型卡" in dest.read_text(encoding="utf-8")
