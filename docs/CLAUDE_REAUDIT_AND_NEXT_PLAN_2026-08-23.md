# Claude 修改后复审与下一步整改计划

日期：2026-08-23  
审核对象：`claude/remediation-stages-7-11` 当前工作区  
审核基准：HEAD `7a2d459` 加当前未提交修改  
阶段 12：继续按项目负责人此前要求暂缓，不计入本轮已完成能力

## 1. 结论

当前项目已经达到“工程底座稳定、主要页面可构建、标准视频转 SOP 和预录评分链路可演示”的原型层级，但**尚不能认定为全部流程完善，也不能标记为 contest_ready**。

本轮确认了 208 个后端测试、类型检查、前端构建、11 个前端单测、安全测试和依赖审计通过；本地 MediaPipe/YOLO ONNX 权重也能实际加载。但以下事实仍成立：

1. 学员考评视频可能在未获得逐资产外发授权时发送给外部 Qwen Provider。
2. 预录批量推理每 4 帧重置一次时间戳，步骤顺序和证据时间轴不可信。
3. 仅 60% 步骤有证据时仍可能给出 `pass`，与“证据不足明确拒判”的产品承诺冲突。
4. 当前 Playwright 主流程为 1 passed / 2 failed，UI 修改已经打断自动回归。
5. 本地 ONNX 在现有 5 视角归档中均为 0 个步骤通过；Qwen 结果未经过独立真值集验证。
6. 当前真实训练仍未开始，`training-metrics.json` 是 fixture 工程实验，不是模型准确率。
7. Windows 启动脚本不启动 worker，自动评分在 Windows 默认流程中不会运行。
8. 阶段 12 的完整流水线、Windows 便携包和发布验收仍是占位目标。

因此，项目当前适合做“受控演示”，不适合宣称“真实自动考评已经完善”或“准确率已经达标”。

## 2. 实际验证结果

### 2.1 通过项

- `make test`：208 个后端测试通过，mypy 87 个源文件 0 error，前端 11 个 Vitest 用例通过。
- 后端覆盖率：80.38%，达到当前 80% 总体门槛。
- 前端：`vue-tsc -b` 与 Vite production build 通过，PWA 生成成功。
- `make security`：Bandit 0 issue，安全测试 5 passed，`pip-audit` 未发现已知依赖漏洞。
- `git diff --check`：通过。
- 本地模型：`hand_landmarker.task` 与 `yolov8n.onnx` 可加载，空白帧推理无异常。
- 服务现状：本机后端、前端和 Qwen worker 均有运行进程；数据库中 27 个任务成功、1 个失败。

### 2.2 失败项

- `make test-e2e`：1 passed / 2 failed。登录按钮已改为“启动终端”，测试仍查找“登录”。
- Ruff：`backend/tests/test_vision_demo.py:123` 超过 100 字符，CI 会失败。
- `make export-onnx`、`make full-pipeline`、`make package-windows` 仍只输出“后续阶段实现”。
- 当前复审结果尚未归档成新的、与 HEAD 和工作区一致的验证 JSON。

## 3. 阻断问题

### P0-1 外部视觉考评绕过资产外发授权

证据：

- `backend/gongxu/vision/pipeline.py:74-88` 只检查资产存在和类型，没有检查 `allow_provider_export`。
- `backend/gongxu/vision/adapters_qwen.py:69-80` 会把降采样后的画面直接发给外部 Provider。
- SOP 编译已在 `backend/gongxu/sop/service.py:139-152` 正确实现逐资产授权，考评链路却没有复用同一门禁。

整改要求：

- 当 `vision_backend=qwen` 时，未明确授权外发的考评资产必须返回 `inconclusive/provider_export_not_authorized`，不能入队调用外部模型。
- 上传与新建考评会话界面必须明确显示外发对象、用途和最小化策略，授权记录包含操作者、时间、资产哈希和 Provider。
- ONNX 本地路径不需要外发授权，但必须在报告中标记 `processing_location=local`。
- Qwen 视觉调用必须产生 Provider receipt 和审计日志，至少记录模型、prompt 版本、输入摘要、响应摘要、耗时、重试和错误，不记录原始图像。

