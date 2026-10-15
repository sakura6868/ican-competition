# 工序智传 Makefile
# Windows 用户使用 windows/*.ps1 等价脚本，无需安装 make。

PY := uv run
BACKEND := backend
NODE := frontend

.PHONY: help setup dev test test-e2e security qwen-probe qwen-eval-preflight qwen-evaluate qwen-evaluate-industreal data-discover data-smoke data-impact data-industreal-pilot data-industreal-development data-audit data-prepare data-prepare-development data-benchmark fixture-smoke full-pipeline package-windows

QWEN_EVAL_MANIFEST ?= data/manifests/qwen-evaluation.json

help: ## 列出可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: ## 安装依赖（Python 3.12 + Node 22）
	cd $(BACKEND) && uv sync --all-extras
	cd $(BACKEND) && uv run python -m gongxu.db.migrate
	cd $(NODE) && npm install

dev: ## 启动后端、任务 worker 与前端开发服务
	./scripts/dev.sh

test: ## 后端与前端测试
	cd $(BACKEND) && uv run pytest
	cd $(BACKEND) && uv run mypy gongxu
	cd $(NODE) && npm run test

test-e2e: ## Playwright 主流程（需后端已启动）
	cd $(BACKEND) && uv run python -m gongxu.tests.seed
	cd $(NODE) && npm run test:e2e

security: ## 秘密扫描与安全测试
	cd $(BACKEND) && uv run bandit -r gongxu -x tests
	cd $(BACKEND) && uv run pytest tests/security
	cd $(BACKEND) && uv run pip-audit

qwen-probe: ## 真实探测默认 Qwen 的 JSON 与图像输入能力（不上传真人数据）
	cd $(BACKEND) && uv run python -m gongxu.providers.probe --mode live --out ../reports/generated/qwen-provider-contract.json

qwen-eval-preflight: ## 校验授权视频评测清单（不调用 Qwen）
	cd $(BACKEND) && GONGXU_VISION_BACKEND=qwen uv run python -m gongxu.evaluation.qwen --manifest ../$(QWEN_EVAL_MANIFEST) --preflight-only

qwen-evaluate: ## 对授权视频运行 Qwen 评测并断点生成 JSON/Markdown 报告
	cd $(BACKEND) && GONGXU_VISION_BACKEND=qwen uv run python -m gongxu.evaluation.qwen --manifest ../$(QWEN_EVAL_MANIFEST)

qwen-evaluate-industreal: ## 用官方 IndustReal val 运行 Qwen 开发验证（需显式确认）
	@test "$(INDUSTREAL_QWEN_ACK)" = "YES" || (echo "请显式设置 INDUSTREAL_QWEN_ACK=YES" && exit 2)
	cd $(BACKEND) && GONGXU_VISION_BACKEND=qwen uv run python -m gongxu.evaluation.industreal_qwen --acknowledge-external-provider-processing --run

data-discover: ## 发现 IMPACT 官方数据（阶段3）
	cd $(BACKEND) && uv run python -m gongxu.data.discover

data-smoke: ## IMPACT 官方样本 smoke（阶段3）
	cd $(BACKEND) && uv run python -m gongxu.data.smoke

data-impact: ## 下载 IMPACT 官方样本（阶段3，需先确认数据许可）
	cd $(BACKEND) && uv run python -m gongxu.data.download --include sample

data-industreal-pilot: ## 下载官方 IndustReal train/val 先导集（不访问 test）
	cd $(BACKEND) && uv run python -m gongxu.data.industreal

data-industreal-development: ## 下载官方 IndustReal 全部 train/val（不访问 test）
	cd $(BACKEND) && uv run python -m gongxu.data.industreal --scope development

data-audit: ## 只读审计本地官方数据、许可、标注与可用性
	cd $(BACKEND) && uv run python -m gongxu.data.audit

data-prepare: ## 自动下载/复用、校验并报告 IndustReal 先导集
	cd $(BACKEND) && uv run python -m gongxu.data.prepare

data-prepare-development: ## 自动校验并报告 IndustReal 全量 train/val
	cd $(BACKEND) && uv run python -m gongxu.data.prepare --scope development

data-benchmark: ## 生成 IndustReal val 本地开发基准清单（不调用外部模型）
	cd $(BACKEND) && uv run python -m gongxu.ml.industreal_benchmark --pilot-root ../data/raw/industreal-v2/development


fixture-smoke: ## 运行确定性 fixture 对齐实验（不代表模型准确率）
	cd $(BACKEND) && uv run python -m gongxu.ml.experiment && uv run python -c "from gongxu.ml.model_card import load_experiment, write_model_card; from pathlib import Path; write_model_card(load_experiment(Path('../reports/generated/training-metrics.json')))"

full-pipeline: ## 全流程（阶段12）
	@echo "full-pipeline 在阶段12实现"

package-windows: ## Windows 便携运行包（阶段12）
	@echo "package-windows 在阶段12实现"
