# 工序智传 Claude 完成版审核与下一步整改计划

> 审核日期：2026-08-16  
> 审核分支：`claude/remediation-stages-7-11`  
> 审核提交：`3390c6c`  
> 基线分支：`main`（`44bde7e`）  
> 审核原则：不把 Fixture、数据导入、接口跑通或 fail-closed 拒判描述为“真实模型已训练可用”。

## 1. 审核结论

Claude 本轮完成了大量真实工程整改，不是空壳：认证授权、CSRF 基础设施、资产和衍生物加密、视频抽帧、外部多模态 Provider、IMPACT 数据导入、实时摄像头传输、拒判机制、离线人工复核、前后端测试均有实质实现。

但项目目前**不能认定为“完整完成”或“可进行正式自动考评”**。核心原因如下：

1. 没有真实视觉推理适配器、模型权重、训练产物或 ONNX 文件；真实预录和实时视频只能拒判。
2. `make train` 运行的是三种 Fixture 序列的确定性对齐实验，不是模型训练。
3. 标准视频到 SOP 虽然能调用真实多模态模型，但实际数据库已经出现与角磨机完全无关的 SOP 内容，现有 Schema 只能检查结构，不能检查语义正确性。
4. RAG 检索未按用户选择的手册隔离，可能把未选择的手册内容发送给外部 Provider。
5. `make security` 当前失败，依赖审计发现已知漏洞。
6. Playwright 只有三个依赖本地预置数据库的冒烟用例，CI 实际没有运行 Playwright。
7. 阶段状态、README、数据与模型页面仍有多处过期或相互矛盾的声明。

因此，当前最准确的项目状态是：

- **工程底座：可用。**
- **视频到 SOP 原型：可运行，但质量与隐私门禁不合格。**
- **确定性评分引擎：可用，但输入仍来自 Fixture 或人工判定。**
- **真实自动视觉考评：未实现。**
- **真实模型训练：未开始。**
- **阶段 12 Windows/Docker/最终打包：按要求未实现。**

## 2. 已验证通过的内容

本次审核实际执行并通过：

| 检查 | 实际结果 |
|---|---|
| 后端测试 | `157 passed` |
| 后端覆盖率 | `82.01%`，达到当前 80% 门槛 |
| mypy strict | 77 个源文件，0 error |
| 前端 Vitest | 11 passed |
| 前端构建 | 通过，PWA 生成 31 个预缓存条目 |
| Playwright（本机已有数据库） | 3 passed |
| Ruff/Bandit | 通过，Bandit 0 issue |
| IMPACT smoke | 通过，5 个视角可见，许可文件存在 |
| 浏览器控制台 | 审核流程中无未处理错误 |
| 真实视频无模型拒判 | 报告显示 `inconclusive`、0% 证据、`no-vision-adapter` |

界面审核截图位于：

`reports/generated/ui-audit-2026-08-16/`

## 3. 阻断问题（P0）

### P0-1：真实自动考评仍未实现

**证据**

- `backend/gongxu/vision/adapters.py:45-52`：即使 hand 与 tool_detector 被登记为 available，仍返回 `NoVisionAdapter`。
- `backend/gongxu/vision/pipeline.py:162-180`：预录视频只抽帧，随后固定抛出 `adapter_unimplemented`。
- `models/` 为 0B，`data/processed/` 为 0B，仓库内没有 ONNX/PT/PTH 权重。
- `Makefile:43-50`：`make train` 运行 Fixture 实验；`make export-onnx` 只是打印占位文本。
- `backend/gongxu/ml/experiment.py:44-95`：指标来自 `pass/missing_step_1/wrong_tool_step_5` 三种预置序列，框架明确是 `deterministic-aligner (no learned params)`。

**影响**

真实摄像头或真实上传视频无法产生步骤识别、工具识别或自动分数。当前只能证明“系统会诚实拒判”，不能证明“系统能考评”。

**必须整改**

1. 冻结一个首发工序，不再同时泛化多个工种。
2. 建立真实视觉适配器：手部关键点、工具/零件检测、工位坐标归一化、手物关系事件、步骤状态机。
3. 使用训练/验证/独立测试隔离的数据产出真实权重、模型哈希、模型卡和指标。
4. 实时与预录必须调用同一适配器；禁止两套判断逻辑。
5. `make train/evaluate/export-onnx` 必须生成真实产物，Fixture 实验更名为 `make fixture-smoke`。

**验收条件**