验收：新增未授权拒绝、授权通过、ONNX 本地不外发、审计不含图像四组测试。

### P0-2 批量关键帧时间戳每 4 帧重置

证据：`backend/gongxu/vision/pipeline.py:199-204` 在每个 batch 内重新 `enumerate(b)`，导致时间戳重复为 0、1000、2000、3000 ms。

整改要求：

- 抽帧函数返回 `frame_path + source_timestamp_ms`，时间戳来自原视频 PTS/时长映射。
- 批处理只切分已经带全局时间戳的 item，不允许 batch 内重新编号。
- 检测结果合并后按 `timestamp_ms` 稳定排序，并拒绝倒序或重复时间戳异常。
- 预录流水线与 CLI demo 必须共用同一采样与时间戳实现。

验收：24 帧、6 个 batch 的时间戳严格递增，首尾覆盖整段视频；报告证据时间能定位回原视频。

### P0-3 证据不足仍可产生“通过”结论

证据：

- `backend/gongxu/scoring/engine.py:142-148` 只有覆盖率为 0 才拒判。
- `reports/generated/batch-verify.json` 中存在 `coverage=0.6`、3 个步骤证据不足、结果仍为 `pass` 的样本。
- README 承诺证据不足时“无法判断”，当前行为与对外口径冲突。

整改要求：

- 推荐默认门禁：任一关键步骤为 `insufficient_evidence`，或总证据覆盖率低于 80%，最终结果为 `inconclusive`。
- `failed` 只表示有明确反证；`insufficient_evidence` 不扣成失败，但也不能在关键证据不足时发放通过结论。
- 临时数值可以展示为 `provisional_score`，不得放在 `total_score/pass` 语义中。
- 评分门禁写入 SOP/scoring profile 版本并随报告归档，不能在 UI 或 demo 脚本中临时覆盖。

验收：覆盖 0%、60%、79%、80%、100%，关键步缺证据和关键步明确失败的边界测试。

### P0-4 自动化浏览器主流程已损坏且覆盖不足

证据：

- `frontend/src/views/LoginView.vue:63-66` 按钮名称为“启动终端”。
- `frontend/e2e/auth.spec.ts:6-41` 仍查找“登录”，导致 2 个用例超时。
- 当前 E2E 只有登录、未登录重定向和页面导航，没有真正执行上传、SOP 编译、考评、报告、离线和权限操作。

整改要求：

- 使用稳定的可访问名称或 `data-testid`，修复现有失败。
- 新增六条独立主流程：知识导入、Replay SOP 编译发布、预录 fixture/Mock Provider 考评、报告证据查看、离线人工复核、管理员账号 RBAC。
- 再增加 Qwen contract 测试，使用录制响应或 mock server，CI 不调用真实外部 API。
- 覆盖 390x844、768x1024、1440x900，检查水平溢出、44px 触控目标和控制台错误。

验收：Playwright 全绿且失败时保存截图、trace、console 日志。

### P0-5 自动视觉考评尚未验证

证据：

- `reports/generated/vision-backend-compare.json` 显示 ONNX 在 front/ego/left/right/top 五视角均为 0 个步骤通过。
- 当前 ONNX 是 MediaPipe hand + COCO YOLO，COCO 不包含首发工序的扳手、砂轮等关键类别。
- Qwen 归档仅是少量演示样本，没有人员/试次/视角隔离的真值评估；同一输入也没有缓存以保证复现。
- `reports/generated/training-metrics.json` 明确是 `fixture-align` 和 `experimental_unvalidated`。

整改要求：

- 不再把通用 COCO ONNX 的“可加载”表示成自动考评已集成。
- 建立首发工序真值集，至少包含正确、漏步、错序、错工具、遮挡和跨视角场景。
- 评估 Qwen-only、local-only、hybrid；输出步骤级 precision/recall/F1、事件编辑距离、拒判率、误判率、延迟 p50/p95 和成本。
- Provider 输出按 `asset_sha256 + model_id + prompt_version + sampling_manifest` 缓存并归档，保证比赛演示可复现且可断网回放。
- 只有在隐藏测试集达到预先写定门槛后，能力状态才能从 implemented 升为 validated。

