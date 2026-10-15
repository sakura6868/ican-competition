"""能力清单 API（整改 P1-6）：文档、UI、报告由同一状态源生成。

状态语义：
- implemented：代码存在，单元测试通过
- integrated：与真实输入/真实 Provider 端到端跑通（如真实视频→SOP）
- validated：在独立测试集上有可复现指标（当前无真实视觉模型，视觉项非 validated）
- contest_ready：可进行正式自动考评（当前不满足）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from gongxu.config import get_settings
from gongxu.db.models import AuditAction, AuditLog, User, UserRole
from gongxu.db.session import get_db
from gongxu.security import auth
from gongxu.vision import models as model_service

router = APIRouter(prefix="/system", tags=["system"])


class CapabilityOut(BaseModel):
    key: str
    label: str
    status: str  # implemented | integrated | validated | contest_ready
    note: str
    runtime_backend: str | None = None
    provider_export_required: bool = False


class ProviderStatusOut(BaseModel):
    provider: str
    configured: bool
    runtime_backend: str
    mode: str
    model_id: str
    vision_ok: bool | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    note: str


def _provider_configuration() -> ProviderStatusOut:
    settings = get_settings()
    configured = bool(
        settings.llm_mode == "live"
        and settings.llm_base_url
        and settings.llm_api_key
        and settings.llm_model
    )
    active = configured and settings.vision_backend == "qwen"
    return ProviderStatusOut(
        provider="qwen",
        configured=configured,
        runtime_backend=settings.vision_backend,
        mode=settings.llm_mode,
        model_id=settings.llm_model,
        note=(
            "Qwen 已配置为主视觉模型；真人视频仅在逐资产授权后发送压缩关键帧"
            if active
            else "请配置 live Qwen 的 Base URL、API Key、模型 ID，并将视觉后端设为 qwen"
        ),
    )


@router.get("/provider-status", response_model=ProviderStatusOut)
def provider_status(
    _user: User = Depends(auth.get_current_user),
) -> ProviderStatusOut:
    """Return configuration state without making a paid or networked model call."""

    return _provider_configuration()


@router.post("/provider-probe", response_model=ProviderStatusOut)
def provider_probe(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(auth.require_roles(UserRole.admin, UserRole.instructor)),
) -> ProviderStatusOut:
    """Probe Qwen JSON and vision with a generated, non-personal image."""

    configured = _provider_configuration()
    if not configured.configured or configured.runtime_backend != "qwen":
        raise HTTPException(status_code=503, detail=configured.note)

    from gongxu.providers.base import ProviderError
    from gongxu.providers.factory import get_provider

    try:
        result = get_provider().probe()
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail=f"Qwen 能力探测失败: {exc}") from exc

    usage = result.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    status = ProviderStatusOut(
        provider="qwen",
        configured=True,
        runtime_backend="qwen",
        mode="live",
        model_id=str(result.get("model_id", configured.model_id)),
        vision_ok=bool(result.get("vision_ok")),
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        total_tokens=int(usage.get("total_tokens", 0) or 0),
        note=(
            "Qwen 文本、结构化 JSON 与图像输入能力探测通过"
            if result.get("ok")
            else "Qwen 返回了响应，但多模态能力探测未通过"
        ),
    )
    db.add(
        AuditLog(
            actor_id=current_user.id,
            action=AuditAction.provider_call,
            target_type="provider_probe",
            target_id=status.model_id,
            detail={
                "provider": status.provider,
                "vision_ok": status.vision_ok,
                "input_tokens": status.input_tokens,
                "output_tokens": status.output_tokens,
                "synthetic_image_only": True,
            },
            ip_address=request.client.host if request.client else "",
        )
    )
    db.commit()
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=status.note)
    return status


@router.get("/capabilities", response_model=list[CapabilityOut])
def capabilities(
    db: Session = Depends(get_db),
    _user: User = Depends(auth.get_current_user),
) -> list[CapabilityOut]:
    settings = get_settings()
    model_service.ensure_required_registered(db)
    local_models_ready = all(
        m.status.value == "available"
        for m in model_service.list_models(db)
        if m.name in ("hand", "tool_detector")
    )
    provider_ready = bool(
        settings.llm_mode == "live"
        and settings.llm_base_url
        and settings.llm_api_key
        and settings.llm_model
    )
    qwen_ready = settings.vision_backend == "qwen" and provider_ready
    onnx_runtime_ready = settings.vision_backend == "onnx" and local_models_ready
    # Bundled ONNX weights currently expose hand landmarks and generic COCO
    # objects only.  Loading those weights is not equivalent to recognizing an
    # industrial SOP's tools/actions, so it must not promote formal assessment
    # to "integrated".
    industrial_assessment_ready = qwen_ready
    active_mode = (
        f"外部多模态模型 {settings.llm_model}"
        if qwen_ready
        else "本地通用 ONNX（仅手部/通用对象事实，不具备工序动作语义闭环）"
        if onnx_runtime_ready
        else "可信拒判"
    )

    return [
        CapabilityOut(
            key="auth",
            label="账号与权限",
            status="integrated",
            note="Argon2 会话 + 角色 RBAC + CSRF 防护，端到端可用",
        ),
        CapabilityOut(
            key="privacy",
            label="加密与隐私",
            status="integrated",
            note="资产/衍生物 AES-256-GCM 加密存储，无长期明文",
        ),
        CapabilityOut(
            key="video_to_sop",
            label="视频→SOP 编译",
            status="integrated" if provider_ready else "implemented",
            note=(
                "标准视频分批分析、受控词表编译、人工编辑与版本发布已接通；"
                f"当前 Provider 为 {settings.llm_model}"
                if provider_ready
                else "后台编译、编辑与发布链路已实现；当前未配置真实外部多模态 Provider"
            ),
        ),
        CapabilityOut(
            key="rag",
            label="手册 RAG",
            status="integrated",
            note="按资产白名单隔离检索，避免未授权文档外发",
        ),
        CapabilityOut(
            key="scoring",
            label="确定性评分引擎",
            status="integrated",
            note="视觉事件只作证据输入，固定规则负责数值；证据不足返回无法判断，同输入同分数",
        ),
        CapabilityOut(
            key="recorded_assessment",
            label="预录自动考评",
            status="integrated" if industrial_assessment_ready else "implemented",
            note=f"当前模式：{active_mode}；上传、任务恢复、步骤证据和报告链路已实现，识别准确率尚未独立验证",
            runtime_backend=settings.vision_backend,
            provider_export_required=settings.vision_backend == "qwen",
        ),
        CapabilityOut(
            key="vision",
            label="视觉事实层",
            status="integrated" if qwen_ready else "implemented",
            note=(
                f"当前模式：{active_mode}；支持 Qwen 与本地 ONNX 适配层，"
                "只有 Qwen 路径具备当前工序语义，识别准确率仍未 validated"
            ),
            runtime_backend=settings.vision_backend,
            provider_export_required=settings.vision_backend == "qwen",
        ),
        CapabilityOut(
            key="training",
            label="Qwen 效果验证",
            status="implemented",
            note=(
                "默认外接 Qwen，不在本项目训练本地动作模型；接口与视觉能力已探测，"
                "目标工序授权视频上的冻结准确率评测尚未完成"
            ),
            runtime_backend="qwen" if qwen_ready else settings.vision_backend,
            provider_export_required=True,
        ),
    ]