- `models/` 中存在可追溯的模型文件、SHA256、许可与来源。
- 真实视频不再固定返回 `no-vision-adapter`。
- 至少 20 条独立测试视频上输出步骤级指标、拒判率、延迟和失败案例。
- 同一人物、同一执行、同一多视角组不得跨 train/val/test。

### P0-2：SOP 语义错误仍可通过校验并发布

**证据**

浏览器实际查看最新 `impact_grinder_safe_subassembly v7` 草稿，内容包含：

- `Video Signal Generator`
- `Digital Overlay Encoder`
- `Vectorscope`
- `Waveform Monitor`
- `obj_01`、`obj_02` 等占位对象

这些内容与角磨机安全拆装无关，但页面仍提供“发布”按钮。截图：

`reports/generated/ui-audit-2026-08-16/04-sop-studio.png`

代码原因：

- `backend/gongxu/sop/validation.py:54-100` 只检查字段、DAG、权重和证据 kind，不检查工具/对象/动作是否属于当前工序。
- `backend/gongxu/sop/service.py:208-275` 会给缺少名称或证据的模型输出自动补占位内容，导致低质量输出被“修成合法 JSON”。
- `backend/gongxu/sop/service.py:230-233` 与 `271-273` 生成 `keyframe-{i}`，不是真实时间戳或文件哈希。
- `backend/gongxu/sop/service.py:312-322` 没有持久化传入的 `reference_video_asset_id`。

**影响**

系统可能把结构正确但业务完全错误的 SOP 发布给学员，并据此评分。这是比赛演示和真实教学场景的双重阻断项。

**必须整改**

1. 禁止 Provider 输出缺少动作、工具、对象或真实证据定位时自动兜底为可发布 SOP。
2. 每步证据必须保存 `asset_id + sha256 + start_ms/end_ms + frame_hash`。
3. 引入工序词表/工具目录一致性检查和手册证据蕴含检查。
4. 加入第二次自动批判 Provider，输出逐步 `supported/unsupported/conflict`，但最终发布门禁由服务端确定性规则决定。
5. 草稿必须经过普通教师/管理员确认；这不是“行业专家审批”，而是防止自动生成内容直接进入正式考评。
6. 增加可编辑草稿、差异对比、驳回原因和重新编译能力。

**验收条件**

- 上述错误工具示例必须被自动拒绝发布。
- 每一步都能点击定位到真实视频时间段或手册页码。
- 删除/替换来源资产后，关联 SOP 必须显示来源失效。
- 10 组刻意错误的 Provider 输出全部被门禁拒绝。

### P0-3：RAG 未按所选手册隔离，存在错误引用和外发风险

**证据**

- `backend/gongxu/assets/indexing.py:73-94`：`retrieve_chunks` 解密并检索数据库中的全部文档块，没有 `asset_ids` 过滤参数。
- `backend/gongxu/sop/service.py:139-151`：虽然校验了 `manual_asset_ids`，循环内却每次调用全库检索；选择两个手册会重复两次全库结果。
- `backend/gongxu/providers/qwen.py:107-128`：这些 RAG 文本与关键帧随后发送给外部 Provider。

**影响**

用户选择 A 手册时，系统可能引用 B 手册，甚至把未选择的内部文档片段发送给第三方 API。

**必须整改**

1. `retrieve_chunks(db, query, asset_ids, top_k)` 强制限定资产集合；空集合不得检索全库。
2. 检索结果去重，并保存 `asset_id/chunk_id/content_hash/page_or_section`。
3. Provider 请求前生成出站清单，禁止未选择资产进入请求。
4. 为多租户/多组织预留 owner/tenant 边界，首版至少按显式资产白名单隔离。

**验收条件**

- 创建 A/B 两份含唯一暗号的手册，只选 A 编译时请求体绝不能出现 B 暗号。
- 自动化测试同时覆盖单手册、多手册、空手册和无权限手册。

## 4. 高优先级问题（P1）

### P1-1：外部 Provider 隐私声明与真实实现不一致

- `backend/gongxu/providers/base.py:35-43` 和 `qwen.py:104-105` 声称关键帧已模糊人脸并去 EXIF。
- `backend/gongxu/sop/service.py:157-175` 实际直接从解密视频抽帧，没有人脸/铭牌脱敏步骤。
- `SourceAsset` 只有 `license_note/authorized_by`，没有“允许发送给第三方 Provider”的独立同意字段。
- `backend/gongxu/sop/service.py:177-182` 丢弃 analyze 调用收据，只保存 compile 调用收据，审计链不完整。
- `qwen.py:122-137` 实际最多发送 6 帧，但收据重新遍历全部帧，`keyframes_sent` 可能与真实发送数不一致。