## 4. 重要问题

### P1-1 实时 Qwen 路径不具备实时性且审计字段错误

- `backend/gongxu/api/routes/live.py:207-208` 在 async WebSocket 处理函数中同步调用视觉适配器。
- `backend/gongxu/providers/qwen.py:45-60` 使用同步 `httpx.post`，单帧可阻塞事件循环数秒。
- `backend/gongxu/api/routes/live.py:221-223` 只给 fixture 设置 evidence source；真实视觉仍保持 `none`。
- `backend/gongxu/api/routes/live.py:401-405` 非 fixture 报告一律写成 `no-vision-adapter`。

整改：Qwen 实时模式改为低频异步任务、仅保留最新帧、丢弃过期帧；真实实时目标应使用本地模型。正确记录 adapter 名称、版本、来源和丢帧原因。

### P1-2 Windows 实际流程不完整

- `windows/Start.ps1:7-18` 只启动 API 和 Vite，不启动 worker；预录自动评分会一直排队。
- `windows/Stop.ps1` 只停止 8011 端口进程，不停止 worker 和前端。
- README 声称存在 Train/Package 脚本，但目录只有 Setup/Start/Stop/Test。

整改：在阶段 12 获得授权前先修正文档，不得声称 Windows 全功能可用。阶段 12 启动时再实现进程清单、PID 文件、健康检查、worker、日志和幂等停止。

### P1-3 7 天保留策略没有真正接入运行流程

- `backend/gongxu/media/processor.py:76-94` 保存 proxy/keyframe 时未传 `expires_at`。
- `backend/gongxu/assets/derived.py:111-151` 有清理函数，但生产代码没有调度调用。
- 当前没有生成失败步骤 evidence clip 的业务链路。

整改：给衍生物设置明确保留策略；推理关键帧任务结束即删，证据短片按配置到期；启动时和每日定时清理并审计。对外文档在实现前删除“已自动 7 天清理”的表述。

### P1-4 数据和模型供应链仍不闭环

- `backend/gongxu/data/assembly101.py:24-33` 使用非官方个人 Hugging Face 镜像，并在 `:65-73` 自动设为允许外发且无授权人。
- `backend/gongxu/vision/fetch_models.py:42-70` 只计算下载后的哈希，没有与预先锁定的官方 SHA256 比较。
- `backend/gongxu/api/routes/capabilities.py:36-43` 仅凭数据库 status 判定 integrated，不校验文件、哈希、加载和最小自测。

整改：禁用未验证镜像；数据授权不得由下载脚本自动代替用户同意；模型清单锁定 URL、版本、许可和 expected SHA256；能力 API 使用运行时自检结果。

### P1-5 未知工序静默回退到角磨机词表

`backend/gongxu/vision/adapters.py:63-66` 对未知 process key 使用默认角磨机词表。这会把正确的新工序对象当成词表外数据丢弃。应 fail-closed 返回 `process_vocabulary_missing`。

### P1-6 凭据与秘密扫描不完善

- 未跟踪的 `USAGE.md:33-41` 包含当前本机可用的教师账号明文密码。
- CI 中名为 Secret scan 的步骤实际只运行 Bandit，不扫描 Git 历史、配置和文档秘密。

整改：立即从交接文档删除真实密码并轮换；演示账号由 seed 首次生成随机密码或要求首次改密；加入 Gitleaks/TruffleHog 类秘密扫描。

### P1-7 当前修改尚未形成可追溯基线

当前有 16 个已修改文件和 2 个未跟踪文件，约 2605 行新增、839 行删除。先修完 P0，再用单一整改提交或按隐私/时间戳/评分/E2E 分提交落库；不得把旧的验证 JSON 当成本轮通过证据。

## 5. Claude 下一轮执行顺序

### 阶段 A：24 小时内完成可信止血

