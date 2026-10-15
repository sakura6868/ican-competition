# 工序智传

将授权的标准教学视频和设备手册编译为结构化、可编辑、可发布的 SOP；学生跟随步骤示范实时练习，通过摄像头获得逐步核验和纠错反馈，教师查看班级学情。

2026-10-13 新增逐步教学与实时陪练：摄像头核验完成条件、纠错后继续、通过后自动进入下一步。
Windows 启动与 API 配置见 [SOP 陪练使用说明](docs/SOP_TUTOR_README.md)。

线下赛一键环境、演示账号和 P0/P1 交付清单见 [DEMO.md](DEMO.md) 与 [参赛版交付说明](docs/CONTEST_FINAL_README_2026-10-15.md)；答辩材料见 [iCAN 智慧教育答辩 PPT](docs/presentations/工序智传_iCAN智慧教育答辩_2026-10-15_v2.pptx)。

2026-10-15 更新智能教育网页界面：学习总览、课程中心、AI 实训课堂和教师工作室。
电脑端使用与开源来源见 [教育界面说明](docs/EDUCATION_UI_README.md)。

公开视频下载、真实 API 备课和步骤核验结果见 [2026-10-15 实测记录](docs/PUBLIC_VIDEO_TEST_2026-10-15.md)。

适合固定机位的简单折纸杯案例、教师校对与 5 步完整通关结果见 [折纸杯简单案例实测](docs/ORIGAMI_CUP_TEST_2026-10-15.md)。

普通步骤一次多帧核验、模糊画面自动补拍、明确错误纠正和关键步骤双确认策略见 [鞋带穿法多帧核验实测](docs/SHOE_LACING_STRATEGY_TEST_2026-10-15.md)。

## 产品边界

- 面向职业院校实训、公共实训基地和企业岗前培训。
- 首版适合单人、单工位、固定机位、5 至 8 个可观察步骤的安全工序。
- 多模态模型负责提取工具、对象、动作和候选步骤，不直接决定数值分数。
- 本地对齐器和固定规则引擎负责步骤判定与六维评分。
- 关键步骤或总体证据不足时返回“无法判断”，不把漏检冒充操作错误。
- 当前识别效果属于比赛演示级功能验证，不是工业认证或生产级准确率结论。

## 主体流程

```text
教师：标准视频/手册 -> 后台分析 -> SOP 草稿 -> 校对 -> 发布
学生：选择已发布课程 -> 观看当前步骤 -> 摄像头多帧核验 -> 纠错或自动跳步 -> 学习报告
教师：班级学情 -> 共性薄弱步骤 -> 下钻单次报告 -> 调整教学或安排复练
```

主体能力包括：

- 加密资产上传、视频处理、手册切片和限定资产范围的 RAG 检索。
- 可恢复后台任务、分阶段进度、自动重试和刷新后恢复。
- 标准视频分批分析、动态工序词表、SOP 版本化编辑与发布。
- 学员视频一站式提交、全片时间戳、步骤事件合并、确定性评分。
- 登录鉴权的视频预览、步骤时间轴、证据帧和改进建议。
- 评分后可选的大模型教学解释与复练计划；模型不接收总分，也不能修改任何数值结果。
- 管理员账号管理、角色权限、PWA 与三类离线工程回放。

## 技术架构

- 后端：Python 3.12、FastAPI、Pydantic、SQLAlchemy 2、Alembic、SQLite WAL。
- 前端：Vue 3、Vite、Pinia、ECharts、PWA、Lucide。
- 媒体：FFmpeg；上传资产与衍生物使用 AES-256-GCM 加密保存。
- 视觉：默认采用 `qwen` 外部多模态适配器；`none` 只用于可信拒判与工程测试。
- 通信：REST；后台 worker 处理资产、SOP 编译和预录考评任务。

## 快速开始

```bash
cd gongxu-zhichuan
cp .env.example .env
make setup

# 空数据库先创建管理员；密码留空时会安全地交互输入
cd backend
uv run python -m gongxu.cli create-admin --username admin
cd ..

make dev
```

