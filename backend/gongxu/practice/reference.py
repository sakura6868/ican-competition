import base64
import hashlib
import json
import math
import tempfile
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from gongxu.assets.service import decrypt_to_temp
from gongxu.config import get_settings
from gongxu.db.models import SourceAsset
from gongxu.media.video import extract_frame_at, probe
from gongxu.security.crypto import decrypt_text, encrypt_text
from gongxu.sop.schema import SopStep


def reference_images(db: Session, step: SopStep) -> list[str]:
    if step.teaching is None:
        raise ValueError("步骤没有标准片段")
    asset = db.get(SourceAsset, step.teaching.source_asset_id)
    if (
        asset is None
        or asset.kind != "video"
        or not asset.encrypted_path
        or not Path(asset.encrypted_path).is_file()
        or not asset.allow_provider_export
    ):
        raise ValueError("教学视频不存在或未授权视觉分析")
    root = get_settings().data_root / "runtime" / "practice-reference"
    root.mkdir(parents=True, exist_ok=True)
    timestamps = step.teaching.reference_timestamps_ms or [
        step.teaching.start_ms,
        max(step.teaching.start_ms, step.teaching.end_ms - 100),
    ]
    # Cache only encrypted standards. Repeated checks must not decrypt a whole video.
    cache_root = get_settings().data_root / "assets" / "practice-standards"
    cache_root.mkdir(parents=True, exist_ok=True)
    identity = json.dumps([asset.id, asset.sha256, timestamps, "jpeg640-v1"])
    cache = cache_root / (hashlib.sha256(identity.encode()).hexdigest() + ".enc")
    if cache.is_file():
        images = json.loads(decrypt_text(cache.read_text(encoding="utf-8")))
        if isinstance(images, list) and images and all(isinstance(i, str) for i in images):
            return images
        raise ValueError("标准参考图缓存无效")
    with tempfile.TemporaryDirectory(dir=root) as temp:
        directory = Path(temp)
        video = decrypt_to_temp(db, asset.id, temp_dir=directory)
        info = probe(video)
        last_frame_ms = max(
            0, round(info.duration_s * 1000) - math.ceil(1000 / (info.fps or 4)) - 50
        )
        images = []
        for index, stamp in enumerate(timestamps):
            path = extract_frame_at(
                video, directory / f"reference_{index}.jpg", timestamp_ms=min(stamp, last_frame_ms)
            )
            images.append("data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode())
        temporary_cache = cache.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            temporary_cache.write_text(encrypt_text(json.dumps(images)), encoding="utf-8")
            temporary_cache.replace(cache)
        finally:
            temporary_cache.unlink(missing_ok=True)
        return images
