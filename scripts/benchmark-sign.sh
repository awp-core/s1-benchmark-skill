#!/usr/bin/env bash
# benchmark-sign.sh — Sign and execute a Benchmark API request.
#
# Uses AWP Wallet (awp-wallet CLI) for EIP-191 message signing.
#
# Usage:
#   benchmark-sign.sh <METHOD> <API_PATH> [BODY]
#
# Environment:
#   BENCHMARK_API_URL — Benchmark server base URL (default: https://tapis1.awp.sh)
#   WALLET_PASSWORD   — AWP Wallet password (managed by awp-wallet, optional)
#
# Examples:
#   benchmark-sign.sh GET  /api/v1/poll
#   benchmark-sign.sh POST /api/v1/questions '{"bs_id":"bs_math",...}'
#   benchmark-sign.sh GET  /api/v1/my/status

set -euo pipefail

METHOD="${1:?Usage: benchmark-sign.sh METHOD PATH [BODY]}"
API_PATH="${2:?Usage: benchmark-sign.sh METHOD PATH [BODY]}"
BODY="${3:-}"

BENCHMARK_API_URL="${BENCHMARK_API_URL:-https://tapis1.awp.sh}"

# Ensure wallet session is active (unlock if needed, cache token)
if [ -z "${AWP_SESSION_TOKEN:-}" ]; then
  AWP_SESSION_TOKEN=$(awp-wallet unlock --duration 3600 2>/dev/null \
    | grep -o '"token":"[^"]*"' | head -1 | cut -d'"' -f4)
  export AWP_SESSION_TOKEN
fi

# Get wallet address (cached in env or fetched)
if [ -z "${WALLET_ADDRESS:-}" ]; then
  WALLET_ADDRESS=$(awp-wallet receive 2>/dev/null \
    | grep -oi '0x[0-9a-fA-F]\{40\}' | head -1)
  export WALLET_ADDRESS
fi

TIMESTAMP=$(date +%s)
BODY_HASH=$(printf '%s' "$BODY" | sha256sum | cut -d' ' -f1)
MESSAGE="${METHOD}${API_PATH}${TIMESTAMP}${BODY_HASH}"

# Sign via AWP Wallet (EIP-191 personal_sign)
SIGN_RESULT=$(awp-wallet sign-message \
  --token "$AWP_SESSION_TOKEN" --message "$MESSAGE" 2>/dev/null)
SIGNATURE=$(echo "$SIGN_RESULT" | grep -o '"signature":"[^"]*"' | head -1 | cut -d'"' -f4)
# Fallback: if no JSON wrapper, the output IS the signature
if [ -z "$SIGNATURE" ]; then
  SIGNATURE="$SIGN_RESULT"
fi

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