访问 `http://127.0.0.1:5185`。`make dev` 会同时启动 API、worker 和前端；主体功能统一使用 Qwen，配置缺失时明确拒绝启动或返回无法判断。
`make setup` 与 `make dev` 都会先将 SQLite 数据库迁移到当前版本；不会使用模型定义静默改写旧表。

真实 Qwen 模式需要在本机 `.env` 配置以下变量，密钥不得提交 Git：

```dotenv
GONGXU_LLM_MODE=live
GONGXU_LLM_BASE_URL=https://your-provider.example/v1
GONGXU_LLM_API_KEY=your-secret
GONGXU_LLM_MODEL=qwen3.8-max
GONGXU_VISION_BACKEND=qwen
```

未配置视觉模型时，真实视频考评会返回结构化“无法判断”。答辩工程回放仍可用于演示产品交互，并始终标记为 `fixture_replay`，不能作为模型准确率。

Windows 11 开发环境可使用 `windows/Setup.ps1`、`windows/Start.ps1` 和
`windows/Stop.ps1`。启动脚本会同时管理 API、worker 与前端，并强制校验 Qwen live 配置；
当前已通过脚本契约测试，但便携发布包和真实 Windows 11/FFmpeg 验收仍属于后续阶段。

## 验证命令

```bash
make test
make test-e2e
make security
make qwen-probe
make qwen-eval-preflight
make qwen-evaluate
```

公开数据默认只用于许可审计、标签映射和开发基准清单，不用于本项目训练本地模型。
只有官方来源、许可元数据和操作者对本次外部处理的明确确认同时满足时，专用评测器
才允许向外部 Provider 发送必要的压缩关键帧：

```bash
# 下载/复用并核验官方 IndustReal train + val
make data-prepare-development

# 生成不含原始帧的 val 开发基准清单
make data-benchmark

# 发送程序生成的无人物图片，验证 Qwen 的文本、JSON 与视觉能力
make qwen-probe

# 明确确认后，用 IndustReal val 运行 Qwen 开发验证（不读取官方 test）
make qwen-evaluate-industreal INDUSTREAL_QWEN_ACK=YES

# 旧的确定性工程场景仍单独保留，不再冒充训练
make fixture-smoke
```

Playwright 包含一条不 mock 业务 API 的本地真实栈 smoke：使用非真人小视频验证加密上传、SQLite 任务、资产与考评 worker、FFmpeg、可信拒判和历史报告；它强制在 `vision=none` 下运行，不会调用外部 Provider，也不代表真实视觉模型准确率。

更完整的操作步骤见 [USAGE.md](USAGE.md)，当前完成情况见 [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md)。
从零部署（Docker、Windows 11、端口与数据持久化）见 [DEPLOYMENT.md](DEPLOYMENT.md)。
比赛首发工序、现场演示边界与后续视频采集规格见
[docs/CONTEST_DEMO_SCOPE.md](docs/CONTEST_DEMO_SCOPE.md)。

## 数据与许可

- 代码采用 AGPL-3.0。
- IMPACT 等公开数据仅按其原许可用于研究与教育，不在仓库重新分发受限原始数据。
- 真人或学员视频不得作为公开仓库素材；外部 Provider 的压缩关键帧、手册文本和去标识化步骤判定出站必须由上传者明确授权，完整视频、账号信息和总分不出站。
- 主产品不训练或分发自研本地动作模型；Qwen 只分析上传者明确授权外发的压缩关键帧。
- 2026-08-24 已在操作者明确授权下，用官方 Apache-2.0 IndustReal v2 `val` 的 16 段视频完成一次 Qwen 开发验证；它没有访问官方 `test`，也不是本地训练或目标工序冻结测试。结果与限制见 [项目状态](docs/IMPLEMENTATION_STATUS.md)。
- 团队授权视频可使用 [Qwen 授权视频自动评测](docs/QWEN_AUTHORIZED_EVALUATION.md) 完成整批预检、断点执行和指标归档；任何文件哈希或授权声明不合格时均在出站前终止。
