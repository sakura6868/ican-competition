"""数据卡生成测试。"""

from __future__ import annotations

from gongxu.data.card import render_data_card, write_data_card
from gongxu.data.manifest import ImpactManifest, ManifestEntry


def _manifest() -> ImpactManifest:
    return ImpactManifest(
        entries=[
            ManifestEntry(identifier="AL07EJ17_Disassembly_A_001_front", view="front", split="S1")
        ]
    )


def test_render_data_card_contains_key_fields() -> None:
    card = render_data_card(_manifest())
    assert "IMPACT v1.1" in card
    assert "CC BY-NC-SA 4.0" in card
    assert "AL07EJ17_Disassembly_A_001_front" in card
    assert "再分发" in card
    assert "trial-level" in card


def test_write_data_card(tmp_path) -> None:
    target = tmp_path / "docs" / "DATA_CARD.md"
    write_data_card(_manifest(), target=target)
    assert target.exists()
    assert "数据卡" in target.read_text(encoding="utf-8")
