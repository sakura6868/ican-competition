# 代码来源与复用边界（lineage）

本文件记录从 SafeHome（`safehome-guardian`）迁移到新项目 `gongxu-zhichuan` 的通用能力及其改写范围。迁移原则：**复制而不是移动**，删除所有 SafeHome 业务含义（老人、跌倒、床区、告警、UP-Fall/URFD、跌倒 TCN、模型指标、页面文案、参赛材料），仅保留通用工程能力。

SafeHome 来源仓库状态（记录于 2026-08-15，只读）：

- 分支：`codex/publish-runtime-tcn-weight`
- HEAD：`8c5a0c0 feat: include runtime TCN development weight`
- 受保护未跟踪文件：`docs/Industrial_SOP_AI_full_research_and_technical_plan_2026-08-15.md`、`docs/ui-concepts/industrial-sop-system-architecture-v1.png`、`frontend/public/brand/`

## 迁移清单

阶段 2–5 的全部模块（认证、加密、审计、任务队列、配置、SQLAlchemy/Alembic、
数据适配、Provider、SOP、RAG、资产上传、媒体处理、worker）均为**全新独立实现**，
参照交接文档 §6–§9 的契约编写，未复制 SafeHome 源码。以下为逐项说明。

| 新项目位置 | 实现方式 | 说明 |
|---|---|---|
| `gongxu/security/*` | 独立实现 | Argon2 + HttpOnly 会话 + AES-256-GCM（分块文件加密） |
| `gongxu/jobs/*` | 独立实现 | SQLite 持久化队列：幂等键/租约/心跳/检查点/重试 |
| `gongxu/db/*` + `migrations/` | 独立实现 | SQLAlchemy 2 + Alembic，3 个迁移 |
| `gongxu/data/*` | 独立实现 | 参考 IMPACT 官方 tools/download_impact.py 的 bundle 键约定 |
| `gongxu/providers/*` | 独立实现 | MultimodalProvider + Replay/Qwen，handoff §8 接口 |
| `gongxu/sop/*` | 独立实现 | Schema + 业务校验（DAG/权重/来源等级/证据） |
| `gongxu/rag/*` | 独立实现 | 零依赖 BM25 + 词袋余弦混合 |
| `gongxu/assets/*`、`gongxu/media/*` | 独立实现 | 资产加密存储、FFmpeg 转码/抽帧/切分 |
| `windows/*.ps1` | 独立实现 | Setup/Start/Stop/Test |

## 参考来源（只读引用，非迁移）

- `safehome-guardian/docs/Industrial_SOP_AI_full_research_and_technical_plan_2026-08-15.md`：本项目的立项与总体技术方案依据。
- `safehome-guardian/docs/ui-concepts/industrial-sop-system-architecture-v1.png`：系统总体框架示意图参考。

## 数据与模型边界

- IMPACT v1.1（CC BY-NC-SA 4.0）：只用于非商业研究与教育；数据与衍生权重默认不进入公开发布包。
- 原始数据 `data/raw/`、训练数据 `data/processed/`、权重 `models/` 均不提交 Git。

## 竞品与技术引用

详见立项技术方案（SafeHome 只读参考文件），本文件只追踪代码血缘。