1. 保留当前所有修改，不 reset、不覆盖报告和数据。
2. 修复 Ruff 和 E2E 按钮定位，让基础 CI 恢复全绿。
3. 为 Qwen 考评加入逐资产外发授权、Provider receipt 和审计。
4. 修复预录全局时间戳，补 24 帧/6 batch 回归测试。
5. 加入关键步骤与 80% 证据覆盖拒判门禁，重新生成 demo 报告；不得手改 JSON 数字。
6. 修复实时 evidence source/model version；在异步化前明确把 Qwen 标为“低频分析”，不标“实时考评”。
7. 提交本阶段代码并生成新的 verification JSON/MD，记录命令、提交哈希和真实输出。

### 阶段 B：完成可复现的预录比赛主流程

1. 用官方 IMPACT 已授权数据建立 source timestamp 和标签适配器。
2. 建立固定的开发/验证/隐藏测试清单，按 trial/person/view 隔离；隐藏测试只在最终评估运行一次。
3. 为 Qwen 视觉输出增加摘要缓存和 Replay，比赛演示不依赖现场网络。
4. 增加预置 SOP、一个正确样本、一个漏步样本和一个拒判样本，全部来自归档推理结果。
5. Playwright 真正跑通：登录 -> 选资产/SOP -> 入队 -> worker -> 报告 -> 证据时间轴。
6. 报告中明确区分 `fixture`、`qwen_live`、`qwen_cached`、`onnx_local` 和 `manual_offline`。

### 阶段 C：准确率与模型工作

1. 先完成真值定义和标注工具，不使用当前 demo 分数做调参真值。
2. 评估 Qwen baseline；分析失败类别和视角差异。
3. 若本地视觉继续保留，训练工序专用工具/对象检测器和动作时序模型，不把 COCO 权重当成训练成果。
4. 固定阈值后冻结隐藏测试，生成模型卡、数据卡和失败案例。
5. 未达到门槛时继续标记 `experimental_unvalidated`，比赛演示以“可信拒判 + 证据化报告”为主要卖点。

### 阶段 D：文档与状态统一

1. 更新 README、USAGE、IMPLEMENTATION_STATUS 和 capabilities 单一状态源。
2. 删除或改正“同视频必定同分”“视觉已集成”“7 天自动清理”“Windows 全功能”等尚未被测试证明的说法。
3. 归档模型 URL、expected SHA256、许可、API 模型 ID、prompt 版本、数据 manifest 和环境哈希。
4. 把当前真实成绩、目标门槛和未完成项分栏展示。

### 阶段 E：阶段 12（继续等待项目负责人授权）

获得授权后再实现 Windows 11 完整启动/停止/测试/打包、Docker、完整流水线、冷启动验收和发布包。阶段 12 未完成前，不得在 README 或答辩材料中写“下载即用、全平台完整支持”。

## 6. 完成定义

Claude 只有在以下条件同时满足后才能再次声明本轮完成：

- `git diff --check`、Ruff、mypy、208+ pytest、覆盖率、Vitest、build、Bandit、dependency audit 全绿。
- Playwright 覆盖六条真实主流程并全绿，不只是页面能打开。
- 未授权视频无法到达外部 Provider，授权与调用均有不含图像的审计证据。
- 预录时间戳严格递增并能回到原视频位置。
- 关键证据不足不会产生 pass。
- demo 报告由命令重新生成，JSON、UI、README 和状态 API 数字一致。
- ONNX/Qwen 能力级别按独立测试结果表述，没有把“能加载”写成“已验证”。
- 当前整改形成 Git 提交，验证报告记录准确提交哈希，工作区只保留用户明确要求不提交的产物。

## 7. 给项目负责人的建议

下一轮不要先扩展更多页面，也不要立刻开始阶段 12。应先完成阶段 A 和 B，把“授权视频 -> 可复现视觉事实 -> 正确时间轴 -> 可信拒判/评分 -> 证据报告”这一条比赛主链做实。当前 UI 已有较完整外观，最缺的是评分可信度和可复现证据，而不是更多视觉装饰。
