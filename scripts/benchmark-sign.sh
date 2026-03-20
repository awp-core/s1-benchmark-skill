#!/usr/bin/env bash
# benchmark-sign.sh — Sign and execute a Benchmark API request via AWP Wallet.
#
# Usage: benchmark-sign.sh METHOD PATH [BODY]
#
# Environment:
#   BENCHMARK_API_URL  — Server URL (default: https://tapis1.awp.sh)
#   WALLET_ADDRESS     — Cached wallet address (auto-detected if unset)
#   AWP_SESSION_TOKEN  — Cached session token (auto-unlocked if unset)
#
# Examples:
#   benchmark-sign.sh GET  /api/v1/poll
#   benchmark-sign.sh POST /api/v1/questions '{"bs_id":"bs_math",...}'

set -euo pipefail

# 自动检测 awp-wallet 路径（OpenClaw 可能不在 $PATH 中）
_AWP_BIN="awp-wallet"
AWP_WALLET=""
for candidate in \
  "$HOME/.local/bin/$_AWP_BIN" \
  "$HOME/.awp/bin/$_AWP_BIN" \
  "/usr/local/bin/$_AWP_BIN" \
  "$(command -v "$_AWP_BIN" 2>/dev/null)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    AWP_WALLET="$candidate"
    break
  fi
done
if [ -z "$AWP_WALLET" ]; then
  echo "[!] $_AWP_BIN not found." >&2
  exit 1
fi

METHOD="${1:?Usage: benchmark-sign.sh METHOD PATH [BODY]}"
API_PATH="${2:?Usage: benchmark-sign.sh METHOD PATH [BODY]}"
BODY="${3:-}"

BENCHMARK_API_URL="${BENCHMARK_API_URL:-https://tapis1.awp.sh}"

# Auto-unlock wallet if no session token cached
if [ -z "${AWP_SESSION_TOKEN:-}" ]; then
  AWP_SESSION_TOKEN=$($AWP_WALLET unlock --duration 3600 2>/dev/null \
    | grep -o '"token":"[^"]*"' | head -1 | cut -d'"' -f4)
  export AWP_SESSION_TOKEN
fi

# Auto-detect wallet address if not cached
if [ -z "${WALLET_ADDRESS:-}" ]; then
  WALLET_ADDRESS=$($AWP_WALLET receive 2>/dev/null \
    | grep -oi '0x[0-9a-fA-F]\{40\}' | head -1)
  export WALLET_ADDRESS
fi

TIMESTAMP=$(date +%s)
BODY_HASH=$(printf '%s' "$BODY" | sha256sum | cut -d' ' -f1)
MESSAGE="${METHOD}${API_PATH}${TIMESTAMP}${BODY_HASH}"

# Sign via AWP Wallet (EIP-191 personal_sign)
SIGN_RESULT=$($AWP_WALLET sign-message \
  --token "$AWP_SESSION_TOKEN" --message "$MESSAGE" 2>/dev/null)
SIGNATURE=$(echo "$SIGN_RESULT" | grep -o '"signature":"[^"]*"' | head -1 | cut -d'"' -f4)
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
