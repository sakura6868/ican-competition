# 工序智传数据集选择与就绪说明

> 状态日期：2026-08-24  
> 适用范围：模型开发、开发验证与后续参赛数据规划  
> 结论口径：本文记录“数据是否可合法、可复现地进入本地开发流程”，不记录模型准确率。

## 一、结论

当前将 **IndustReal v2** 选为首要公开开发数据集，用于验证工序动作、步骤完成、装配状态和错误事件的本地处理流程。官方 `train + val` 开发子集已经完整下载并通过文件大小、SHA256、标注解析和视频解码检查：共 52 段视频，其中 `train` 36 段、`val` 16 段，总时长 11,696.8 秒（约 3.25 小时），包含 5,595 个动作片段、444 个参考完成事件和 463 个含错误版本完成事件，其中 19 个为错误完成事件。

该结果只代表**官方开发数据已就绪**，不代表模型已经训练，也不代表取得任何正式准确率。官方 held-out test 的归档和成员均未访问；`val` 只能用于开发验证和阈值选择，不能包装为独立测试集。

IndustReal 是头戴式第一视角的玩具装配数据，与项目计划采用的普通摄像头固定工位存在明显域差异。因此它适合建立通用基线和检验流水线，不足以单独支持“工业现场准确率”或“跨视角已解决”等参赛声明。最终仍需建立目标演示工序的授权固定机位数据，并冻结人物、视角独立的测试集。

## 二、IndustReal v2 已就绪内容

### 2.1 来源与完整性

- 官方项目：https://github.com/TimSchoonbeek/IndustReal
- 官方 4TU 数据记录：https://data.4tu.nl/datasets/b008dd74-020d-4ea4-a8ba-7bb60769d224/2
- DOI：https://doi.org/10.4121/b008dd74-020d-4ea4-a8ba-7bb60769d224.v2
- 数据许可：Apache-2.0；项目发布包默认不重新分发原始真人视频，是否可外发给第三方模型服务需另行审查。
- 本地根目录：`data/raw/industreal-v2/development/`
- Manifest：`data/raw/industreal-v2/development/manifest.json`
- Manifest Schema：`industreal-development-v2`
- Manifest SHA256：`685ec8dfed2a88173f80fa992e7aa7678cca0d028d0cb2b3c838f520bd07be74`
- Manifest 文件项：470 项；所选内容共 2,809,999,508 字节。
- Manifest 明确记录：`test_archives_accessed=false`、`test_split_member_extracted=false`、`external_provider_export=not_authorized_by_this_manifest`。
- Manifest 固定官方 DOI、归档 URL 和 4TU 发布的整包 MD5；选择性下载不会伪称在本地重新计算了整包 MD5。
- 每个选择性下载成员均记录并核验官方 ZIP 成员大小和 CRC32，同时保留本地 SHA256。CRC32 用于发现传输或存储损坏，不等同于对 manifest 与文件同时被篡改的密码学认证。
- 下载采用 `.part` 检查点：短写可恢复，完整但 CRC32 不匹配的断点会删除并从零重拉；非 seek 流采用重新读取并跳过本地偏移，不宣称远端一定支持 HTTP Range。

### 2.2 已包含模态

每段 recording 包含 RGB 视频以及下列官方标注：

- 动作识别标签 `AR_labels.csv`
- 工序状态标签 `PSR_labels.csv`
- 含错误工序状态标签 `PSR_labels_with_errors.csv`
- 原始组件状态 `PSR_labels_raw.csv`
- 物体检测标签 `OD_labels.json`
- 手部跟踪 `hands.csv`
- 注视跟踪 `gaze.csv`
- 头部姿态 `pose.csv`

为控制体积，当前没有下载 RGB 帧目录、双目图像、深度图和环境光图。视频中的官方帧编号按 10 Hz 对齐，必要帧应从 MP4 本地解码，不应把标注帧编号当作连续推理结果。

### 2.3 开发验证清单

