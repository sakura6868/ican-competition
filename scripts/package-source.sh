#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%F)"
DESTINATION="${1:-$ROOT_DIR/../工序智传_可部署源码包_${STAMP}}"

if [[ -e "$DESTINATION" ]]; then
  echo "目标已存在，拒绝覆盖：$DESTINATION" >&2
  exit 2
fi

mkdir -p "$DESTINATION"
rsync -a \
  --include="/.env.example" \
  --include="/deploy/.env.example" \
  --exclude="/.env*" \
  --exclude="/deploy/.env*" \
  --exclude="**/.env*" \
  --exclude-from="$ROOT_DIR/.releaseignore" \
  "$ROOT_DIR/" "$DESTINATION/"

echo "已创建可部署源码包：$DESTINATION"
echo "包含源码、文档、Docker/Windows 启动脚本与环境变量模板；不包含密钥、数据库、视频、数据集、模型权重和本机依赖。"
