# 工序智传阶段 7-11 真实性整改报告

> 面向后续开发智能体（Claude）的执行文档  
> 审核日期：2026-08-15  
> 审核基线：`main` / `44bde7e`  
> 仓库：`/Users/a1/Desktop/sword-edge-demo/gongxu-zhichuan`  
> 本轮范围：整改阶段 7-11；阶段 12 按项目负责人要求继续暂缓

## 1. 执行结论

当前仓库已经具备较完整的工程骨架、账号系统、数据库、任务队列、结构化 SOP、确定性评分器和可交互前端，但不能接受“阶段 1-11 已全部完成”的表述。

真实状态如下：

| 阶段 | 审核结论 | 主要原因 |
|---|---|---|
| 1 独立基线 | 通过 | 独立仓库、AGPL、提交历史和原创性边界已建立 |
| 2 后端与账号底座 | 基本通过 | 认证、Argon2、会话、AES-GCM、审计和任务队列存在；仍需补 CSRF/限流及资源归属检查 |
| 3 数据适配 | 部分完成 | 只有官方来源发现和协议资产 manifest；没有下载、试次级解析、训练划分或真实样本 |
| 4 知识导入 | 部分完成 | 文件加密、文档解析、视频转码/抽帧存在；ASR、OCR 和视频语义分析缺失 |
| 5 Provider/RAG/SOP | 部分完成 | Replay 和文本 RAG 可用；Qwen Provider 未真正发送图像，真实凭据未验收 |
| 6 SOP 编译室与工位 | 基本完成 | 页面、结构化 Schema、发布和工位标定存在；SOP 内容仍主要来自 Fixture/Replay |
| 7 视觉事实层 | 未完成真实链路 | 没有手部、工具、对象、动作推理模型；模型可用分支仍是 TODO |
| 8 训练与实验 | 未完成 | `models/`、`data/raw/`、`data/processed/` 均为空；现有指标来自 Fixture |
| 9 预录考评 | 仅演示闭环 | 模型缺失时忽略视频并使用预置检测，可能生成与输入视频无关的报告 |
| 10 实时考评 | 仅演示闭环 | 浏览器没有摄像头采集，WebSocket 接收客户端自报语义标签且缺少认证 |
| 11 产品 UI 与离线模式 | 部分完成 | 主要页面可用；离线页是人工自评，离线规则与后端不一致 |
| 12 Windows 与最终验收 | 暂缓 | 项目负责人明确要求本阶段暂不实施，不计入本轮缺陷 |

本轮整改目标不是增加更多页面，而是把阶段 7-11 从“Fixture 可演示”升级为“真实输入可验证、失败时可信拒判、指标可复现”。

## 2. 不可违反的边界

Claude 实施时必须遵守以下规则：

1. 不覆盖、回退或删除已有提交、运行数据和用户修改。
2. 在新分支 `claude/remediation-stages-7-11` 工作，按工作包形成小而真实的提交。
3. Fixture 只能用于单元测试和显式演示模式，不能在正常生产 API 中静默替代真实模型。
4. 模型缺失、视频不可读、证据不足、遮挡严重或质量不合格时必须返回 `inconclusive` 或明确错误，不能生成伪精确评分。
5. 不使用 held-out test 调参，不把测试集结果写成验证集结果，不虚构训练或准确率。
6. 只使用官方、许可明确的数据；无法合法取得时停止该数据任务并生成阻塞说明。
7. 不把 API Key、原始数据、用户视频、明文关键帧、运行数据库或模型缓存提交 Git。
8. 大模型只负责 SOP 语义理解和教学反馈，不得直接修改数值分数。
9. 不允许客户端直接声明 `tool`、`object`、`action` 后被服务端当作可信视觉事实。
10. 不为了通过测试降低认证、加密、拒判、清理或证据要求。
11. README、实施状态、模型卡和数据卡必须描述真实完成情况，不能把目标或 Fixture 指标写成现有能力。
12. 阶段 12 的 Docker、Windows 便携包和最终发布流水线继续暂缓，除非项目负责人另行授权。

## 3. 当前可复现的审核基线

整改前应先保存以下结果，便于前后对比：

