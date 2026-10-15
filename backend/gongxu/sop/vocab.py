"""工序词表与 SOP 语义一致性门禁（整改 P0-2）。

问题：审核发现系统可发布与工序完全无关的 SOP（如角磨机工序出现
Video Signal Generator / Vectorscope）。现有 Schema 只查结构，不查语义。

整改：为每个工序定义受控词表（工具/对象/动作），SOP 编译和发布前
做一致性检查——步骤中的工具/对象/动作必须属于该工序词表，否则拒绝
（不兜底为可发布）。

词表来源：工序定义（首版由人工登记，后续可由手册/目录自动扩展）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from gongxu.db.models import ProcessVocabulary

# 首版工序：断电角磨机安全拆装（handoff §2 固定场景）
PROCESS_VOCABULARIES: dict[str, dict[str, set[str]]] = {
    "impact_grinder_safe_subassembly": {
        "tools": {
            "power_plug",
            "gloves",
            "goggles",
            "screwdriver",
            "wrench",
            "brush",
        },
        "objects": {
            "power_outlet",
            "grinding_wheel",
            "guard",
            "locking_nut",
            "workbench",
        },
        "actions": {"unplug", "wait", "wear", "remove", "loosen", "inspect", "clean"},
    },
    # 演示工序：IMPACT 齿轮箱拆装（数据/词表来自官方 TAS-B 标注，域匹配）
    "impact_gearbox_disassembly_demo": {
        "tools": {
            "combination_wrench",
            "flat_head_screwdriver",
            "phillips_screwdriver",
            "torx_screwdriver",
        },
        "objects": {
            "gearbox_housing",
            "gearbox_housing_drive_shaft",
            "drive_shaft",
            "bevel_gear",
            "adapter_plate",
            "bearing_plate",
            "anti_vibration_handle",
            "M6_nut",
            "M4_nut",
            "screw",
            "lever",
            "spring",
            "washer",
        },
        "actions": {
            "loosen",
            "tighten",
            "remove",
            "pick_up",
            "place",
            "mount",
            "insert",
            "attach",
            "detach",
            "extract",
            "hand_loosen",
            "hand_tighten",
            "hold",
            "store",
            "transfer",
            "adjust",
        },
    },
    # 扩展演示工序：Assembly101 履带机器人装配（数据来自官方 sample 视频，
    # 词表基于 qwen 实测识别能力，SOP 人工定义，来源标记 manual）
    "assembly101_robot_assemble_demo": {
        "tools": {
            "screwdriver",
            "wrench",
        },
        "objects": {
            "robot",
            "tracked_robot",
            "chassis",
            "wheel",
            "bolt",
            "screw",
            "instruction_manual",
            "workbench",
        },
        "actions": {
            "pick_up",
            "screw",
            "tighten",
            "place",
            "assemble",
            "inspect",
            "adjust",
            "hold",
        },
    },
}


class SopSemanticError(Exception):
    """SOP 语义门禁失败（工具/对象/动作不属于当前工序词表）。"""


@dataclass(frozen=True)
class ControlledVocabulary:
    process_key: str
    tools: frozenset[str]
    objects: frozenset[str]
    actions: frozenset[str]
    approved: bool
    source: str

    def as_prompt(self) -> dict[str, list[str]]:
        return {
            "tools": sorted(self.tools),
            "objects": sorted(self.objects),
            "actions": sorted(self.actions),
        }


def _normalise_terms(values: list[str] | set[str] | frozenset[str]) -> frozenset[str]:
    terms: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise SopSemanticError("受控词表只允许字符串词项")
        term = raw.strip().lower()
        if not term:
            continue
        if len(term) > 64 or any(ord(char) < 32 for char in term):
            raise SopSemanticError(f"非法受控词表词项: {raw!r}")
        terms.add(term)
    if len(terms) > 128:
        raise SopSemanticError("单类受控词表最多 128 个词项")
    return frozenset(terms)


def get_process_vocabulary(
    process_key: str,
    *,
    db: Session | None = None,
    include_proposed: bool = True,
) -> ControlledVocabulary | None:
    """读取持久化词表；数据库登记优先于内置比赛演示词表。"""
    if db is not None:
        row = db.get(ProcessVocabulary, process_key)
        if row is not None and (row.approved or include_proposed):
            return ControlledVocabulary(
                process_key=process_key,
                tools=_normalise_terms(list(row.tools or [])),
                objects=_normalise_terms(list(row.objects or [])),
                actions=_normalise_terms(list(row.actions or [])),
                approved=bool(row.approved),
                source=row.source,
            )
    builtin = PROCESS_VOCABULARIES.get(process_key)
    if builtin is None:
        return None
    return ControlledVocabulary(
        process_key=process_key,
        tools=_normalise_terms(builtin["tools"]),
        objects=_normalise_terms(builtin["objects"]),
        actions=_normalise_terms(builtin["actions"]),
        approved=True,
        source="builtin",
    )


def propose_process_vocabulary(
    db: Session,
    process_key: str,
    steps: list[dict[str, Any]],
    *,
    created_by: str | None = None,
) -> ControlledVocabulary:
    """从首份编译草稿收集候选词；只保存提案，不自动批准。"""
    existing = get_process_vocabulary(process_key, db=db, include_proposed=True)
    if existing is not None:
        return existing
    row = ProcessVocabulary(
        process_key=process_key,
        tools=sorted(
            {
                str(v).strip().lower()
                for step in steps
                for v in step.get("tools", [])
                if str(v).strip()
            }
        ),
        objects=sorted(
            {
                str(v).strip().lower()
                for step in steps
                for v in step.get("objects", [])
                if str(v).strip()
            }
        ),
        actions=sorted(
            {
                str(v).strip().lower()
                for step in steps
                for v in step.get("actions", [])
                if str(v).strip()
            }
        ),
        approved=False,
        source="compiled_proposal",
        created_by=created_by,
        updated_by=created_by,
    )
    db.add(row)
    db.flush()
    proposed = get_process_vocabulary(process_key, db=db, include_proposed=True)
    if proposed is None:  # pragma: no cover - flush 后不应发生
        raise SopSemanticError(f"无法创建工序 {process_key} 的词表提案")
    return proposed


def save_process_vocabulary(
    db: Session,
    process_key: str,
    *,
    tools: list[str],
    objects: list[str],
    actions: list[str],
    approved: bool,
    updated_by: str | None = None,
) -> ControlledVocabulary:
    """保存教师校对结果；批准时至少需要动作和一个工具/对象词。"""
    normalised = {
        "tools": _normalise_terms(tools),
        "objects": _normalise_terms(objects),
        "actions": _normalise_terms(actions),
    }
    if approved and not normalised["actions"]:
        raise SopSemanticError("批准词表前至少登记一个动作词")
    if approved and not (normalised["tools"] or normalised["objects"]):
        raise SopSemanticError("批准词表前至少登记一个工具或对象词")
    row = db.get(ProcessVocabulary, process_key)
    if row is None:
        row = ProcessVocabulary(process_key=process_key, created_by=updated_by)
        db.add(row)
    row.tools = sorted(normalised["tools"])
    row.objects = sorted(normalised["objects"])
    row.actions = sorted(normalised["actions"])
    row.approved = approved
    row.source = "teacher_confirmed" if approved else "teacher_draft"
    row.updated_by = updated_by
    db.commit()
    saved = get_process_vocabulary(process_key, db=db, include_proposed=True)
    if saved is None:  # pragma: no cover - commit 后不应发生
        raise SopSemanticError(f"无法保存工序 {process_key} 的词表")
    return saved


def _check_step_with_vocab(
    process_key: str,
    step: dict[str, Any],
    vocab: ControlledVocabulary,
) -> None:
    allowed = {
        "tools": vocab.tools,
        "objects": vocab.objects,
        "actions": vocab.actions,
    }
    labels = {"tools": "工具", "objects": "对象", "actions": "动作"}
    for field, values in allowed.items():
        for raw in step.get(field, []):
            term = str(raw).strip().lower()
            if term and term not in values:
                raise SopSemanticError(
                    f"步骤 {step.get('index')} {labels[field]}「{raw}」不属于工序 "
                    f"{process_key} 的受控{labels[field]}词表"
                )


def check_step_semantics(
    process_key: str,
    step: dict[str, Any],
    *,
    db: Session | None = None,
    require_approved: bool = True,
) -> None:
    """校验单步语义；未登记或未批准词表一律显式阻断。"""
    vocab = get_process_vocabulary(process_key, db=db, include_proposed=True)
    if vocab is None:
        raise SopSemanticError(f"工序 {process_key} 尚未登记受控词表")
    if require_approved and not vocab.approved:
        raise SopSemanticError(f"工序 {process_key} 的受控词表尚未由教师确认")
    _check_step_with_vocab(process_key, step, vocab)


def check_sop_semantics(
    process_key: str,
    steps: list[dict[str, Any]],
    *,
    db: Session | None = None,
    require_approved: bool = True,
) -> None:
    """校验整个 SOP 的语义一致性；任何步骤失败即整体拒绝。"""
    vocab = get_process_vocabulary(process_key, db=db, include_proposed=True)
    if vocab is None:
        raise SopSemanticError(f"工序 {process_key} 尚未登记受控词表")
    if require_approved and not vocab.approved:
        raise SopSemanticError(f"工序 {process_key} 的受控词表尚未由教师确认")
    for step in steps:
        _check_step_with_vocab(process_key, step, vocab)
