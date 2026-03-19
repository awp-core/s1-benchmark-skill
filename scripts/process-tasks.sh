#!/usr/bin/env bash
# process-tasks.sh — List pending tasks for the OpenClaw agent to process.
#
# Usage: process-tasks.sh [TASK_DIR]
#
# Reads all pending task files from TASK_DIR/pending/ and prints them.
# The agent reads the prompt from each task, generates a response,
# and writes the result to TASK_DIR/done/<task_id>.json.
#
# Default TASK_DIR: /tmp/benchmark-tasks

set -euo pipefail

TASK_DIR="${1:-${BENCHMARK_TASK_DIR:-/tmp/benchmark-tasks}}"
PENDING_DIR="$TASK_DIR/pending"
DONE_DIR="$TASK_DIR/done"

mkdir -p "$PENDING_DIR" "$DONE_DIR"

# List pending tasks
TASKS=$(find "$PENDING_DIR" -name '*.json' -type f 2>/dev/null | sort)

if [ -z "$TASKS" ]; then
  echo "NO_PENDING_TASKS"
  exit 0
fi

echo "PENDING_TASKS:"
for task_file in $TASKS; do
  cat "$task_file"
  echo "---"
done
