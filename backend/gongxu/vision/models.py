"""模型登记：跟踪本地模型可用性，模型缺失时明确拒判（handoff §7 门禁）。

- hand / tool_detector / tcn / pose 统一登记
- status: available | experimental | missing
- 检测缺失时不伪造结果，抛出 ModelMissingError 供上层降级
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from gongxu.data.manifest import sha256_file
from gongxu.db.models import ModelRegistryEntry, ModelStatus


class ModelMissingError(Exception):
    pass


REQUIRED_MODELS = ("hand", "tool_detector", "tcn")


def register_model(
    db: Session,
    *,
    name: str,
    kind: str,
    version: str,
    path: str | None,
    status: str = "available",
    sha256: str | None = None,
    latency_ms: int = 0,
    notes: str = "",
) -> ModelRegistryEntry:
    """登记一个模型（按 name 幂等更新）。path 存在时自动计算 SHA256。"""
    checksum = sha256 or ""
    if path and Path(path).exists():
        checksum = sha256_file(Path(path))
    entry = get_model(db, name)
    if entry is None:
        entry = ModelRegistryEntry(name=name)
        db.add(entry)
    entry.kind = kind
    entry.status = ModelStatus(status)
    entry.version = version
    entry.sha256 = checksum
    entry.path = path
    entry.latency_ms = latency_ms
    entry.notes = notes
    db.commit()
    db.refresh(entry)
    return entry


def mark_missing(db: Session, name: str, *, reason: str) -> ModelRegistryEntry:
    entry = db.scalar(select(ModelRegistryEntry).where(ModelRegistryEntry.name == name))
    if entry is None:
        entry = ModelRegistryEntry(
            name=name, kind="unknown", status=ModelStatus.missing, notes=reason
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry
    entry.status = ModelStatus.missing
    entry.notes = reason
    db.commit()
    db.refresh(entry)
    return entry


def get_model(db: Session, name: str) -> ModelRegistryEntry | None:
    return db.scalar(select(ModelRegistryEntry).where(ModelRegistryEntry.name == name))


def require_model(db: Session, name: str) -> ModelRegistryEntry:
    """要求模型可用；否则抛 ModelMissingError（明确拒判，不猜测）。"""
    entry = get_model(db, name)
    if entry is None or entry.status != ModelStatus.available:
        note = entry.notes if entry else "未登记"
        raise ModelMissingError(f"模型 {name} 不可用（{note}）；已降级并拒判")
    return entry


def list_models(db: Session) -> list[ModelRegistryEntry]:
    return list(db.scalars(select(ModelRegistryEntry).order_by(ModelRegistryEntry.name)))


def ensure_required_registered(db: Session, *, reason: str = "未安装本地模型权重") -> None:
    """确保 hand/tool_detector/tcn 已登记；缺失时标记 missing。"""
    for name in REQUIRED_MODELS:
        if get_model(db, name) is None:
            mark_missing(db, name, reason=reason)