- Git：`main`，基线提交 `44bde7e`，审核结束时工作树干净。
- 后端使用 `PYTHONPATH=..` 临时修正后：`140 passed`，分支覆盖率 `81.95%`。
- 后端直接运行 `uv run pytest`：测试收集失败，`ModuleNotFoundError: ml`。
- `make train`：失败，找不到 `ml.experiment`。
- `uv run mypy gongxu`：115 个错误，配置却声明 `strict = true`。
- `make security`：Bandit 0 问题，但 `tests/security` 不存在，因此目标失败。
- 前端 Vitest：10 项通过。
- 前端生产构建：通过；PWA precache 31 项。
- `npm run test:e2e`：失败，Playwright 未安装。
- `models/`、`data/raw/`、`data/processed/`：均为 0B。
- 浏览器桌面主流程可打开，控制台未发现未处理错误。
- 390x844 下无整页横向溢出，但导航触控高度约 38px，且图标模式缺少可访问名称。

整改结束后必须将新的命令、退出码、测试数、覆盖率和模型状态写入 `reports/generated/remediation-verification.json` 和同名 Markdown 报告。

## 4. P0 工作包：必须首先完成

### 4.1 P0-A：WebSocket、会话和报告权限修复

涉及文件：

- `backend/gongxu/api/routes/live.py`
- `backend/gongxu/api/routes/assessment.py`
- `backend/gongxu/security/auth.py`
- `backend/gongxu/assessment/service.py`
- `backend/tests/test_live.py`
- `backend/tests/test_assessment.py`

实现要求：

1. 为 WebSocket 增加 HttpOnly 会话 Cookie 认证；在 `accept()` 前完成认证和授权。
2. 未登录连接使用 WebSocket 关闭码 `4401`，无资源权限使用 `4403`，会话不存在使用 `4404`。
3. 建立统一的 `authorize_assessment_session(user, session, action)` 服务，REST 和 WebSocket 共用，禁止路由各写一套。
4. learner 只能创建、查看和运行自己的会话与报告。
5. instructor 可以查看和运行其负责范围内的会话；首版没有班级关系时，可暂时允许 instructor 查看全部，但必须在代码和文档中明确。
6. observer 只能读取，不能创建、运行、结束、评分或修改。
7. admin 拥有全局管理权限。
8. `GET /assessment-sessions` 必须按角色过滤，不能向 learner 返回全体学员数据。
9. `GET /assessment-sessions/{id}`、`.../report`、`GET /reports/{id}` 都必须做资源归属校验。
10. WebSocket 不再接受浏览器传入的 `hand/tool/object/action/relation` 作为可信事实；测试注入必须使用后端依赖注入或专用测试适配器。
11. 登录增加可配置的失败速率限制；修改状态的 Cookie 请求增加 CSRF 防护或严格同源双提交令牌。
12. `secure` Cookie 不能写死为 `False`，增加 `GONGXU_COOKIE_SECURE` 配置，并在生产配置中默认开启。

完成定义：

- 未登录 WebSocket 无法读取、恢复或结束任何会话。
- learner A 无法读取 learner B 的会话和报告。
- 篡改或猜测会话 ID 不泄露资源存在性和评分内容。
- 新增越权、未登录、停用账号、会话过期、重连授权测试。
- 认证相关测试和完整后端测试全部通过。

### 4.2 P0-B：移除正常接口中的 Fixture 静默回退

涉及文件：

- `backend/gongxu/vision/pipeline.py`
- `backend/gongxu/vision/fixture.py`
- `backend/gongxu/api/routes/assessment.py`
- `frontend/src/views/AssessmentView.vue`
- `frontend/src/views/LiveView.vue`
- `backend/tests/test_vision.py`

实现要求：

1. 正常预录考评必须绑定 `input_asset_id`，并验证资产存在、类型为 video、当前用户有权使用。
2. 删除生产 `ProcessRequest` 中的 `scenario` 字段。
3. 模型缺失时不得调用 `fixture_detections`，应返回结构化拒判：
   - `status=inconclusive`
   - `reason_code=model_unavailable`
   - 明确列出缺失模型