**整改**：增加出站同意、帧级脱敏、Provider 域名白名单、两次调用收据、准确的帧哈希与数量、可关闭云端视觉的本地模式。

### P1-2：安全依赖门禁当前失败

实际执行 `make security`：

- `cryptography 45.0.7` 命中多条已知漏洞；当前约束 `>=44.0,<46.0` 阻止升级到修复版本。
- `pytest 8.4.2` 命中已知漏洞，修复版本为 9.0.3；当前约束 `<9.0`。

**整改**：升级约束和锁文件，重新运行加密篡改/错误密钥/迁移/全量测试；不允许简单忽略 pip-audit 结果。

**验收**：`make security` 退出码为 0，CI 同样通过。

### P1-3：角色、CSRF 与 WebSocket 边界仍有缺口

- `backend/gongxu/api/routes/assessment.py:136-157`：创建考评会话没有 CSRF 依赖。
- 同一代码允许 `READERS` 创建会话，因此 observer 也可创建，违背 `observer 只读`。
- `backend/gongxu/api/routes/auth.py:102-118`：logout 未校验 CSRF。
- `backend/gongxu/api/routes/live.py:70-124`：未校验 WebSocket Origin，也未要求会话 `mode == live`。
- 前端 `LiveView.vue:37-40,228-237` 加载并展示全部 recorded/live 会话；实测实时页默认选中 recorded completed 会话。

**整改**：observer 禁止创建；创建/登出统一 CSRF；WebSocket 增加 Origin 白名单、live 模式和允许状态检查；实时页只列 live 且未结束会话。

### P1-4：实时会话内存状态可能永久残留

- `backend/gongxu/api/routes/live.py:38-39` 使用进程级 `_LIVE_STATE`。
- 只有正常 finish 成功后才在 `live.py:364` 删除；普通断开在 `121-124` 仅关闭数据库。

**影响**：大量断线或恶意创建会话会持续保留最多 500 条检测对象，直到进程重启。

**整改**：增加最后活动时间、TTL 清理、单用户/单 IP 并发限制、断线宽限期、最大全局会话数和恢复测试。

### P1-5：E2E 并不是可移植的 CI 门禁

- `frontend/e2e/auth.spec.ts:3-4` 明确依赖外部启动的后端和固定测试账号。
- `auth.spec.ts:17-19,35-38` 依赖开发数据库中预先存在的 SOP 和历史会话。
- `.github/workflows/ci.yml:42-59` 只运行 Vitest 和 build，没有安装浏览器、启动后端/worker 或运行 Playwright。
- 目前仅 3 个 E2E，用例没有覆盖上传、处理任务、SOP 编译、发布拒绝、预录拒判、实时重连、离线模式、管理员和移动端。

**整改**：测试前自动建立空数据库、迁移、种子账号和最小资产；CI 启动后端/worker/Vite；扩展主流程并归档 trace/screenshot。

### P1-6：状态和产品文案存在误导

- `docs/IMPLEMENTATION_STATUS.md:36-51` 仍把视觉、训练、预录、实时标为“已完成”，与文件顶部“无模型、未训练”矛盾。
- `reports/generated/remediation-verification.md:29-30` 将“数据与训练”“真实实时”标为完成，容易误读。
- `frontend/src/views/DataModelsView.vue:23-33` 把数据状态硬编码为“尚需下载”，但本地 `data/raw` 已有 6GB。
- 模型页写“使用 Fixture 视觉源”，而默认配置是 demo fixture 关闭、真实输入 fail-closed。
- README 宣称 OpenCV/ONNX 本地推理与真实自动考评，但依赖和适配器尚未实现。

**整改**：建立单一能力清单 API，文档、UI、报告由同一状态源生成；区分 `implemented/integrated/validated/contest_ready`。

## 5. 中优先级问题（P2）

1. 登录限流只按 IP，成功登录会清空该 IP 全部失败次数；共享 NAT 下可被合法账号反复重置。应改为 IP+用户名双桶。
2. Qwen 429 立即失败，没有按声明的 3 次退避重试；5xx 重试也没有指数退避和 jitter。
3. `compile_sop_from_media` 尚未接入 ASR、OCR 和本地检测摘要，当前三个字段固定为空。
4. 移动端数据表虽然页面无整体横向溢出，但表格右侧内容被裁切，缺少明确横向滚动提示或卡片化行视图。
5. 知识导入仍使用浏览器原生文件选择控件，操作列按钮文字在桌面截图中发生窄列换行。
6. 首页技术字段大量直接显示英文枚举和内部 `process_key`，不适合比赛演示。