已从官方 `val` 生成本地开发基准清单：16 段 recording、5 名参与者、148 个事件窗口，其中 143 个正确事件、5 个错误事件。该清单只含事件、哈希与时间窗口，不含原始帧，也没有执行外部 Provider 调用。

以下数字不能被解读为模型预测结果：

- `143 / 5` 是**真值分布**，不是模型判断正确/错误的数量。
- `external_provider_calls=0`，尚无真实 Provider 在此数据上的准确率、拒判率、延迟或费用指标。
- `formal_accuracy_claim_allowed=false`，尚未形成可用于参赛声明的冻结测试结果。

### 2.4 类别不平衡

按 `AR_labels.csv` 的动作标签统计：

| Split | Recording | 动作片段 | 动作类别 | 出现不超过 5 次的类别 |
|---|---:|---:|---:|---:|
| `train` | 36 | 3,667 | 72 | 17 |
| `val` | 16 | 1,928 | 65 | 16 |

训练集最高频类别 `check_instruction` 有 404 个片段，而 `fit_partial_model`、`loosen_acorn_nut`、`loosen_tooth_washer`、`take_small_screw_pin`、`tighten_tooth_washer` 等类别各只有 1 个片段。只报告总体 accuracy 会掩盖长尾类别失败，后续必须同时报告 macro-F1、每类召回率、混淆矩阵和拒判率。

## 三、其他本地候选数据的边界

| 数据集 | 当前本地状态 | 可用方向 | 当前不能做的声明 |
|---|---|---|---|
| IMPACT v1.1 | 官方标注和 quick-start media 已存在；许可为 CC BY-NC-SA 4.0 | 多视角工业操作数据适配、解析与工程联调 | quick-start 不是正式 metric split，不能据此宣称工业模型准确率 |
| Assembly101 | 官方 mistake-detection 标注已存在，官方完整视频需要接受访问条款；本地没有可配对的官方媒体 | 错误、纠正和时序关系 Schema 设计 | 不能用 annotation-only 计算视频模型指标；来源不明的第三方 sample 已排除 |
| CaptainCook4D | 官方标注已存在，本地没有媒体；领域为烹饪 | 缺步、顺序、技术和测量错误的辅助分类研究 | 不能把烹饪域结果写成工业装配准确率 |

选择顺序如下：

1. IndustReal v2 用于当前本地开发和开发验证。
2. IMPACT quick-start 仅用于数据适配与多视角工程联调；取得正式 split 的完整授权媒体后再讨论指标。
3. Assembly101 在完成官方访问条款、获得匹配媒体并确认非商业用途前保持 annotation-only。
4. CaptainCook4D 只作流程错误分类的辅助参考，不进入首版工业效果主指标。
5. 用户本地新增数据必须先验证官方来源、许可、人物同意、标签对应关系和文件哈希；不因“文件能播放”就进入训练或评测。

### 3.1 本机其他数据扫描结论

已只读扫描本机 `Desktop`、`Downloads`、`Documents` 和相邻项目中的常见视频、压缩包与标注文件。相邻 SafeHome 项目里的 UP-Fall 数据和跌倒录像属于居家跌倒检测域，与工业装配步骤、工具、零件和错误操作没有标签对应关系，因此不迁移到本项目。其他个人录屏、旧项目演示视频和无法证明来源的 Assembly101 sample 也全部排除；没有因为“本地已有”而自动纳入训练。

## 四、隐私和外部 Provider 门槛

当前任何包含真人的公开数据帧都**不得发送到未审查的外部多模态 Provider**。数据许可允许研究使用，不自动等于参与者同意被第三方商业 API 留存、转处理或跨境处理。

启用外发前必须同时满足：

1. 数据条款明确允许目标用途和第三方处理，或已取得补充授权。
2. Provider 的正式主体、地区、留存周期、是否用于训练、子处理方和删除机制已经记录。
3. 使用官方端点而非来源不明的转发代理；API Key 只保存在后端密钥环境。
4. 只外发完成任务所需的最小关键帧，并记录资产哈希、时间戳、调用目的和删除策略。
5. 未满足条件时，只运行不含原始帧的清单生成、标注解析和离线工程测试，不向 Qwen 发送真人帧。

