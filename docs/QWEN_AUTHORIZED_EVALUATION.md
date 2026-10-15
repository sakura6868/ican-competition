# Qwen 授权视频自动评测

本工具把一组团队自有、已明确同意第三方多模态处理的视频，依次送入产品的
正式“视频加密登记 -> Qwen 关键帧分析 -> 步骤对齐 -> 确定性评分 -> 报告”
流水线。它不训练本地模型，也不会未经专用许可检查和操作者确认自动把公开数据集
送给外部 API。

## 1. 使用边界

- 只使用团队自己录制或权利人明确授权 Qwen 处理的视频。
- `IMPACT`、`IndustReal`、`Assembly101` 等公开数据的原许可，不自动构成任意第三方
  API 处理授权。通用清单不得引用这些视频；专用公开数据评测器还必须固定官方来源、
  许可标识，并要求操作者对该次 Provider 处理作出明确确认。
- Qwen 只接收产品抽取的压缩关键帧，不接收完整视频、账号、总分或本机路径。
- 真值不能由同一个待评测模型生成。开发验证和冻结测试都必须使用事先确定的
  预期结果与逐步骤标签。
- `frozen_test` 只能运行一次用于最终报告，结果不得反向修改提示词、SOP、模型选择
  或评分规则。

## 2. 准备清单

在 `data/manifests/qwen-evaluation.json` 建立本机清单。真人视频与清单不应提交 Git。
相对视频路径以清单所在目录为基准。

```json
{
  "schema_version": "qwen-authorized-evaluation-v1",
  "dataset_id": "filter-replacement-dev-v1",
  "evaluation_role": "development_validation",
  "provider_export_authorized": true,
  "authorization_scope": "team_owned_explicit_provider_processing",
  "authorization_note": "团队原创录制，参与者已同意本次 Qwen 关键帧处理",
  "authorized_by_username": "admin",
  "process_key": "filter_replacement",
  "sop_version_id": "数据库中的已发布 SOP ID",
  "threshold_tuning_prohibited": true,
  "cases": [
    {
      "case_id": "correct-front-01",
      "video_path": "../evaluation-videos/correct-front-01.mp4",
      "sha256": "64位小写SHA256",
      "authorization_note": "团队成员自愿录制并授权本次 API 评测",
      "expected_result": "pass",
      "expected_steps": [
        {"step_index": 1, "judgement": "passed"},
        {"step_index": 2, "judgement": "passed"}
      ]
    }
  ]
}
```

清单必须覆盖已发布 SOP 的全部步骤。步骤标签可取：
`passed`、`failed`、`unobservable`、`insufficient_evidence`。

冻结测试还必须填写 ISO 8601 时间，例如：

```json
{
  "evaluation_role": "frozen_test",
  "frozen_at": "2026-09-01T12:00:00+08:00",
  "threshold_tuning_prohibited": true
}
```

## 3. 运行

先执行无网络预检。它会检查 Qwen 配置、授权角色、已发布 SOP、全部视频文件、
逐文件 SHA256 和完整步骤真值；任一项失败时整批终止，Provider 调用为零。

```bash
make qwen-eval-preflight
```

预检通过后执行正式评测：

```bash
make qwen-evaluate
```

使用其他清单：

```bash
make qwen-evaluate QWEN_EVAL_MANIFEST=data/manifests/filter-v2.json
```

IndustReal v2 使用单独的许可与标签适配器，只读取 `train` 或 `val`，不会读取官方
`test`。下列确认只适用于当前本机运行，不构成以后运行的永久授权：

```bash
make qwen-evaluate-industreal INDUSTREAL_QWEN_ACK=YES
```

Windows 11 PowerShell 使用同一评测器：

```powershell
# 先做零出站预检
powershell -ExecutionPolicy Bypass -File .\windows\Evaluate-Qwen.ps1 -PreflightOnly

# 预检通过后正式运行
powershell -ExecutionPolicy Bypass -File .\windows\Evaluate-Qwen.ps1
```

调试时可只运行前两个案例：

```bash
cd backend
uv run python -m gongxu.evaluation.qwen \
  --manifest ../data/manifests/qwen-evaluation.json \
  --max-cases 2
```

IndustReal 开发集可按官方 recording id 精确选样；`--case-id` 可重复传入，未匹配
当前任务类型的 id 会被忽略，不会为了找到目标样本重跑前面的所有视频：

```bash
cd backend
GONGXU_VISION_BACKEND=qwen uv run python -m gongxu.evaluation.industreal_qwen \
  --acknowledge-external-provider-processing \
  --run --split train \
  --case-id 01_assy_0_1 \
  --case-id 15_main_3_1
```

该入口只用于开发验证。不得用官方 `test` 选样、调提示词、调时间槽或调整评分门槛。

## 4. 断点与报告

每个案例完成后都会原子更新 JSON 检查点。相同清单 SHA256、Qwen 模型和 SOP 再次
运行时，已记录案例不会重复调用 API。清单、模型或 SOP 变化时拒绝混用旧检查点。

默认输出：

- `reports/generated/qwen-evaluation-<dataset_id>.json`
- `reports/generated/qwen-evaluation-<dataset_id>.md`

报告包含通过/未通过准确率、三状态总体匹配率、步骤分类混淆矩阵、步骤宏 F1、拒判率、执行失败数、延迟
p50/p95、Qwen 调用与失败次数、token 总量以及逐案例结果。费用在没有明确价格表时
固定标记为 `not_configured`，不会猜测成本。

只有 `pass` / `fail` 真值进入主要结果准确率，只有 `passed` / `failed` 步骤进入主要
步骤宏 F1。全是证据不足的合成 smoke 因此会显示主要准确率为 `null`，避免把一次
正确拒判包装成“100% 工序识别准确率”；三状态匹配率和全标签宏 F1 仍单独保留。

`execution_failure` 与 `inconclusive` 严格分开：前者是工程运行失败，后者是系统基于
证据不足作出的可信拒判。任何评测数字都只适用于该清单、SOP、模型和机位条件，
不代表工业安全认证。

## 5. 当前开发验证结论（2026-08-24）

- 当前正式视觉契约为 `prompt-v11 / parser-v6 / sampling-v13`，评分为
  `scoring-v5`，评测为 `evaluation-v19`；外部模型为 `qwen3.8-max`，没有训练
  本地动作模型。
- `completion_checks` 已与 `observable_checks`（安全规范）分离：动作被看见不等于
  工件最终状态正确，也不等于安全规范已满足。只有前后两个步骤都有明确视觉状态时，
  本地规则才会给出 `wrong_order`，防止采样时间点制造假错序。
- 正常装配开发样本 `01_assy_0_1` 在 v19 中 9/9 步状态匹配、步骤 F1 `1.0`、21 次
  Provider 调用均成功；多个完成状态仍不够清楚，因此整单如实返回
  `inconclusive`，不是“通过准确率 100%”。耗时 435.099 秒、50,928 tokens。
- 官方错误开发样本 `15_main_3_1` 的真值是第 7 步错误。v19 仍把第 7 步判为
  `passed`，并把正常第 5 步误判为 `failed`；顶层 `fail` 只是偶然命中，错误类召回
  仍为 `0`。该单步骤宏 F1 `0.4286`、20 次调用均成功、耗时 345.336 秒、47,718
  tokens，不能作为错误检出成功或工业考评能力的声明。
- 历史 v4 全量 `val` 报告的 SOP 步骤顺序后来被确认错误，结果级和步骤级指标均已
  失效，只能作为工程运行记录，不得用于答辩 Claims Sheet 或模型效果比较。
