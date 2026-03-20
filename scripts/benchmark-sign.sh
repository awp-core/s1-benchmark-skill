#!/usr/bin/env bash
# benchmark-sign.sh — Sign and execute a Benchmark API request via AWP Wallet.
#
# Usage: benchmark-sign.sh METHOD PATH [BODY]
#
# Environment:
#   BENCHMARK_API_URL  — Server URL (default: https://tapis1.awp.sh)
#   AWP_WALLET         — awp-wallet 二进制路径（自动检测）
#   WALLET_ADDRESS     — Cached wallet address (auto-detected if unset)
#   AWP_SESSION_TOKEN  — Cached session token (auto-unlocked if unset)
#   WALLET_PASSWORD    — Required for unlock (managed by OpenClaw secret store)
#
# Examples:
#   benchmark-sign.sh GET  /api/v1/poll
#   benchmark-sign.sh POST /api/v1/questions '{"bs_id":"bs_math",...}'

set -euo pipefail

# 自动检测 awp-wallet 路径（OpenClaw 的 npm link 可能装在不同位置）
if [ -z "${AWP_WALLET:-}" ]; then
  _AWP_BIN="awp-wallet"
  for candidate in \
    "/usr/bin/$_AWP_BIN" \
    "/usr/local/bin/$_AWP_BIN" \
    "$HOME/.local/bin/$_AWP_BIN" \
    "$HOME/.awp/bin/$_AWP_BIN" \
    "$(command -v "$_AWP_BIN" 2>/dev/null)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      AWP_WALLET="$candidate"
      break
    fi
  done
fi
# 最后尝试 node 直接运行
if [ -z "${AWP_WALLET:-}" ]; then
  _NODE_ENTRY="/usr/lib/node_modules/awp-wallet/scripts/wallet-cli.js"
  if [ -f "$_NODE_ENTRY" ]; then
    AWP_WALLET="node $_NODE_ENTRY"
  fi
fi
if [ -z "${AWP_WALLET:-}" ]; then
  echo '{"error":"awp-wallet not found. Install it first."}' >&2
  exit 1
fi

METHOD="${1:?Usage: benchmark-sign.sh METHOD PATH [BODY]}"
API_PATH="${2:?Usage: benchmark-sign.sh METHOD PATH [BODY]}"
BODY="${3:-}"

BENCHMARK_API_URL="${BENCHMARK_API_URL:-https://tapis1.awp.sh}"

# Auto-unlock wallet if no session token cached
# unlock 需要 WALLET_PASSWORD（由 OpenClaw secret store 注入）
if [ -z "${AWP_SESSION_TOKEN:-}" ]; then
  UNLOCK_OUT=$($AWP_WALLET unlock --duration 3600 2>/dev/null) || UNLOCK_OUT=""
  AWP_SESSION_TOKEN=$(echo "$UNLOCK_OUT" | jq -r '.sessionToken // empty' 2>/dev/null)
  # 兼容旧版输出格式
  if [ -z "$AWP_SESSION_TOKEN" ]; then
    AWP_SESSION_TOKEN=$(echo "$UNLOCK_OUT" | grep -o '"sessionToken":"[^"]*"' | head -1 | cut -d'"' -f4)
  fi
  export AWP_SESSION_TOKEN
fi

# Auto-detect wallet address if not cached
if [ -z "${WALLET_ADDRESS:-}" ]; then
  RECV_OUT=$($AWP_WALLET receive 2>/dev/null) || RECV_OUT=""
  WALLET_ADDRESS=$(echo "$RECV_OUT" | jq -r '.address // empty' 2>/dev/null)
  # 兼容：如果 jq 解析失败，回退到 grep
  if [ -z "$WALLET_ADDRESS" ]; then
    WALLET_ADDRESS=$(echo "$RECV_OUT" | grep -oi '0x[0-9a-fA-F]\{40\}' | head -1)
  fi
  export WALLET_ADDRESS
fi

TIMESTAMP=$(date +%s)
BODY_HASH=$(printf '%s' "$BODY" | sha256sum | cut -d' ' -f1)
MESSAGE="${METHOD}${API_PATH}${TIMESTAMP}${BODY_HASH}"

# Sign via AWP Wallet (EIP-191 personal_sign)
SIGN_RESULT=$($AWP_WALLET sign-message \
  --token "$AWP_SESSION_TOKEN" --message "$MESSAGE" 2>/dev/null)
SIGNATURE=$(echo "$SIGN_RESULT" | jq -r '.signature // empty' 2>/dev/null)
# 兼容：如果 jq 解析失败，回退到 grep
if [ -z "$SIGNATURE" ]; then
  SIGNATURE=$(echo "$SIGN_RESULT" | grep -o '"signature":"[^"]*"' | head -1 | cut -d'"' -f4)
fi
[ -z "$SIGNATURE" ] && SIGNATURE="$SIGN_RESULT"

if [ -n "$BODY" ]; then
  curl -s -X "$METHOD" \
    -H "Content-Type: application/json" \
    -H "X-Worker-Address: $WALLET_ADDRESS" \
    -H "X-Signature: $SIGNATURE" \
    -H "X-Timestamp: $TIMESTAMP" \
    -d "$BODY" \
    "${BENCHMARK_API_URL}${API_PATH}"
else
  curl -s -X "$METHOD" \
    -H "X-Worker-Address: $WALLET_ADDRESS" \
    -H "X-Signature: $SIGNATURE" \
    -H "X-Timestamp: $TIMESTAMP" \
    "${BENCHMARK_API_URL}${API_PATH}"
fi
