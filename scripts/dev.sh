#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime/dev"
mkdir -p "$RUNTIME_DIR"

children=()

cleanup() {
  local child
  for child in "${children[@]:-}"; do
    if kill -0 "$child" 2>/dev/null; then
      kill "$child" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}

trap cleanup EXIT INT TERM

if curl --fail --silent http://127.0.0.1:8011/api/v1/health >/dev/null 2>&1; then
  echo "[dev] port 8011 already serves a Gongxu API; stop the existing stack before make dev" >&2
  exit 1
fi
if curl --fail --silent http://127.0.0.1:5185 >/dev/null 2>&1; then
  echo "[dev] port 5185 is already in use; stop the existing UI before make dev" >&2
  exit 1
fi

echo "[dev] applying database migrations"
(
  cd "$ROOT_DIR/backend"
  uv run python -m gongxu.db.migrate
)

settings_probe="$({
  cd "$ROOT_DIR/backend"
  uv run python -c 'from gongxu.config import get_settings; s=get_settings(); print(f"{s.llm_mode}|{int(bool(s.llm_base_url and s.llm_api_key and s.llm_model))}|{s.vision_backend}")'
})"
IFS='|' read -r llm_mode llm_configured configured_vision <<<"$settings_probe"

if [[ -z "${GONGXU_VISION_BACKEND:-}" && "$configured_vision" == "none" && "$llm_mode" == "live" && "$llm_configured" == "1" ]]; then
  export GONGXU_VISION_BACKEND=qwen
  echo "[dev] vision backend: qwen (auto-selected from configured live Provider)"
else
  export GONGXU_VISION_BACKEND="${GONGXU_VISION_BACKEND:-$configured_vision}"
  echo "[dev] vision backend: $GONGXU_VISION_BACKEND"
fi

if [[ "$GONGXU_VISION_BACKEND" == "qwen" && "$llm_configured" != "1" ]]; then
  echo "[dev] qwen requires GONGXU_LLM_BASE_URL/API_KEY/MODEL; refusing a silent no-model start" >&2
  exit 1
fi

if [[ "$GONGXU_VISION_BACKEND" == "none" ]]; then
  echo "[dev] note: real video assessment will return 'inconclusive'; offline contest replays remain available"
fi

echo "[dev] starting API on http://127.0.0.1:8011"
(
  cd "$ROOT_DIR/backend"
  exec uv run uvicorn gongxu.main:app --reload --host 127.0.0.1 --port 8011
) >"$RUNTIME_DIR/backend.log" 2>&1 &
children+=("$!")

echo "[dev] starting background worker (vision=${GONGXU_VISION_BACKEND:-from-.env})"
(
  cd "$ROOT_DIR/backend"
  exec uv run python -m gongxu.worker
) >"$RUNTIME_DIR/worker.log" 2>&1 &
children+=("$!")

echo "[dev] waiting for API health check"
for _ in $(seq 1 40); do
  if curl --fail --silent http://127.0.0.1:8011/api/v1/health >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "${children[0]}" 2>/dev/null; then
    echo "[dev] API failed to start; see $RUNTIME_DIR/backend.log" >&2
    exit 1
  fi
  sleep 0.25
done

if ! curl --fail --silent http://127.0.0.1:8011/api/v1/health >/dev/null 2>&1; then
  echo "[dev] API health check timed out; see $RUNTIME_DIR/backend.log" >&2
  exit 1
fi

echo "[dev] starting UI on http://127.0.0.1:5185"
echo "[dev] logs: $RUNTIME_DIR/backend.log and $RUNTIME_DIR/worker.log"
cd "$ROOT_DIR/frontend"
npm run dev -- --host 127.0.0.1