4. Fixture 迁移到显式演示通道，只能在 `GONGXU_DEMO_FIXTURES=true` 时启用。
5. 演示 API、演示会话、演示报告必须带 `demo=true` 和 `evidence_source=fixture`，界面持续显示“预置演示，不代表真实识别”。
6. 真实模型登记为 available 但推理未实现时，系统应启动失败或模型自检失败，不能执行空 TODO 后继续。
7. 报告中的 `model_version` 必须来自实际推理适配器，不能继续写固定 `fixture-v1` 或 `live-fixture-v1`。
8. 普通考评页删除“正确操作/漏步骤/错工具”场景选择器，改为真实视频资产选择、文件哈希、处理进度和拒判状态。

完成定义：

- 无视频无法启动正常预录考评。
- 无模型只能得到可信拒判，不能得到 59、96、100 等数值分数。
- 正常 API 和 WebSocket 中没有客户端可控制的 Fixture 开关。
- Fixture 仅在测试或显式演示模式工作，并具有显著、不可隐藏的来源标记。
- 增加回归测试证明不同输入视频不会被同一 Fixture 覆盖。

### 4.3 P0-C：隐私衍生物修复

涉及文件：

- `backend/gongxu/media/processor.py`
- `backend/gongxu/assets/service.py`
- `backend/gongxu/security/filecrypto.py`
- `backend/gongxu/api/routes/assets.py`
- `backend/gongxu/db/models.py`
- Alembic migration

实现要求：

1. `proxy.mp4`、关键帧和证据片段不得以长期明文形式存放在 `data/derivatives/`。
2. 建议新增 `derived_assets` 表，至少包含：
   - `id`
   - `source_asset_id`
   - `kind`
   - `sha256`
   - `encrypted_path`
   - `created_at`
   - `expires_at`
   - `deleted_at`
3. 需要长期使用的衍生物采用 AES-256-GCM 文件加密；只在权限受限的任务临时目录短暂解密。
4. 只用于单次模型推理的关键帧应在任务结束后立即删除，不进入长期存储。
5. 任何异常、取消、超时和进程恢复路径都必须清理临时明文。
6. 增加定时清理服务，处理过期密文、孤立文件、残留临时目录和无数据库记录的文件。
7. 上传、解密访问、删除、过期清理和篡改失败均写入审计日志。
8. API 响应不再返回服务器内部 `encrypted_path`。
9. 以文件系统快照测试证明普通监测和失败任务期间没有长期原始帧落盘。

完成定义：

- `file`、`ffprobe`、播放器不能把密文当普通 MP4 打开。
- 错误密钥和篡改密文解密失败。
- 临时明文和到期文件清理率 100%。
- 清理操作全部有审计记录。
- API 不暴露本地绝对存储路径。

## 5. P1 工作包：真实核心链路

### 5.1 P1-A：标准视频到结构化 SOP

目标链路：

`授权视频/手册 -> 转码与时间戳 -> ASR/OCR/关键帧/场景 -> 多模态分析 -> RAG -> Schema 校验 -> SOP 草稿 -> 发布`

实现要求：

1. 为视频处理任务增加阶段化检查点：`probe`、`transcode`、`asr`、`ocr`、`scenes`、`vision`、`compile_ready`。
2. ASR 默认采用本地 Faster-Whisper CPU/int8 路径；模型未安装时明确标记该信息源缺失，不伪造转写。
3. OCR 选择一个 Windows 11 可用且许可明确的实现，建议优先 RapidOCR/ONNX；不要同时引入多套 OCR 框架。
4. 保留每条 ASR/OCR 结果的开始时间、结束时间、置信度、来源文件哈希和模型版本。
5. 多模态 Provider 必须真正发送关键帧图像内容或供应商支持的视频内容，不能只发送本地文件路径字符串。
6. 出站前执行隐私策略：只发送用户授权的标准教学资产关键帧，不发送学员考评原视频；记录发送帧数、摘要、Provider 和调用回执。
7. `POST /sops/compile` 改为接受 `reference_video_asset_id`、`manual_asset_ids` 和 `process_key`，不能让前端直接提交任意 `analysis` 伪造结果。
8. Provider 输出先通过 JSON Schema/Pydantic 校验，再通过 DAG、步骤编号、权重、来源等级和证据引用校验。
9. 每个 SOP 步骤至少绑定一个真实来源：视频时间段、ASR 时间段、OCR 帧或手册页码。
10. Provider 失败后可重试和恢复；重放模式必须读取归档回执，不用硬编码 Fixture SOP 冒充实时分析。
11. UI 展示处理阶段、来源覆盖率、失败原因、证据预览和“未验证草稿”状态。

