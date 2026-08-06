#!/usr/bin/env bash
# Entry: Apple Music favorites/recent → NetEase like/scrobble
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

export PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy no_proxy NO_PROXY

# shellcheck disable=SC1091
if [[ -f "$ROOT/config.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/config.env"
  set +a
fi

resolve_path() {
  local p="$1"
  if [[ -z "$p" ]]; then
    echo ""
    return
  fi
  if [[ "$p" = /* ]]; then
    echo "$p"
  else
    echo "$ROOT/${p#./}"
  fi
}

export COOKIE_FILE="$(resolve_path "${COOKIE_FILE:-./data/cookie.txt}")"
export LOG_DIR="$(resolve_path "${LOG_DIR:-./logs}")"
export DATA_DIR="$(resolve_path "${DATA_DIR:-./data}")"
if [[ -n "${AM_CONFIG:-}" ]]; then
  export AM_CONFIG="$(resolve_path "$AM_CONFIG")"
fi
if [[ -n "${FEEDBACK_STATE_FILE:-}" ]]; then
  export FEEDBACK_STATE_FILE="$(resolve_path "$FEEDBACK_STATE_FILE")"
fi

mkdir -p "$LOG_DIR" "$DATA_DIR"
LOCK="$DATA_DIR/feedback.lock"

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    echo "[$(date -Iseconds)] feedback already running" >>"$LOG_DIR/cron.log"
    exit 0
  fi
fi

echo "[$(date -Iseconds)] feedback start $*" >>"$LOG_DIR/cron.log"

NCM_API_BASE="${NCM_API_BASE:-http://127.0.0.1:3000}"
if ! curl -fsS --noproxy '*' --max-time 3 "${NCM_API_BASE}/banner?type=0" >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1; then
    docker compose -f "$ROOT/docker-compose.yml" up -d >>"$LOG_DIR/cron.log" 2>&1 || true
    sleep 3
  fi
fi

PYTHON="${PYTHON:-python3}"
# shellcheck disable=SC2086
"$PYTHON" "$ROOT/apple_to_netease_feedback.py" "$@"
rc=$?
echo "[$(date -Iseconds)] feedback finished rc=$rc" >>"$LOG_DIR/cron.log"
exit "$rc"
