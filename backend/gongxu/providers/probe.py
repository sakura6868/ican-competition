"""Provider 能力探测 CLI。

用法：
  uv run python -m gongxu.providers.probe --mode live    # 真实凭据（从 .env）
  uv run python -m gongxu.providers.probe --mode replay  # 离线 fixture

真实凭据验收结果写入 reports/generated/provider-contract.json。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from gongxu.providers.factory import get_provider


def _main() -> int:
    parser = argparse.ArgumentParser(description="Provider 能力探测")
    parser.add_argument("--mode", default="auto", help="auto|replay|live")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    from gongxu.config import get_settings

    settings = get_settings()
    if args.mode != "auto":
        # 覆盖模式（用于显式探测）
        import os

        os.environ["GONGXU_LLM_MODE"] = args.mode
        get_settings.cache_clear()
        settings = get_settings()

    try:
        provider = get_provider(allow_replay=settings.llm_mode == "replay")
    except Exception as exc:  # 构造失败（如缺凭据）
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "mode": args.mode}
        _dump(result, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    try:
        result = provider.probe()
        result["ok"] = bool(result.get("ok"))
    except Exception as exc:
        result = {
            "ok": False,
            "provider": provider.name,
            "mode": provider.mode,
            "error": f"{type(exc).__name__}: {exc}",
        }
    result["configured_model"] = settings.llm_model
    result["base_url"] = settings.llm_base_url
    _dump(result, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def _dump(result: dict[str, Any], out: Path | None) -> None:
    root = Path(__file__).resolve().parents[3]
    dest = out or (root / "reports" / "generated" / "provider-contract.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(_main())