完成定义：

- 一段此前未登记的授权标准视频能自动生成带真实时间引用的 SOP 草稿。
- 删除或替换输入视频后，SOP 证据哈希随之变化。
- 关闭外部 API 时仍可查看已缓存 SOP，但不能把 Replay 结果宣称为新视频分析。
- 对 Provider 返回的非法步骤、环形依赖、无来源步骤和权重错误进行拒绝测试。

### 5.2 P1-B：真实视觉事实层和预录考评

建议结构：

```text
backend/gongxu/vision/
  adapters.py          推理适配器协议与依赖注入
  hand.py              手/手腕/手掌关键点
  detector.py          工具、零件、PPE 检测
  relations.py         手-工具-对象空间关系
  action.py            短时动作事件
  quality.py           可见性、遮挡、多人、视角质量
  pipeline.py          统一编排，不包含具体模型硬编码
```

实现要求：

1. 选用 Windows 11 CPU 可运行、许可兼容 AGPL 项目的预训练视觉组件；模型来源、许可、版本和 SHA256 必须登记。
2. 视频按真实时间戳重采样，默认 8-10 FPS；不能只按 1 FPS 抽帧进行动作判定。
3. 将像素坐标通过已配置工位标定转换为工位坐标；没有标定时使用人体/手部尺度归一化并降低置信度。
4. 事实层输出 `hand-tool-object-action-state` 关系事件，而不是比较两段视频的原始像素。
5. 检测结果必须包含模型版本、时间戳、类别、置信度、框/关键点、帧哈希和质量标记。
6. 工具和对象类别必须来自当前 SOP/工位目录的受控词表。
7. 对严重遮挡、不可见安全参数、多人与画面外操作返回 `unobservable` 或 `insufficient_evidence`。
8. 预录报告必须能回放步骤级证据：匿名关键点/框、时间区间、关系事件和风险曲线。
9. 若保留视频证据片段，只保存失败步骤附近的加密短片并遵循保留期限。
10. 评分器继续保持确定性；视觉模型只提供事实和置信度，不直接输出最终总分。

完成定义：

- 至少用三段真实且授权的视频证明：正确流程、漏步骤、错工具。
- 报告中每个通过或扣分项都能追溯到真实视频时间段和模型事实。
- 把视频改成无关内容时必须拒判，不能仍然给出高分。
- 相同视频、模型和配置重复运行得到相同评分；允许的浮点误差需写入测试。

### 5.3 P1-C：实时摄像头考评

实现要求：

1. 浏览器使用 `getUserMedia` 获取摄像头，默认 640x360 或 640x480、8-10 FPS。
2. 前端只发送压缩图像帧、单调递增采集时间戳和帧序号，不发送语义检测标签。
3. WebSocket 增加消息大小、帧率、队列长度和会话时长限制。
4. 后端使用与预录考评相同的视觉适配器，避免实时和预录出现两套判断逻辑。
5. 实现有界队列和最新帧优先背压，真实统计接收帧、处理帧、丢帧和延迟。
6. 断线重连恢复步骤状态，但不能在进程内无限保留 `_LIVE_STATE`；会话结束、超时和异常必须释放。
7. 完成摄像头授权拒绝、切换设备、流中断、标签页后台、服务重启和重连状态。
8. 原始预览只在浏览器本地显示；后端不得为实时预览持久化原始帧。
9. 实时结束后生成与预录相同的数据结构和确定性评分报告。

完成定义：

- 正常页面确实调用摄像头，而不是播放 Fixture 计时器。
- 浏览器开发者网络记录中看不到客户端自报工具/动作标签。
- 无登录、跨用户、超帧率、超大帧和重复 finish 均被拒绝。
- 流中断后能重连；结束后内存状态被回收。

