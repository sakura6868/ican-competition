# 鞋带穿法公开视频测试样本

此目录保存用于通用 SOP 编译与实时多帧核验测试的公开视频、许可信息和可复现实验结果。

- `shoe-lacing.webm`：原始公开视频。
- `source.json`、`ATTRIBUTION.txt`：来源、许可和文件哈希。
- `contact-sheet.jpg`、`fine-contact.jpg`：人工校对用时间轴缩略图。
- `timeline/`、`fine/`、`observations/`：抽帧及核验输入。
- `results/generated-sop.json`：模型生成的原始 SOP 草稿。
- `results/reviewed-sop-v3.json`：教师校对并发布的 4 步 SOP。
- `results/practice-results.json`：最终普通步骤多帧核验回归结果。
- `results/practice-results-v1.json`、`practice-results-v2.json`：条件纠错与教师标准审计记录。

测试使用同一教学视频回放，不代表独立学员或真实摄像头条件下的识别准确率。完整说明见 [`docs/SHOE_LACING_STRATEGY_TEST_2026-10-15.md`](../../../docs/SHOE_LACING_STRATEGY_TEST_2026-10-15.md)。

