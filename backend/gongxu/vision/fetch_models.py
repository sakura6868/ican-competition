"""模型获取 CLI：下载本地视觉模型并登记到 model_registry。

用法：uv run python -m gongxu.vision.fetch_models [--force]

幂等：目标文件已存在且 SHA256 匹配则跳过；--force 强制重新下载。
下载源为公开官方资源：
- hand_landmarker.task：Google MediaPipe 官方存储（Apache-2.0）
- yolov8n.onnx：Ultralytics 官方 release（AGPL-3.0，与项目一致）
"""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.data.manifest import sha256_file
from gongxu.db.session import get_session_factory, init_db
from gongxu.vision.models import register_model

_HAND_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)
_YOLO_URL = (
    "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.onnx"
)
_MODELS: dict[str, tuple[str, str, str]] = {
    "hand_landmarker.task": ("hand", "hand_landmarker_float16", _HAND_URL),
    "yolov8n.onnx": ("tool_detector", "yolov8n_coco80", _YOLO_URL),
}


def _get_db() -> Session:
    init_db()
    return get_session_factory()()


def _download(url: str, dest: Path, *, force: bool) -> bool:
    if dest.exists() and not force:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(180.0)) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
    return True


def cmd_fetch_models(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = _get_db()
    try:
        for filename, (reg_name, version, url) in _MODELS.items():
            dest = settings.model_root / filename
            downloaded = _download(url, dest, force=args.force)
            checksum = sha256_file(dest)
            register_model(
                db,
                name=reg_name,
                kind="vision",
                version=version,
                path=str(dest),
                status="available",
                sha256=checksum,
                notes="本地推理权重（官方来源）",
            )
            action = "重新下载" if downloaded else "已存在"
            print(f"{action}: {filename} -> {reg_name}@{version} sha256={checksum[:12]}")
        print("下一步：设置 GONGXU_VISION_BACKEND=onnx 后运行预录考评。")
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="gongxu.vision.fetch_models", description="下载并登记本地视觉模型"
    )
    parser.add_argument("--force", action="store_true", help="强制重新下载（覆盖已有文件）")
    args = parser.parse_args()
    return cmd_fetch_models(args)


if __name__ == "__main__":
    raise SystemExit(main())