### 5.4 P1-D：数据、标注、训练与 ONNX

实现要求：

1. 修正 IMPACT manifest：当前 37 条是协议目录资产，不得称为“试次级样本”。
2. 修复数据卡标题 `IMPACT vv1.1` 为正确版本，并区分“官方可用数据”与“本地已下载数据”。
3. 只有项目负责人确认许可后，才从官方 GitHub/Hugging Face revision 下载 annotations/sample；记录 SHA256。
4. 将官方标注解析为真正的 trial、participant、view、task、step interval、split 条目。
5. 同一人员/同一试次的所有视角必须进入同一划分，防止跨视角泄漏。
6. 训练、验证、测试集完全隔离；阈值只在验证集选择，测试集只运行最终一次归档评估。
7. 自动预标注可以用于降低人工量，但必须保留来源和置信度；不能把大模型预标注当作无误真值。
8. 先建立可复现基线，再训练轻量动作/关系时序模型。工具检测若采用预训练权重，必须如实标注“迁移使用”，不得称为团队从零训练。
9. 固定种子 `42、2026、731`，归档环境、配置、数据哈希、训练曲线和最佳权重。
10. 评估至少包含：步骤 macro-F1、步骤 precision/recall、漏项和错序 precision/recall、工具错误识别、拒判率、证据覆盖率、编辑距离、延迟 p50/p95。
11. 输出逐任务、逐视角、逐人员结果和 95% 置信区间，不能只报总体平均值。
12. 导出 ONNX 后验证输入输出 Schema、动态 batch、PyTorch/ONNX 最大误差 `<1e-4`。
13. 权重文件登记 SHA256、许可、训练数据版本和验证状态；未满足真实门槛时标记 `experimental_unvalidated`。
14. 将顶层 `ml/` 改为可正确安装/导入的包。推荐迁入 `backend/gongxu/ml/`，统一由后端项目管理依赖；不要依赖手工设置 `PYTHONPATH`。

完成定义：

- `make data-smoke` 在已授权样本上通过；无数据时明确返回“未下载”，不伪造成功。
- `make train`、`make evaluate`、`make export-onnx` 都是真实命令，不再是 echo 或 Fixture-only。
- 模型卡明确区分 Fixture 工程指标、公开数据指标和真实工位视频指标。
- 没有真实训练时，UI 不显示“模型已验证”。

## 6. P1 工作包：工程质量门禁

### 6.1 测试和 CI

必须修复：

1. `cd backend && uv run pytest` 无额外环境变量即可成功。
2. `make test` 同时运行后端和前端测试并返回 0。
3. `uv run mypy gongxu` 达到 0 error；若严格类型确有例外，只能局部、带原因忽略，不能整体关闭 strict。
4. 安装并锁定 `@playwright/test`，创建真实 `frontend/e2e/` 用例。
5. 创建存在且有内容的 `backend/tests/security/`，覆盖认证、越权、CSRF、上传、路径、加密、篡改和清理。
6. `make security` 必须真实运行秘密扫描、Bandit 和安全测试。
7. CI 增加 mypy、前端 E2E、许可检查和依赖审计；不能使用本地必败的命令。
8. 测试不得以删除失败场景、`skip` 或固定返回成功的方式变绿。
9. CI 使用 Replay Provider 和小型授权测试夹具，不调用付费外部 API。
10. Provider live 模式通过 mock server 验证多模态请求格式；真实凭据验收另行归档。

最终命令门禁：

```bash
make test
make security
make train
make evaluate
make export-onnx
cd backend && uv run ruff check gongxu tests
cd backend && uv run mypy gongxu
cd frontend && npm run build
cd frontend && npm run test:e2e
```

无授权数据时，`train/evaluate/export-onnx` 可以返回明确的阻塞状态报告，但不能以 Fixture 指标冒充真实训练成功。

### 6.2 数据库和迁移

1. 所有新表和字段必须使用 Alembic migration。
2. 测试空数据库升级、现有数据库升级、重复升级、降级后再升级。
3. 生产启动应检查 migration revision；不能单纯依赖 `create_all()` 掩盖缺失迁移。
4. 会话、报告、观察、衍生资产和审计记录的外键及删除行为必须明确。
5. 清理任务删除文件和更新数据库应具备幂等性。

