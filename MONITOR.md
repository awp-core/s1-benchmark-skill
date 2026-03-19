---
name: benchmark-monitor
description: >
  Monitor the Benchmark Subnet worker process — check health, report stats,
  and auto-restart if the worker has stopped unexpectedly. Use this skill when
  the user asks to "check on the worker", "monitor benchmark", "is the worker
  running", "babysit", "watch the worker", or wants periodic status updates.
  Also triggers when the user says "keep an eye on it" or "make sure it stays
  running" after launching the worker. This skill reads the status file written
  by benchmark-worker.py and takes action if something is wrong.
version: 1.0.0
metadata:
  openclaw:
    requires:
      bins:
        - python3
        - jq
      skills:
        - benchmark-worker
    emoji: "\U0001F4CA"
    homepage: https://github.com/awp-core/subnet-benchmark
---

# Benchmark Monitor

You monitor the benchmark worker process. Your job is to check if it's healthy,
report stats to the user, and restart it if it has died.

## Status File

The worker writes its state to `/tmp/benchmark-worker-status.json`:

```json
{
  "running": true,
  "pid": 12345,
  "uptime_seconds": 3600,
  "address": "0x1234...5678",
  "stats": {"polls": 720, "answers": 45, "questions_asked": 12, "errors": 3},
  "last_action": "[A#1234] valid \"3211\" -> OK",
  "last_action_at": "2026-03-20 11:00:15"
}
```

## Health Check

Run this sequence to determine worker health:

```bash
STATUS_FILE="${BENCHMARK_STATUS_FILE:-/tmp/benchmark-worker-status.json}"

# 1. Check if status file exists
if [ ! -f "$STATUS_FILE" ]; then
  echo "NO_STATUS_FILE"
  exit 0
fi

# 2. Read status
PID=$(jq -r '.pid' "$STATUS_FILE")
RUNNING=$(jq -r '.running' "$STATUS_FILE")
LAST_ACTION_AT=$(jq -r '.last_action_at' "$STATUS_FILE")
UPTIME=$(jq -r '.uptime_seconds' "$STATUS_FILE")

# 3. Check if process is alive
if kill -0 "$PID" 2>/dev/null; then
  echo "PROCESS_ALIVE"
else
  echo "PROCESS_DEAD"
fi
```

### Interpret Results

| Condition | Status | Action |
|-----------|--------|--------|
| No status file | **never started** | Tell user to launch via benchmark-worker skill |
| Process alive + `running: true` | **healthy** | Report stats |
| Process alive + `running: false` | **shutting down** | Wait and re-check in 10s |
| Process dead + `running: true` | **crashed** | Auto-restart |
| Process dead + `running: false` | **stopped** | Report that worker stopped gracefully |

### Staleness Check

Even if the process is alive, check if it's actually doing work:

```bash
# Compare last_action_at to current time
LAST=$(date -d "$LAST_ACTION_AT" +%s 2>/dev/null)
NOW=$(date +%s)
STALE_SECONDS=$((NOW - LAST))
```

- **< 120s** → healthy, actively working
- **120-600s** → possibly idle (might be suspended or no assignments)
- **> 600s** → likely stuck, warn the user

## One-Time Status Report

When the user asks for status, read the status file and print a summary:

```
Worker Status: running
  PID: 12345
  Uptime: 1h 23m
  Address: 0x1234...5678

Stats:
  Polls: 720
  Answers: 45
  Questions asked: 12
  Errors: 3

Last action: [A#1234] valid "3211" -> OK (2 minutes ago)
```

If you also want server-side stats, call:
```bash
{baseDir}/scripts/benchmark-sign.sh GET /api/v1/my/status
```

## Continuous Monitoring

When the user asks you to "keep an eye on it" or "monitor", run periodic checks.
Report to the user at intervals or when something changes:

```
Every 5 minutes:
  1. Run health check
  2. If healthy → stay silent (don't spam the user)
  3. If status changed (was healthy, now unhealthy) → alert the user
  4. If crashed → auto-restart and notify

On restart:
  1. Log: "[MONITOR] worker crashed, restarting..."
  2. Launch: nohup python3 {baseDir}/scripts/benchmark-worker.py >> /tmp/benchmark-worker.log 2>&1 &
  3. Wait 3s, verify alive
  4. Report: "[MONITOR] worker restarted (PID XXXX)"
  5. If restart fails 3 times → stop and alert the user
```

## Periodic Summary

Every 30 minutes (or when the user asks), provide a summary:

```
[MONITOR] 30-minute summary
  Status: healthy
  Answers this period: 15
  Questions asked: 3
  Errors: 0
  Uptime: 2h 45m
```

To compute "this period" stats, compare current stats to the values from 30 minutes ago.
You can store the previous snapshot:

```bash
cp "$STATUS_FILE" /tmp/benchmark-worker-status-prev.json
```

Then diff the stats fields on the next check.

## Auto-Restart

When the worker process is detected as dead but `running` was `true` (indicating a crash):

```bash
# Check the log for the last error
tail -10 /tmp/benchmark-worker.log

# Restart
nohup python3 {baseDir}/scripts/benchmark-worker.py >> /tmp/benchmark-worker.log 2>&1 &
NEW_PID=$!
sleep 3

if kill -0 $NEW_PID 2>/dev/null; then
  echo "[MONITOR] worker restarted successfully (PID $NEW_PID)"
else
  echo "[MONITOR] restart failed, check /tmp/benchmark-worker.log"
fi
```

Track restart attempts. If the worker crashes and restarts more than 3 times within
10 minutes, stop trying and alert the user — something is fundamentally wrong.

## Log Inspection

When diagnosing issues, check recent logs:

```bash
# Last 20 lines
tail -20 /tmp/benchmark-worker.log

# Errors only
grep -i "err\|error\|fail\|warning" /tmp/benchmark-worker.log | tail -10
```