## 6. 下一步执行计划

### 阶段 A：真实性与安全止血（2-3 天）

1. 修复 P0-3 RAG 资产隔离并补回归测试。
2. 增加 Provider 出站同意与真实脱敏；完整记录两次调用收据。
3. 升级漏洞依赖，使 `make security` 全绿。
4. 修复 observer/CSRF/WebSocket mode/origin/TTL 问题。
5. 修正文档、状态页和验收报告，不再把数据导入写成训练完成。

**阶段门禁**：后端全测、覆盖率、mypy、Ruff、Bandit、pip-audit、前端测试和 build 全绿。

### 阶段 B：SOP 可信生成闭环（3-5 天）

1. 保存真实视频时间戳、帧哈希、源资产和手册页码。
2. 建立工具/对象目录与工序语义一致性检查。
3. 增加自动批判、冲突检测、支持度评分和发布阻断。
4. 增加可编辑草稿、版本差异、驳回与重编译。
5. 重新用一个明确工序生成 SOP，清理数据库中错误演示草稿。

**阶段门禁**：10 组错误 SOP 全部拒绝；正确 SOP 的每步证据均可追溯；禁止无动作/无对象/占位对象发布。

### 阶段 C：真实视觉 MVP（建议 2 周）

1. 确认首发工序、工具清单、零件清单和 5-8 个步骤。
2. MediaPipe/同类方案提取手部关键点；训练轻量工具/零件检测器。
3. 用 AprilTag/工位点位做跨视角归一化。
4. 生成手-工具-对象-动作-状态事件，接入现有对齐器。
5. 先用状态机获得可解释基线，再根据数据量决定是否训练 TCN。
6. 导出 ONNX，接入统一 `VisionAdapter`，预录与实时共用。

**阶段门禁**：真实独立测试视频有可复现指标；错误工具、漏步、错序、遮挡与拒判均有案例；模型哈希与版本在 UI 显示。

### 阶段 D：可复现 E2E 与比赛演示（4-6 天）

1. 空库一键 seed，自动启动 API、worker、前端和 Replay Provider。
2. Playwright 覆盖知识导入、SOP 编译、错误发布阻断、预录考评、实时断线重连、离线复核、管理员。
3. 覆盖 390x844、768x1024、1440x900，无横向溢出和小于 44px 的主触控目标。
4. 演示数据与真实验证数据分开，所有 Fixture 明确标识。
5. 形成 6 分钟主演示和断网/Provider 失败/摄像头失败备用演示。

### 阶段 E：阶段 12 发布工程（核心闭环通过后再做）

1. Windows 11 PowerShell 安装、启动、停止、测试、训练和打包。
2. Docker 非 root 镜像、健康检查和本地卷。
3. 冷启动、断网和新目录验收。
4. SBOM、第三方许可、模型卡、数据卡、Claims Sheet、演示材料统一生成。

## 7. 建议 Claude 下一轮只做的任务

下一轮不要直接开始 Windows 打包，也不要先扩大 UI。请只完成以下四项：

1. 修复 RAG 资产隔离与 Provider 出站隐私/审计。
2. 修复依赖漏洞、角色/CSRF/WebSocket/内存状态问题。
3. 建立 SOP 语义质量门禁并阻止当前错误 SOP 发布。
4. 把文档、状态页、CI/E2E 声明统一为真实状态。

完成后提交：

- 逐项修复 commit；
- 新增测试清单；
- `make test`、覆盖率、`make security`、前端 build、Playwright 的原始结果；
- 一份新的 `remediation-verification-v2.json/.md`；
- 不得覆盖本报告和审核截图。

## 8. 需要项目负责人确认的两项产品决策

1. **首发演示工序**：继续使用断电角磨机安全拆装，还是改为更安全、更容易现场重复演示的滤芯/小型机械模块更换？
2. **云端隐私策略**：是否允许把标准教学视频的关键帧发送给外部 Qwen API？若允许，是否要求人脸/工牌/屏幕内容先本地脱敏并记录逐资产授权？

这两项确认后，阶段 C 的数据、工具类别、检测模型和答辩演示才能冻结。
