# 阶段 1 验收清单：独立基线

对照交接文档 §17 与 §15。逐项勾选并记录证据。

| # | 项 | 证据 | 通过 |
|---|---|---|---|
| 1 | 独立兄弟仓库 `gongxu-zhichuan`，不在 SafeHome 内改造 | 仓库根目录存在，SafeHome 未被修改 |  |
| 2 | Git 初始化 `main` 分支，真实提交历史 | `git log` |  |
| 3 | AGPL-3.0 `LICENSE` + README 许可声明 | LICENSE 文本完整 |  |
| 4 | Python 固定 3.12（`.python-version` / pyproject `requires-python`） | `.python-version` = 3.12.x |  |
| 5 | Node 固定 22（`.nvmrc`） | `.nvmrc` = 22 |  |
| 6 | `.gitignore` 排除原始数据、权重、密钥、媒体、数据库 | 文件存在，`git status` 无泄漏 |  |
| 7 | CI：后端 lint + test + 秘密扫描 + 前端 build | `.github/workflows/ci.yml` |  |
| 8 | 开发命令 `make setup/dev/test/security` 等 | Makefile |  |
| 9 | `docs/IMPLEMENTATION_STATUS.md` 建立 | 文件存在，含里程碑记录 |  |
| 10 | `docs/lineage.md` 建立并记录 SafeHome 来源状态 | 文件存在 |  |
| 11 | 最小 FastAPI/SQLite 工程可启动，`/api/v1/health` 返回 ok | 测试通过 |  |
| 12 | 秘密扫描无高危问题 | bandit / gitleaks 结果 |  |
| 13 | 首个提交 `chore: establish independent gongxu zhichuan baseline` | commit hash |  |

## 执行记录

- 2026-08-15：创建仓库、写 README/LICENSE/.gitignore/pyproject/Makefile/CI/状态/lineage/验收清单。
- 2026-08-15：记录 SafeHome 保护状态（分支 `codex/publish-runtime-tcn-weight`，HEAD `8c5a0c0`）。