## 五、可复现命令

从项目根目录执行：

```bash
# 下载或复用官方 IndustReal 全量 train + val；代码禁止访问 test
make data-industreal-development

# 离线复核全部文件大小、SHA256、标注和视频可解码性
make data-prepare-development

# 重新生成本地数据来源、许可与可用性审计
make data-audit

# 生成 val 开发基准事件清单；不解出原始帧、不调用 Provider
make data-benchmark
```

关键输出：

- `reports/generated/industreal-development-readiness.json`
- `reports/generated/industreal-development-readiness.md`
- `reports/generated/industreal-benchmark.json`
- `reports/generated/industreal-benchmark.md`
- `reports/generated/dataset-readiness.json`
- `reports/generated/dataset-readiness.md`

重跑时应核对 manifest SHA256 与上述归档值一致。广义数据审计会同时扫描 pilot 和 development 目录，因此其中的 56 个 IndustReal 视频包含 4 个重复 pilot；正式开发集精确数量应以 development manifest 和 readiness report 的 52 段为准。

## 六、下一步 Qwen 评测建议

### 6.1 先冻结任务，不训练本地“大而全”分类器

建议按三层任务推进：

1. **动作片段识别**：预测受控动作词表，用于验证局部视觉事实。
2. **步骤完成与错误事件检测**：以 PSR 和带错误 PSR 为监督，输出事件时间、状态和证据充足度。
3. **SOP 对齐与评分**：由确定性状态机消费识别事件；大模型只生成文字反馈，不修改数值分数。

公开数据标签只有通过显式 `label -> SOP step` 映射后才能进入产品评分。看不清、标签超出受控词表或关键证据缺失时必须返回 `insufficient_evidence`，不能靠大模型猜测。

### 6.2 外部 Qwen 开发验证顺序

1. Qwen 为外接模型，不在本项目内训练；只在开发集选择 prompt、受控词表、抽帧策略和拒判门槛。
2. 只有团队自有或明确允许第三方处理的媒体才能进入 Qwen 评测，公开真人数据不因许可证开放而自动取得外发授权。
3. 所有方法使用同一事件窗口和冻结划分；不能通过删除稀有失败类别提高表面指标。
4. 评测至少包含 macro-F1、micro-F1、每类 precision/recall、步骤事件级 precision/recall/F1、错误事件召回率、误报/小时、时间偏差、拒判率、推理延迟和 token 用量。
5. 逐 recording 保存预测、真值、证据时间戳、模型哈希和配置哈希；报告数字只能从同一归档结果生成。
6. 官方 test 继续冻结。若未来合法获得 test 的评测入口，只在模型、阈值和映射全部冻结后运行一次，不将结果反向用于调参。

### 6.3 补齐参赛目标域

IndustReal 不能替代项目自己的固定工位数据。下一阶段应选定一个 5 至 8 步、安全、可重复的桌面工序，录制不同操作者、固定机位和至少一种变化视角，并覆盖：完整操作、漏步、错序、错误工具/零件、遮挡和无法判断。人物和机位应隔离划分，训练/验证集完成调优后再冻结独立测试集。

### 6.4 当前真实 Provider 状态

默认 `qwen3.8-max` 已通过不含真人的文本、JSON 和合成图片能力探测，但尚未在带人工真值的目标工序视频集上评测。因此当前只能声明 Provider 接口可用，不能声明步骤识别准确率。后续应使用团队自有授权视频，按开发集调 prompt，再对冻结测试集运行一次并归档逐样本证据。

## 七、当前可对外表述

可以表述：

> 已接入并校验官方 IndustReal v2 的 52 段 train/val 开发视频与多模态标注，建立了不访问 held-out test、不外发真人帧、可按 manifest SHA256 复现的数据准备和开发基准流程。

暂时不能表述：

> Qwen 已在 IndustReal 真人视频上完成评测；模型准确率达到某个数值；系统已经通过独立工业测试。
