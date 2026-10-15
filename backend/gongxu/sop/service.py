"""SOP 编译与发布服务。

流程：Provider 输出 SOP JSON → Pydantic Schema 校验 → 业务校验（DAG/权重/证据）
→ 存 provider_receipt + sop_versions（draft）→ publish 置为 published。
非法输出不入库，保留诊断。
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from gongxu.assets.service import AssetError
from gongxu.config import get_settings
from gongxu.db.models import (
    DocumentChunk,
    ProviderReceipt,
    SopPublishStatus,
    SopVersion,
    SourceAsset,
)
from gongxu.db.models import (
    SourceGrade as DbSourceGrade,
)
from gongxu.media.video import MediaError
from gongxu.providers.base import (
    MultimodalProvider,
    ProviderError,
    ReferenceSegment,
)
from gongxu.providers.base import (
    ProviderReceipt as ProviderCallRecord,
)
from gongxu.sop.schema import SopVersion as SopSchema
from gongxu.sop.validation import SopValidationError, validate_sop


class SopCompileError(Exception):
    pass


class SopNotFoundError(Exception):
    pass


class SopPublishError(Exception):
    pass


ProgressCallback = Callable[[str, int, str, dict[str, Any] | None], None]


def _emit_progress(
    callback: ProgressCallback | None,
    stage: str,
    progress_percent: int,
    status_text: str,
    details: dict[str, Any] | None = None,
) -> None:
    if callback is not None:
        callback(stage, progress_percent, status_text, details)


def _now() -> datetime:
    return datetime.now(UTC)


def _persist_receipt(db: Session, receipt: ProviderCallRecord) -> str:
    """持久化 ProviderReceipt 并返回 id。"""
    row = ProviderReceipt(
        provider=receipt.provider,
        mode=receipt.mode,
        model_id=receipt.model_id,
        prompt_version=receipt.prompt_version,
        input_digest=receipt.input_digest,
        response_digest=receipt.response_digest,
        input_tokens=receipt.input_tokens,
        output_tokens=receipt.output_tokens,
        latency_ms=receipt.latency_ms,
        cost_usd=receipt.cost_usd,
        error=receipt.error,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def compile_sop(
    db: Session,
    *,
    provider: MultimodalProvider,
    process_key: str,
    analysis: dict[str, Any],
    rag_chunks: list[dict[str, Any]],
    compiled_by: str | None = None,
    prompt_version: str = "sop-compile-v1",
) -> SopVersion:
    """调用 Provider 编译 SOP 并落库。非法输出抛 SopCompileError，不落库。"""
    try:
        raw_sop, receipt = provider.compile_sop(analysis, rag_chunks)
    except ProviderError as exc:
        raise SopCompileError(f"Provider 编译失败: {exc}") from exc

    receipt_id = _persist_receipt(db, receipt)

    try:
        sop = SopSchema.model_validate(raw_sop)
        validate_sop(sop)
    except (ValidationError, SopValidationError) as exc:
        # 非法输出：保留诊断，不创建伪 SOP
        raise SopCompileError(f"SOP 校验失败，未入库: {exc}") from exc

    # 以 process_key + 已有最高版本确定新版本号
    latest = (
        db.query(SopVersion)
        .filter(SopVersion.process_key == process_key)
        .order_by(SopVersion.version.desc())
        .first()
    )
    version = (latest.version + 1) if latest else 1

    row = SopVersion(
        process_key=process_key,
        version=version,
        status=SopPublishStatus.draft,
        source_grade=sop.source_grade.value,
        steps=[s.model_dump(mode="json") for s in sop.steps],
        scoring_profile=sop.scoring_profile.model_dump(),
        prompt_version=prompt_version,
        provider_receipt_id=receipt_id,
        compiled_by=compiled_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def compile_sop_from_media(
    db: Session,
    *,
    provider: MultimodalProvider,
    process_key: str,
    reference_video_asset_id: str,
    manual_asset_ids: list[str],
    compiled_by: str | None = None,
    progress_callback: ProgressCallback | None = None,
    teaching_mode: bool = False,
) -> SopVersion:
    """从授权标准视频 + 手册证据编译 SOP 草稿（整改 P1-A）。

    流程：校验资产 → 解密视频抽关键帧 → 手册切片 RAG 检索 →
    Provider 分析参考片段 → 编译 SOP → Schema/业务校验 → 落库。
    非法输出或证据不足抛 SopCompileError，不落库。
    """
    settings = get_settings()
    _emit_progress(progress_callback, "probing", 5, "正在检查视频与参考资料")
    video_asset = db.get(SourceAsset, reference_video_asset_id)
    if video_asset is None or video_asset.kind != "video":
        raise SopCompileError("标准视频资产不存在或类型不是视频")
    # 出站隐私门禁（整改 P1-1）：标准视频关键帧要发往外部 Provider，
    # 必须逐资产显式授权 allow_provider_export；未授权即拒绝编译。
    if not video_asset.allow_provider_export:
        raise SopCompileError(
            "标准视频未授权发送给外部 Provider；请在上传时勾选「允许外部 AI 分析」"
        )
    manual_assets = []
    for mid in manual_asset_ids:
        m = db.get(SourceAsset, mid)
        if m is None or m.kind not in ("manual", "image"):
            raise SopCompileError(f"手册资产不存在或类型不支持: {mid}")
        if not m.allow_provider_export:
            raise SopCompileError(f"手册资产 {m.filename} 未授权发送给外部 Provider")
        indexed_chunks = db.query(DocumentChunk).filter(DocumentChunk.asset_id == m.id).count()
        if indexed_chunks == 0:
            raise SopCompileError(f"手册资产 {m.filename} 尚未处理并建立 RAG 索引")
        manual_assets.append(m)

    # RAG 检索：仅在该次编译所选的手册资产白名单内检索（整改 P0-3），
    # 不允许把未选择资产的文档片段外发给 Provider。
    from gongxu.assets.indexing import retrieve_chunks

    rag_chunks: list[dict[str, Any]] = []
    if manual_assets:
        rag_chunks = retrieve_chunks(
            db,
            f"{process_key} 安全操作 步骤 工具",
            asset_ids=[m.id for m in manual_assets],
            top_k=6,
        )
        if not rag_chunks:
            raise SopCompileError("所选手册没有检索到可引用片段，不能声明手册来源")
    _emit_progress(
        progress_callback,
        "probing",
        15,
        "参考资料检查完成",
        {"manual_count": len(manual_assets), "rag_chunk_count": len(rag_chunks)},
    )

    from gongxu.assets import service as asset_service

    work_root = settings.data_root / "runtime" / "sop-compile"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{process_key}_", dir=work_root) as tmpdir:
        tmp = Path(tmpdir)
        video_path = asset_service.decrypt_to_temp(db, video_asset.id, temp_dir=tmp / "plain")
        # 全片均匀抽帧，保留源视频全局时间（出站仍由 Provider 压到 ≤6 帧）。
        from gongxu.media.video import extract_keyframes_uniform

        _emit_progress(progress_callback, "sampling", 25, "正在从标准视频提取关键帧")
        windows: list[tuple[int, int]] = []
        duration_ms = 0
        if teaching_mode:
            from gongxu.sop.teaching import sample_teaching_video

            try:
                sampled_frames, windows, duration_ms = sample_teaching_video(
                    video_path, tmp / "frames"
                )
            except ValueError as exc:
                raise SopCompileError(str(exc)) from exc
        else:
            sampled_frames = extract_keyframes_uniform(video_path, tmp / "frames", count=24)
        if not sampled_frames:
            raise SopCompileError("视频未抽出关键帧，无法分析")
        keyframes = [frame.path for frame in sampled_frames]
        keyframe_timestamps_ms = [frame.timestamp_ms for frame in sampled_frames]
        _emit_progress(
            progress_callback,
            "sampling",
            38,
            f"已提取 {len(keyframes)} 帧",
            {
                "sampled_frames": len(keyframes),
                "timeline_start_ms": keyframe_timestamps_ms[0],
                "timeline_end_ms": keyframe_timestamps_ms[-1],
            },
        )

        # 分析参考片段（多模态：关键帧 + 手册证据）
        segment = ReferenceSegment(
            segment_id=f"{video_asset.id}:v1",
            keyframes=[str(k) for k in keyframes],
            keyframe_timestamps_ms=keyframe_timestamps_ms,
            asr_text="",
            ocr_text="",
            detection_summary={},
            rag_chunks=rag_chunks,
            start_ms=keyframe_timestamps_ms[0],
            end_ms=keyframe_timestamps_ms[-1],
        )
        try:
            _emit_progress(
                progress_callback,
                "vision_analyzing",
                45,
                "正在识别工具、对象与操作阶段",
                {"sampled_frames": len(keyframes)},
            )
            configure_reference_progress = getattr(
                provider,
                "set_reference_progress_callback",
                None,
            )
            # Teaching progress covers all windows. A provider's per-window batch
            # counter would otherwise jump to 67% and then back to the global total.
            if callable(configure_reference_progress) and not teaching_mode:
                configure_reference_progress(
                    lambda completed, total: _emit_progress(
                        progress_callback,
                        "vision_analyzing",
                        45 + round(completed / max(total, 1) * 22),
                        f"正在分析标准视频（{completed}/{total} 批）",
                        {
                            "analyzed_batches": completed,
                            "total_batches": total,
                            "sampled_frames": len(keyframes),
                        },
                    )
                )
            try:
                if teaching_mode:
                    from gongxu.sop.teaching import analyze_windows

                    analysis, analyze_receipt = analyze_windows(
                        provider,
                        video_path,
                        video_asset.id,
                        sampled_frames,
                        windows,
                        tmp,
                        rag_chunks,
                        progress_callback,
                    )
                else:
                    analysis, analyze_receipt = provider.analyze_reference_segment(segment)
            finally:
                if callable(configure_reference_progress):
                    configure_reference_progress(None)
            # 服务端用抽帧器的真实时间坐标覆盖任何模型自报值，使后续
            # compile_sop 能把 evidence_refs 绑定到可追溯的源视频时间点。
            analysis["reference_timeline"] = [
                {
                    "frame_index": frame.frame_index,
                    "timestamp_ms": frame.timestamp_ms,
                }
                for frame in sampled_frames
            ]
            # 受控词表约束：编译输出必须可发布（整改 P0-2）。
            # Qwen 编译 prompt 读取 analysis.controlled_vocab 强制 tools/objects/actions
            # 从工序词表取值，避免自由中文导致语义门禁拒绝。
            from gongxu.sop.vocab import get_process_vocabulary

            vocab = get_process_vocabulary(process_key, db=db, include_proposed=True)
            if vocab:
                analysis["controlled_vocab"] = vocab.as_prompt()
                analysis["controlled_vocab_status"] = "approved" if vocab.approved else "proposed"
            _emit_progress(
                progress_callback,
                "sop_compiling",
                72,
                "正在把识别结果编译为结构化 SOP",
            )
            raw_sop, compile_receipt = provider.compile_sop(analysis, rag_chunks)
            if teaching_mode:
                from gongxu.sop.teaching import enrich_steps

                enrich_steps(raw_sop, video_asset.id, duration_ms)
        except ProviderError as exc:
            raise SopCompileError(f"Provider 分析失败: {exc}") from exc

        # 完整审计：analyze + compile 两次调用都落收据（整改 P1-1）
        _analyze_id = _persist_receipt(db, analyze_receipt)
        compile_receipt_id = _persist_receipt(db, compile_receipt)
        row = _validate_and_store(
            db,
            raw_sop=_analysis_to_sop(
                raw_sop,
                analysis,
                process_key,
                keyframes,
                reference_video_asset_id=video_asset.id,
                rag_chunks=rag_chunks,
            ),
            receipt_id=compile_receipt_id,
            analyze_receipt_id=_analyze_id,
            process_key=process_key,
            compiled_by=compiled_by,
            reference_video_asset_id=video_asset.id,
            source_asset_ids=[video_asset.id] + [m.id for m in manual_assets],
        )
        _emit_progress(
            progress_callback,
            "draft_ready",
            100,
            "SOP 草稿已生成",
            {"sop_id": row.id, "sop_version": row.version},
        )
        return row


def _analysis_to_sop(
    raw_sop: dict[str, Any],
    analysis: dict[str, Any],
    process_key: str,
    keyframes: list[Path],
    *,
    reference_video_asset_id: str,
    rag_chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """服务端确定性适配：Provider 语义输出 → 结构化 SOP 草稿。

    - steps：优先用 Provider 的 steps（若已是对象数组），否则把 analysis 的
      步骤文本列表转为 SopStep（name=文本、index 顺序、weight 均分）。
    - evidence_refs：绑定视频关键帧时间戳（来源证据）。
    - 系统字段（id/process_key/version/status/source_grade）在 _validate_and_store 补全。
    """
    steps: list[dict[str, Any]] = []
    raw_steps = raw_sop.get("steps") or []
    if isinstance(raw_steps, list) and all(isinstance(s, dict) for s in raw_steps):
        for i, s in enumerate(raw_steps):
            step = dict(s)
            # 服务端权威补充 id（P1-A.9）；其余字段禁止兜底占位（整改 P0-2）：
            # 缺 name / 缺证据 / 缺动作工具对象的步骤不得被修成"合法 JSON"。
            step.setdefault("id", f"step_{int(step.get('index', i + 1)):02d}")
            if step.get("index") is None:
                step["index"] = i + 1
            if not step.get("name"):
                raise SopCompileError(
                    f"Provider 输出步骤 {step.get('index', i + 1)} 缺少名称，拒绝编译（不兜底占位）"
                )
            # 证据规范化：Provider 只能引用服务端实际抽出的时间点或 RAG 块。
            refs = step.get("evidence_refs") or []
            if not isinstance(refs, list) or not refs:
                raise SopCompileError(
                    f"步骤 {step.get('index')} 缺少来源证据，拒绝编译（不允许假证据 keyframe-i）"
                )
            step["evidence_refs"] = [
                _normalise_evidence_ref(
                    ref,
                    reference_video_asset_id=reference_video_asset_id,
                    timeline=analysis.get("reference_timeline") or [],
                    rag_chunks=rag_chunks,
                    step_index=int(step.get("index", i + 1)),
                )
                for ref in refs
            ]
            steps.append(step)

        # Normalize relative model weights, including 12+ equally weighted steps.
        total = sum(float(s.get("weight", 0)) for s in steps)
        if total > 0:
            scale = 100.0 / total
            for s in steps:
                s["weight"] = round(float(s["weight"]) * scale, 2)
        # 修正浮点误差：最后一步补齐到 100
        if steps:
            diff = 100.0 - sum(float(s["weight"]) for s in steps)
            steps[-1]["weight"] = round(float(steps[-1]["weight"]) + diff, 2)
    else:
        # 文本步骤列表不构成可发布 SOP：缺少工具/对象/动作/真实证据（整改 P0-2），
        # 拒绝编译，不自动构造假证据。
        raise SopCompileError("Provider 未返回结构化 steps（缺工具/对象/动作/证据），拒绝编译")
    evidence_kinds = {ref["kind"] for step in steps for ref in step.get("evidence_refs", [])}
    if rag_chunks and "manual" not in evidence_kinds:
        raise SopCompileError("选择了手册但 Provider 未引用任何真实 RAG 片段，拒绝伪装为纯视频来源")
    source_grade = (
        "sensor_verified"
        if "sensor" in evidence_kinds
        else "manual_backed"
        if "manual" in evidence_kinds
        else "video_reference"
    )
    return {**raw_sop, "steps": steps, "source_grade": source_grade}


_VIDEO_TIMESTAMP_RE = re.compile(r"^video@(\d+)ms$")
_VIDEO_RANGE_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?-\d{1,2}:\d{2}(?::\d{2})?$")
_MANUAL_CHUNK_RE = re.compile(r"(?:chunk[=:]|rag@)(\d+)")


def _normalise_evidence_ref(
    raw: object,
    *,
    reference_video_asset_id: str,
    timeline: list[dict[str, Any]],
    rag_chunks: list[dict[str, Any]],
    step_index: int,
) -> dict[str, Any]:
    item = (
        {"kind": "video", "ref": raw}
        if isinstance(raw, str)
        else dict(raw)
        if isinstance(raw, dict)
        else None
    )
    if item is None:
        raise SopCompileError(f"步骤 {step_index} 包含无法解析的来源证据")
    kind = str(item.get("kind") or "video").strip().lower()
    ref = str(item.get("ref") or "").strip()
    description = str(item.get("description") or "").strip()

    if kind == "video":
        raw_timestamp = item.get("timestamp_ms")
        timestamp_ms = int(raw_timestamp) if isinstance(raw_timestamp, (int, float)) else None
        match = _VIDEO_TIMESTAMP_RE.fullmatch(ref)
        if timestamp_ms is None and match:
            timestamp_ms = int(match.group(1))
        allowed_timestamps = {
            int(frame["timestamp_ms"])
            for frame in timeline
            if isinstance(frame, dict) and isinstance(frame.get("timestamp_ms"), (int, float))
        }
        if timestamp_ms is not None:
            if timestamp_ms not in allowed_timestamps:
                raise SopCompileError(
                    f"步骤 {step_index} 引用了不存在的标准视频时间点 {timestamp_ms}ms"
                )
            ref = f"video@{timestamp_ms}ms"
        elif not _VIDEO_RANGE_RE.fullmatch(ref):
            raise SopCompileError(f"步骤 {step_index} 的 video 证据不是可追溯时间点/时间段")
        return {
            "kind": "video",
            "ref": ref,
            "description": description or "标准视频证据",
            "asset_id": reference_video_asset_id,
            "timestamp_ms": timestamp_ms,
        }

    if kind == "manual":
        raw_chunk_id = item.get("chunk_id")
        chunk_id = int(raw_chunk_id) if isinstance(raw_chunk_id, (int, float)) else None
        match = _MANUAL_CHUNK_RE.search(ref)
        if chunk_id is None and match:
            chunk_id = int(match.group(1))
        allowed = {
            int(chunk["chunk_id"]): chunk
            for chunk in rag_chunks
            if isinstance(chunk.get("chunk_id"), int)
        }
        chunk = allowed.get(chunk_id or -1)
        if chunk is None:
            raise SopCompileError(
                f"步骤 {step_index} 的 manual 证据未引用本次检索返回的真实 chunk_id"
            )
        locator = [f"chunk={chunk['chunk_id']}"]
        if chunk.get("page") is not None:
            locator.append(f"page={chunk['page']}")
        if chunk.get("section"):
            locator.append(f"section={chunk['section']}")
        return {
            "kind": "manual",
            "ref": f"manual@{chunk['asset_id']}#{';'.join(locator)}",
            "description": description or "手册 RAG 来源片段",
            "asset_id": chunk["asset_id"],
            "page": chunk.get("page"),
            "section": chunk.get("section"),
            "chunk_id": chunk["chunk_id"],
            "chunk_index": chunk["chunk_index"],
            "content_hash": chunk["content_hash"],
        }

    if kind == "sensor" and ref.startswith("sensor#"):
        return {
            "kind": "sensor",
            "ref": ref,
            "description": description or "传感器校验记录",
            "asset_id": item.get("asset_id"),
        }
    raise SopCompileError(f"步骤 {step_index} 的来源证据类型或定位符无效: {kind}")


def _validate_and_store(
    db: Session,
    *,
    raw_sop: dict[str, Any],
    receipt_id: str,
    process_key: str,
    compiled_by: str | None,
    reference_video_asset_id: str | None = None,
    analyze_receipt_id: str | None = None,
    source_asset_ids: list[str] | None = None,
) -> SopVersion:
    try:
        # 系统字段由服务端权威生成（id/process_key/version/status），不信任 LLM；
        # steps 必须完整并经过严格业务校验（P1-A.8）。
        raw_sop.setdefault("id", f"sop_{process_key}_draft")
        raw_sop.setdefault("process_key", process_key)
        raw_sop.setdefault("version", 1)
        raw_sop.setdefault("status", "draft")
        raw_sop.setdefault("source_grade", "video_reference")
        raw_sop.setdefault("prompt_version", "sop-compile-v1")
        steps_raw = raw_sop.get("steps", [])

        def _coerce_index(step: dict[str, Any], fallback: int) -> int:
            raw = step.get("index")
            if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit()):
                return int(raw)
            return fallback

        # index 服务端权威规范化：Qwen 可能从 0 或 1 编号，统一为从 1 连续编号。
        # 检测最小 index：为 0 则整体 +1（0-based），否则保持；predecessors 同步偏移。
        raw_idx = [_coerce_index(s, i) for i, s in enumerate(steps_raw, start=1)]
        offset = 1 if raw_idx and min(raw_idx) == 0 else 0
        for i, step in enumerate(steps_raw):
            step["index"] = raw_idx[i] + offset
            step.setdefault("id", f"step_{int(step['index']):02d}")
            if step.get("predecessors"):
                step["predecessors"] = [int(p) + offset for p in step["predecessors"]]
        sop = SopSchema.model_validate(raw_sop)
        validate_sop(sop)
        _validate_source_asset_links(
            db,
            sop,
            reference_asset_id=reference_video_asset_id,
            source_asset_ids=source_asset_ids or [],
        )
    except (ValidationError, SopValidationError) as exc:
        raise SopCompileError(f"SOP 校验失败，未入库: {exc}") from exc

    # 工序词表语义一致性门禁（整改 P0-2）：无关工具/对象/动作直接拒绝编译。
    from gongxu.sop.vocab import (
        SopSemanticError,
        check_sop_semantics,
        get_process_vocabulary,
        propose_process_vocabulary,
    )

    try:
        serialized = [s.model_dump(mode="json") for s in sop.steps]
        if get_process_vocabulary(process_key, db=db, include_proposed=True) is None:
            propose_process_vocabulary(
                db,
                process_key,
                serialized,
                created_by=compiled_by,
            )
        check_sop_semantics(
            process_key,
            serialized,
            db=db,
            require_approved=False,
        )
    except SopSemanticError as exc:
        raise SopCompileError(f"SOP 语义门禁失败，未入库: {exc}") from exc

    latest = (
        db.query(SopVersion)
        .filter(SopVersion.process_key == process_key)
        .order_by(SopVersion.version.desc())
        .first()
    )
    version = (latest.version + 1) if latest else 1

    row = SopVersion(
        process_key=process_key,
        version=version,
        status=SopPublishStatus.draft,
        source_grade=sop.source_grade.value,
        steps=[s.model_dump(mode="json") for s in sop.steps],
        scoring_profile=sop.scoring_profile.model_dump(),
        prompt_version=sop.prompt_version,
        provider_receipt_id=receipt_id,
        reference_asset_id=reference_video_asset_id,
        source_asset_ids=source_asset_ids or [],
        compiled_by=compiled_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def publish_sop(db: Session, sop_id: str, *, published_by: str | None = None) -> SopVersion:
    """发布 SOP：必须已通过结构+语义校验。已发布/已归档不可修改。

    发布门禁（整改 P0-2）：发布前重新跑工序词表语义一致性检查，
    防止低质量/无关 SOP 直接进入正式考评。
    """
    row = db.get(SopVersion, sop_id)
    if row is None:
        raise SopNotFoundError(sop_id)
    if row.status == SopPublishStatus.published:
        raise SopPublishError("SOP 已发布，不可重复发布")
    if row.status == SopPublishStatus.archived:
        raise SopPublishError("SOP 已归档，不可发布")
    # 发布前从数据库快照重建完整 Schema，并重跑结构、来源和语义门禁，防止
    # 绕过编译或数据库快照损坏后直接发布。
    from gongxu.sop.vocab import SopSemanticError, check_sop_semantics

    try:
        schema = _schema_from_row(row)
        validate_sop(schema)
        if any(s.teaching or s.verification for s in schema.steps):
            from gongxu.practice.service import readiness

            tutor_issues = [issue for issue in readiness(row, db) if issue != "课程尚未发布"]
            if tutor_issues:
                raise SopValidationError("；".join(tutor_issues))
            from gongxu.assets.service import decrypt_to_temp
            from gongxu.media.video import probe

            root = get_settings().data_root / "runtime" / "sop-compile"
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=root) as temp:
                source = decrypt_to_temp(db, row.reference_asset_id or "", temp_dir=Path(temp))
                duration_ms = round(probe(source).duration_s * 1000)
                if any(s.teaching and s.teaching.end_ms > duration_ms for s in schema.steps):
                    raise SopValidationError("教学片段时间超出标准视频时长")
        _validate_source_asset_links(
            db,
            schema,
            reference_asset_id=row.reference_asset_id,
            source_asset_ids=list(row.source_asset_ids or []),
        )
        check_sop_semantics(
            row.process_key,
            [step.model_dump(mode="json") for step in schema.steps],
            db=db,
            require_approved=True,
        )
    except (
        ValidationError,
        SopValidationError,
        SopSemanticError,
        ValueError,
        AssetError,
        MediaError,
    ) as exc:
        raise SopPublishError(f"SOP 完整校验失败，拒绝发布: {exc}") from exc
    row.status = SopPublishStatus.published
    row.published_at = _now()
    db.commit()
    db.refresh(row)
    return row


def update_sop(
    db: Session,
    sop_id: str,
    *,
    steps: list[dict[str, Any]] | None = None,
    scoring_profile: dict[str, Any] | None = None,
    source_grade: str | None = None,
    course_meta: dict[str, Any] | None = None,
    edited_by: str | None = None,
) -> SopVersion:
    """Update a draft, or fork an immutable published version into a new draft."""
    row = db.get(SopVersion, sop_id)
    if row is None:
        raise SopNotFoundError(sop_id)
    if row.status == SopPublishStatus.archived:
        raise SopPublishError("SOP 已归档，不能编辑")
    if steps is None and scoring_profile is None and source_grade is None and course_meta is None:
        raise SopPublishError("没有可更新的字段")

    next_steps = steps if steps is not None else row.steps
    next_profile = scoring_profile if scoring_profile is not None else row.scoring_profile
    next_grade = source_grade if source_grade is not None else row.source_grade.value
    next_meta = course_meta if course_meta is not None else dict(row.course_meta or {})
    from gongxu.sop.vocab import SopSemanticError, check_sop_semantics

    try:
        schema = SopSchema.model_validate(
            {
                "id": row.id,
                "process_key": row.process_key,
                "version": row.version,
                "status": "draft",
                "source_grade": next_grade,
                "steps": next_steps,
                "course_meta": next_meta,
                "scoring_profile": next_profile,
                "prompt_version": row.prompt_version,
                "provider_receipt_id": row.provider_receipt_id or "",
            }
        )
        validate_sop(schema)
        _validate_source_asset_links(
            db,
            schema,
            reference_asset_id=row.reference_asset_id,
            source_asset_ids=list(row.source_asset_ids or []),
        )
        check_sop_semantics(
            row.process_key,
            [step.model_dump(mode="json") for step in schema.steps],
            db=db,
            require_approved=False,
        )
    except (ValidationError, SopValidationError, SopSemanticError, ValueError) as exc:
        raise SopPublishError(f"SOP 修改校验失败: {exc}") from exc

    serialized_steps = [step.model_dump(mode="json") for step in schema.steps]
    serialized_profile = schema.scoring_profile.model_dump()
    db_grade = DbSourceGrade(schema.source_grade.value)
    if row.status == SopPublishStatus.draft:
        row.steps = serialized_steps
        row.course_meta = schema.course_meta.model_dump(mode="json")
        row.scoring_profile = serialized_profile
        row.source_grade = db_grade
        row.compiled_by = edited_by or row.compiled_by
        db.commit()
        db.refresh(row)
        return row

    latest = (
        db.query(SopVersion)
        .filter(SopVersion.process_key == row.process_key)
        .order_by(SopVersion.version.desc())
        .first()
    )
    draft = SopVersion(
        process_key=row.process_key,
        version=(latest.version + 1) if latest else row.version + 1,
        status=SopPublishStatus.draft,
        source_grade=db_grade,
        steps=serialized_steps,
        course_meta=schema.course_meta.model_dump(mode="json"),
        scoring_profile=serialized_profile,
        prompt_version=row.prompt_version,
        provider_receipt_id=row.provider_receipt_id,
        reference_asset_id=row.reference_asset_id,
        source_asset_ids=list(row.source_asset_ids or []),
        compiled_by=edited_by or row.compiled_by,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


def _schema_from_row(row: SopVersion) -> SopSchema:
    """从持久化快照重建发布时使用的完整 SOP Schema。"""
    return SopSchema.model_validate(
        {
            "id": row.id,
            "process_key": row.process_key,
            "version": row.version,
            "status": row.status.value,
            "source_grade": row.source_grade.value,
            "steps": row.steps,
            "course_meta": dict(row.course_meta or {}),
            "scoring_profile": row.scoring_profile,
            "prompt_version": row.prompt_version,
            "provider_receipt_id": row.provider_receipt_id or "",
        }
    )


def _validate_source_asset_links(
    db: Session,
    sop: SopSchema,
    *,
    reference_asset_id: str | None,
    source_asset_ids: list[str],
) -> None:
    """校验 SOP 来源资产均真实存在，且等级声明有对应类型资产支撑。

    EvidenceRef 负责页码/时间点等片段定位；此处校验 SOP 级资产白名单，二者
    共同组成可追溯来源。不会自动生成或猜测任何资产引用。
    """
    unique_ids = list(dict.fromkeys(asset_id for asset_id in source_asset_ids if asset_id))
    if not unique_ids:
        raise SopValidationError("SOP 缺少真实来源资产，不能发布")

    assets = db.query(SourceAsset).filter(SourceAsset.id.in_(unique_ids)).all()
    by_id = {asset.id: asset for asset in assets}
    missing = [asset_id for asset_id in unique_ids if asset_id not in by_id]
    if missing:
        raise SopValidationError(f"SOP 来源资产不存在: {missing}")
    unavailable = [
        asset.id
        for asset in assets
        if not asset.encrypted_path or not Path(asset.encrypted_path).is_file()
    ]
    if unavailable:
        raise SopValidationError(f"SOP 来源资产密文不可用: {unavailable}")

    if reference_asset_id:
        reference = by_id.get(reference_asset_id)
        if reference is None:
            raise SopValidationError("标准视频引用不在 SOP 来源资产白名单中")
        if reference.kind != "video":
            raise SopValidationError("标准视频引用必须指向真实 video 资产")

    evidence_kinds = {ref.kind for step in sop.steps for ref in step.evidence_refs}
    linked_kinds = {asset.kind for asset in assets}
    if "video" in evidence_kinds and "video" not in linked_kinds:
        raise SopValidationError("SOP 包含 video 证据，但未关联真实 video 资产")
    if sop.source_grade.value == "video_reference":
        if reference_asset_id is None:
            raise SopValidationError("video_reference SOP 缺少标准视频资产引用")
    elif sop.source_grade.value == "manual_backed" and "manual" not in linked_kinds:
        raise SopValidationError("manual_backed SOP 未关联真实 manual 资产")

    from gongxu.assets.indexing import get_chunk_provenance

    for step in sop.steps:
        if step.teaching and step.teaching.source_asset_id != reference_asset_id:
            raise SopValidationError("教学片段必须引用当前 SOP 的标准视频资产")
        for ref in step.evidence_refs:
            if ref.kind == "video":
                bound_video_id = ref.asset_id or reference_asset_id
                asset = by_id.get(bound_video_id or "")
                if asset is None or asset.kind != "video":
                    raise SopValidationError(f"步骤 {step.index} 的 video 证据未绑定真实视频资产")
            elif ref.kind == "manual":
                if not ref.asset_id:
                    raise SopValidationError(f"步骤 {step.index} 的 manual 证据缺少 asset_id")
                asset = by_id.get(ref.asset_id)
                if asset is None or asset.kind != "manual":
                    raise SopValidationError(
                        f"步骤 {step.index} 的 manual 证据未绑定白名单内真实 manual 资产"
                    )
                if ref.chunk_id is None or not ref.content_hash:
                    raise SopValidationError(
                        f"步骤 {step.index} 的 manual 证据缺少 chunk_id/content_hash"
                    )
                provenance = get_chunk_provenance(db, ref.chunk_id)
                if provenance is None or provenance["asset_id"] != ref.asset_id:
                    raise SopValidationError(f"步骤 {step.index} 的 manual chunk 不属于所声明资产")
                if provenance["content_hash"] != ref.content_hash:
                    raise SopValidationError(f"步骤 {step.index} 的 manual chunk 内容哈希不匹配")
                if ref.chunk_index is not None and provenance["chunk_index"] != ref.chunk_index:
                    raise SopValidationError(f"步骤 {step.index} 的 manual chunk_index 不匹配")
                if ref.page is not None and provenance["page"] != ref.page:
                    raise SopValidationError(f"步骤 {step.index} 的 manual 页码与索引来源不匹配")
                if ref.section is not None and provenance["section"] != ref.section:
                    raise SopValidationError(f"步骤 {step.index} 的 manual 章节与索引来源不匹配")
            elif ref.kind == "sensor" and not ref.ref.startswith("sensor#"):
                raise SopValidationError(
                    f"步骤 {step.index} 的 sensor 证据必须使用 sensor# 校验记录"
                )


def get_sop(db: Session, sop_id: str) -> SopVersion:
    row = db.get(SopVersion, sop_id)
    if row is None:
        raise SopNotFoundError(sop_id)
    return row


def list_sops(
    db: Session, *, process_key: str | None = None, status: SopPublishStatus | None = None
) -> list[SopVersion]:
    stmt = db.query(SopVersion)
    if process_key:
        stmt = stmt.filter(SopVersion.process_key == process_key)
    if status:
        stmt = stmt.filter(SopVersion.status == status)
    return stmt.order_by(SopVersion.created_at.desc()).all()
