#!/usr/bin/env bash
# 网易云登录（Hermes / 人工均可）
#   qr-init  生成二维码后立即退出（适合 agent 把图发给用户）
#   qr-poll  轮询已生成的 key，成功则写 cookie
#   qr       前台一体化扫码（交互终端）
#   status   检查 cookie
#   cookie   手动写入
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy

# shellcheck disable=SC1091
[[ -f "$ROOT/config.env" ]] && source "$ROOT/config.env"

NCM_API_BASE="${NCM_API_BASE:-http://127.0.0.1:3000}"
COOKIE_FILE="${COOKIE_FILE:-$ROOT/data/cookie.txt}"
DATA_DIR="${DATA_DIR:-$ROOT/data}"
mkdir -p "$DATA_DIR"
CURL=(curl -fsS --noproxy '*')
KEY_FILE="$DATA_DIR/login-key.txt"
QR_PNG="$DATA_DIR/login-qr.png"
QR_URL_FILE="$DATA_DIR/login-qrurl.txt"

need_api() {
  if ! "${CURL[@]}" --max-time 3 "$NCM_API_BASE/banner?type=0" >/dev/null 2>&1; then
    echo "NCM API 未就绪: $NCM_API_BASE" >&2
    echo "请先: cd $ROOT && docker compose up -d" >&2
    exit 1
  fi
}

save_cookie() {
  local cookie="$1"
  cookie="$(echo "$cookie" | tr '\n' ';' | sed 's/;;*/;/g;s/;$//')"
  printf '%s\n' "$cookie" >"$COOKIE_FILE"
  chmod 600 "$COOKIE_FILE"
  echo "Cookie 已写入: $COOKIE_FILE"
}

check_status() {
  local cookie
  cookie="$(tr -d '\n\r' <"$COOKIE_FILE")"
  "${CURL[@]}" --get "$NCM_API_BASE/login/status" \
    --data-urlencode "cookie=${cookie}" \
    --data-urlencode "timestamp=$(date +%s)"
}

cmd="${1:-}"

case "$cmd" in
  qr-init)
    need_api
    key_json="$("${CURL[@]}" "$NCM_API_BASE/login/qr/key?timestamp=$(date +%s)")"
    key="$(echo "$key_json" | jq -r '.data.unikey // empty')"
    [[ -n "$key" ]] || { echo "获取 key 失败: $key_json" >&2; exit 1; }
    printf '%s\n' "$key" >"$KEY_FILE"
    create_json="$("${CURL[@]}" "$NCM_API_BASE/login/qr/create?key=${key}&qrimg=true&timestamp=$(date +%s)")"
    qrurl="$(echo "$create_json" | jq -r '.data.qrurl // empty')"
    qrimg="$(echo "$create_json" | jq -r '.data.qrimg // empty')"
    printf '%s\n' "$qrurl" >"$QR_URL_FILE"
    if [[ -n "$qrimg" ]]; then
      b64="${qrimg#*,}"
      echo "$b64" | base64 -d >"$QR_PNG" 2>/dev/null || true
    fi
    if command -v qrencode >/dev/null 2>&1 && [[ -n "$qrurl" ]]; then
      qrencode -t PNG -o "$QR_PNG" "$qrurl" 2>/dev/null || true
      qrencode -t ANSIUTF8 "$qrurl" 2>/dev/null || true
    fi
    echo "QR_INIT_OK"
    echo "key_file=$KEY_FILE"
    echo "qr_png=$QR_PNG"
    echo "qr_url=$qrurl"
    echo "请用网易云 App 扫码；扫码后执行: $0 qr-poll"
    ;;

  qr-poll)
    need_api
    if [[ ! -f "$KEY_FILE" ]]; then
      echo "无 login key，先运行: $0 qr-init" >&2
      exit 1
    fi
    key="$(tr -d '\n\r' <"$KEY_FILE")"
    max="${2:-90}"
    for i in $(seq 1 "$max"); do
      check="$("${CURL[@]}" -D "$DATA_DIR/login-headers.txt" \
        "$NCM_API_BASE/login/qr/check?key=${key}&timestamp=$(date +%s)")"
      code="$(echo "$check" | jq -r '.code // empty')"
      msg="$(echo "$check" | jq -r '.message // empty')"
      case "$code" in
        800)
          echo "QR_EXPIRED: 二维码过期，请重新 qr-init" >&2
          exit 1
          ;;
        801)
          printf '[%s/%s] waiting_scan\n' "$i" "$max"
          ;;
        802)
          printf '[%s/%s] scanned_confirm_on_phone\n' "$i" "$max"
          ;;
        803)
          cookie="$(echo "$check" | jq -r '.cookie // empty')"
          if [[ -z "$cookie" || "$cookie" == "null" ]]; then
            cookie="$(grep -i '^set-cookie:' "$DATA_DIR/login-headers.txt" 2>/dev/null \
              | sed 's/[Ss]et-[Cc]ookie: //;s/\r//' \
              | awk -F';' '{print $1}' \
              | paste -sd';' -)"
          fi
          [[ -n "$cookie" ]] || { echo "LOGIN_FAIL no cookie: $check" >&2; exit 1; }
          save_cookie "$cookie"
          echo "LOGIN_OK"
          check_status | jq '{code: .data.code, profile: .data.profile.nickname}' 2>/dev/null || true
          exit 0
          ;;
        *)
          printf '[%s/%s] code=%s %s\n' "$i" "$max" "$code" "$msg"
          ;;
      esac
      sleep 2
    done
    echo "QR_TIMEOUT" >&2
    exit 1
    ;;

  qr)
    # 一体化
    "$0" qr-init
    "$0" qr-poll 90
    ;;

  status)
    need_api
    if [[ ! -f "$COOKIE_FILE" ]]; then
      echo "无 cookie: $COOKIE_FILE" >&2
      exit 1
    fi
    check_status | jq .
    ;;

  cookie)
    need_api
    if [[ -z "${2:-}" ]]; then
      echo "用法: $0 cookie 'MUSIC_U=...; __csrf=...'" >&2
      exit 1
    fi
    save_cookie "$2"
    check_status | jq .
    ;;

  *)
    echo "用法:"
    echo "  $0 qr-init          # 生成二维码（Hermes 用）"
    echo "  $0 qr-poll [次数]   # 轮询扫码结果"
    echo "  $0 qr               # 终端一体化登录"
    echo "  $0 status"
    echo "  $0 cookie '...'"
    exit 1
    ;;
esac