## 7. P1/P2 工作包：离线能力和 UI 真实性

### 7.1 离线考试

当前实现是用户手动选择每步“通过/失败”，且把顺序、效率固定为 100，不能继续称为自动离线考试或声称“数值与在线一致”。

推荐先执行诚实降级：

1. 将页面改名为“离线人工复核”。
2. 明确显示“判定由操作者手工录入，不是视觉模型自动识别”。
3. 复用后端生成的评分规则版本和官方测试向量，在前端实现逐字段一致的评分结果。
4. 删除固定顺序 100、效率 100 的逻辑；无法离线计算的维度标为不可观察并按同一规则处理。
5. 本地报告标记 `manual_offline`，不得混入在线自动模型指标。
6. localStorage 中的 SOP 和报告视为可篡改缓存，不作为正式成绩；正式结果需重新联网上传并由服务端校验或签名。

在真实 ONNX 模型能够通过 ONNX Runtime Web 离线运行后，再将页面恢复为“离线自动考试”。该升级不应阻塞本轮预录真实链路。

### 7.2 UI 和无障碍

1. 普通考评页面增加：真实视频选择、SHA256、模型版本、处理进度、质量门控、证据播放器和失败原因。
2. 普通实时页面增加：摄像头选择、授权状态、本地预览、实际推理状态和安全停止。
3. Fixture 场景仅放入管理员可见的“答辩演示”页，并持续标记预置数据。
4. `process_key` 不直接作为主要标题，增加用户可读工序名称；技术标识放入次级信息。
5. 手机端采用主要任务优先导航；所有图标按钮增加 `aria-label` 和 tooltip。
6. 主要触控目标宽高均不得小于 44px。
7. 表格在手机端使用可横向滚动的明确容器或列表视图，不隐藏关键列。
8. 覆盖登录、知识导入、SOP、预录、实时、离线人工复核和账号管理的加载、空白、错误、拒判和权限不足状态。
9. `390x844`、`768x1024`、`1440x900` 下无页面级横向溢出和元素遮挡。
10. 控制台无未处理 error；键盘可完成登录、导航、上传、选择和报告查看。

## 8. 建议 API 契约

以下为推荐方向，允许在保持兼容性的前提下调整命名：

```text
POST /api/v1/assets
POST /api/v1/assets/{asset_id}/process
GET  /api/v1/jobs/{job_id}

POST /api/v1/sops/compile
  { process_key, reference_video_asset_id, manual_asset_ids[] }

POST /api/v1/assessment-sessions
  { process_key, mode, sop_version_id, input_asset_id }

POST /api/v1/assessment-sessions/{id}/process
  正常模式无 scenario 参数

GET  /api/v1/assessment-sessions/{id}/report
GET  /api/v1/assessment-sessions/{id}/evidence

WS   /api/v1/ws/assessment-sessions/{id}
  client -> start/frame(binary or encoded image)/finish
  server -> resume/state/reject/report/error

POST /api/v1/demo/assessment-sessions
  仅 GONGXU_DEMO_FIXTURES=true 时可用
```

所有写接口必须具备：身份、角色、资源归属、请求大小限制、幂等键、审计和结构化错误。

## 9. 实施顺序与提交要求

### R1：真实性和安全止血

- WebSocket/REST 权限。
- 移除 Fixture 静默回退。
- 修正文档中的完成状态和错误宣传。
- 提交建议：`fix: enforce assessment authorization and fail-closed inference`

### R2：质量门禁修复

- 修复 `ml` 导入、pytest、mypy、安全测试、Playwright 和 CI。
- 提交建议：`test: restore reproducible backend frontend and security gates`

### R3：隐私衍生物

- 加密衍生资产、临时文件清理、保留期和审计。
- 提交建议：`fix: encrypt derived media and audit retention cleanup`

### R4：标准视频到 SOP

- ASR、OCR、真正多模态请求、来源证据和可恢复任务。
- 提交建议：`feat: compile evidence-grounded SOPs from authorized media`

