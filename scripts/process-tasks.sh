#!/usr/bin/env bash
# process-tasks.sh — List pending tasks and worker status for the OpenClaw agent.
#
# Usage: process-tasks.sh [TASK_DIR]
#
# The agent reads this output, processes each task, and writes responses.

set -euo pipefail

TASK_DIR="${1:-${BENCHMARK_TASK_DIR:-/tmp/benchmark-tasks}}"
STATUS_FILE="${BENCHMARK_STATUS_FILE:-/tmp/benchmark-worker-status.json}"
PENDING_DIR="$TASK_DIR/pending"
DONE_DIR="$TASK_DIR/done"

mkdir -p "$PENDING_DIR" "$DONE_DIR"

# Count pending tasks
PENDING_COUNT=$(find "$PENDING_DIR" -name '*.json' -type f 2>/dev/null | wc -l)

# Print worker status
echo "=== WORKER STATUS ==="
if [ -f "$STATUS_FILE" ]; then
  cat "$STATUS_FILE"
else
  echo '{"running": false, "error": "status file not found"}'
fi

echo ""
echo "=== PENDING TASKS: $PENDING_COUNT ==="

if [ "$PENDING_COUNT" -eq 0 ]; then
  echo "No pending tasks."
  exit 0
fi

# Print each task
find "$PENDING_DIR" -name '*.json' -type f 2>/dev/null | sort | while IFS= read -r task_file; do
  echo "--- $(basename "$task_file") ---"
  cat "$task_file"
  echo ""
done