### R5：真实预录考评

- 视觉适配器、关系事实、质量门控、证据播放器和真实视频验证。
- 提交建议：`feat: assess recorded operations from real visual evidence`

### R6：数据与训练

- 试次 manifest、合法数据适配、训练、评估、ONNX、模型卡。
- 提交建议：`feat: add licensed training evaluation and ONNX pipeline`

### R7：真实实时考评

- 摄像头、帧传输、统一推理、背压、重连和资源回收。
- 提交建议：`feat: run authenticated live camera assessment`

### R8：离线诚实降级与 UI 验收

- 离线人工复核、评分一致性、移动端、无障碍、全流程 E2E。
- 提交建议：`fix: align offline review and responsive product flows`

每完成一个 R 阶段：

1. 更新 `docs/IMPLEMENTATION_STATUS.md`，写真实提交哈希、测试命令和结果。
2. 运行定向测试，再运行当时能够运行的完整门禁。
3. 记录自动修复和剩余风险。
4. 不把多个阶段压成一个无法审核的大提交。

## 10. 最终验收清单

只有以下条目全部满足，才能把阶段 7-11 标为完成：

- [ ] 正常预录接口必须使用真实视频资产，不接受 `scenario`。
- [ ] 无模型、无视频或低质量输入只产生可信拒判。
- [ ] 真实标准视频能生成带真实时间引用的 SOP 草稿。
- [ ] 多模态 Provider 实际发送获授权关键帧，不是文件路径文本。
- [ ] 三类授权真实视频完成正确、漏步骤、错工具验证。
- [ ] 报告每个判断都能追溯到真实时间段和事实记录。
- [ ] 实时页使用真实摄像头和服务端视觉推理。
- [ ] WebSocket 和 REST 全部执行身份与资源归属校验。
- [ ] 普通运行中不存在长期明文代理视频和关键帧。
- [ ] 过期、孤立和临时文件清理测试全部通过。
- [ ] `make test`、`make security`、mypy、build 和 Playwright 全绿。
- [ ] 已授权数据完成试次级解析、隔离划分和可复现实验；否则如实标记未验证。
- [ ] 至少存在一个经过 SHA256 和 ONNX 一致性检查的真实模型权重，或 UI 明确显示模型未安装。
- [ ] 离线功能不再宣称未实现的自动视觉评分。
- [ ] 手机端触控目标不小于 44px，图标有可访问名称。
- [ ] README、实施状态、数据卡、模型卡与归档指标一致。
- [ ] 阶段 12 仍保持未开始，除非收到新授权。

## 11. Claude 最终汇报格式

Claude 完成一轮后，必须按以下结构汇报，不能只说“已完成”：

```markdown
# 整改轮次 N 结果

## 本轮完成
- 工作包和具体行为变化

## 关键文件
- 文件路径和作用

## 测试证据
- 完整命令
- 退出码
- 通过/失败数量
- 覆盖率

## 真实输入证据
- 输入资产 SHA256
- 模型 SHA256
- 数据划分
- 输出报告路径

## 未完成和阻塞
- 未完成内容
- 阻塞原因
- 是否需要项目负责人操作

## 下一轮
- 严格按优先级列出
```

## 12. 需要项目负责人确认的事项

下列事项不应由 Claude 猜测：

1. 是否允许从 IMPACT 官方 GitHub/Hugging Face 下载 `annotations` 和约 2.8 GiB 的 `sample` 数据用于非商业比赛研究。
2. 实际使用的多模态 API 供应商、Base URL、模型 ID，以及是否已经准备好后端环境变量形式的 API Key。
3. 首个真实考评工序是否继续固定为“断电角磨机安全拆装子流程”；该选择决定工具类别、数据适配和演示材料。
4. 离线页面是否接受本轮先改成“离线人工复核”，等真实 ONNX 模型成熟后再恢复自动离线考试。
5. 标准教学视频的关键帧是否允许发送给已选多模态供应商；学员考评视频默认禁止出站。

在这些问题得到答复前，Claude 可以先完成 P0 安全止血、质量门禁、隐私清理和 UI 诚实标记，但不得声称真实模型、真实数据或真实 Provider 已验收。
