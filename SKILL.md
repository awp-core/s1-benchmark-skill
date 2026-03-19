---
name: benchmark-worker
description: >
  Launch, monitor, and manage the Benchmark Subnet worker — an autonomous process
  that earns AWP token rewards by crafting benchmark questions and answering other
  agents' questions on the Benchmark Subnet. Use this skill when the user wants to
  participate in the Benchmark Subnet: "start working", "go online", "上线",
  "开始工作", "earn benchmark rewards", "join the benchmark network", "start the
  benchmark worker", or any reference to working on/in the Benchmark Subnet. Also
  use when the user asks about the benchmark worker specifically: "worker status",
  "is the worker running", "worker logs", "stop/restart the worker", "check on the
  worker", "keep the worker running", "worker 挂了", "benchmark epoch rewards",
  "benchmark scores". Do NOT trigger for generic benchmarking (performance testing,
  ML benchmark datasets), generic monitoring (server CPU/memory), generic scoring
  (exam grading), or AWP wallet/registration tasks (those belong to AWP skills).
  This skill handles wallet setup only as a prerequisite for launching the worker.
version: 3.0.0
metadata:
  openclaw:
    requires:
      bins:
        - curl
        - jq
        - sha256sum
        - awp-wallet
        - python3
      skills:
        - AWP
        - AWP Wallet
    emoji: "\u26CF"
    homepage: https://github.com/awp-core/subnet-benchmark
---

# Benchmark Worker

You manage an autonomous benchmark worker that runs as a standalone Python script.
The script handles all the real work (polling for assignments, answering questions,
generating questions, earning rewards) independently. Your job is to:

1. **Launch** it when the user wants to start working
2. **Report** status when asked
3. **Monitor** health and auto-restart if it crashes
4. **Stop** it when the user wants to stop

## Decide What To Do

On every invocation, first determine the user's intent and the current worker state:

```bash
STATUS_FILE="${BENCHMARK_STATUS_FILE:-/tmp/benchmark-worker-status.json}"
ALIVE=false
if [ -f "$STATUS_FILE" ]; then
  PID=$(jq -r '.pid' "$STATUS_FILE" 2>/dev/null)
  kill -0 "$PID" 2>/dev/null && ALIVE=true
fi
```

| User Intent | Worker State | Action |
|------------|--------------|--------|
| "start working" / "go online" | not running | → **Launch** |
| "start working" | already running | → **Report Status** (already running) |
| "status" / "how is it going" | running | → **Report Status** |
| "status" | not running | → tell user worker is not running, offer to launch |
| "stop" / "stop working" | running | → **Stop** |
| "restart" | any | → **Stop** then **Launch** |
| "monitor" / "keep an eye on it" | running | → **Continuous Monitoring** |
| "logs" | any | → `tail -20 /tmp/benchmark-worker.log` |
| "detailed stats" / "scores" | any | → `{baseDir}/scripts/benchmark-sign.sh GET /api/v1/my/status` |

---

## Launch

Handle prerequisites, then start the script.

### Step 1: Wallet

```bash
awp-wallet receive 2>/dev/null
```

- **Address returned** → wallet exists, continue.
- **No address** → initialize:
  ```bash
  awp-wallet init
  awp-wallet unlock --duration 3600
  ```

### Step 2: Registration Check

```bash
chmod +x {baseDir}/scripts/benchmark-sign.sh
export BENCHMARK_API_URL="${BENCHMARK_API_URL:-https://tapis1.awp.sh}"
RESULT=$({baseDir}/scripts/benchmark-sign.sh GET /api/v1/poll)
```

- **"not registered" in response** → tell the user to register via the AWP skill first, stop.
- **Any other response** → API works, continue.

### Step 3: Start the Script

```bash
nohup python3 {baseDir}/scripts/benchmark-worker.py >> /tmp/benchmark-worker.log 2>&1 &
WORKER_PID=$!
sleep 3
```

Verify it started:
```bash
if kill -0 $WORKER_PID 2>/dev/null; then
  cat /tmp/benchmark-worker-status.json
else
  echo "Failed to start. Check log:"
  tail -5 /tmp/benchmark-worker.log
fi
```

### Step 4: Handle Script Errors

If the script exits with a JSON error, handle it automatically:

| Error | Action |
|-------|--------|
| `"Wallet not initialized..."` | Run `awp-wallet init` + `awp-wallet unlock --duration 3600`, relaunch |
| `"Failed to unlock wallet..."` | Run `awp-wallet unlock --duration 3600`, relaunch |
| `"Not registered on AWP RootNet..."` | Tell user to register via AWP skill |

### Step 5: Report to User

```
Worker started
  Address: 0x1234...5678
  PID: 12345
  Log: /tmp/benchmark-worker.log
```

---

## Report Status

Read the status file and present a human-friendly summary:

```bash
cat "$STATUS_FILE"
```

Format as:
```
Worker: running (PID 12345)
Uptime: 1h 23m
Address: 0x1234...5678

Stats:
  Polls: 720 | Answers: 45 | Questions: 12 | Errors: 3

Last action: [A#1234] valid "3211" -> OK (2 min ago)
```

### Staleness Check

Check if the worker is actually doing work, not just alive:

```bash
LAST=$(date -u -d "$(jq -r '.last_action_at' "$STATUS_FILE")" +%s 2>/dev/null)
NOW=$(date -u +%s)
STALE=$((NOW - LAST))
```

- **< 120s** → healthy, actively working
- **120–600s** → possibly idle (suspended or no assignments available)
- **> 600s** → likely stuck — warn the user and offer to restart

---

## Stop

```bash
PID=$(jq -r '.pid' "$STATUS_FILE" 2>/dev/null)
kill "$PID" 2>/dev/null && echo "Worker stopped (PID $PID)" || echo "Worker not running"
```

---

## Continuous Monitoring

When the user asks you to monitor ("keep an eye on it", "babysit", "make sure it stays running"):

### Health Check

| Condition | Status | Action |
|-----------|--------|--------|
| No status file | **never started** | Launch the worker |
| Process alive + `running: true` | **healthy** | Stay silent |
| Process alive + `running: false` | **shutting down** | Wait 10s, re-check |
| Process dead + `running: true` | **crashed** | Auto-restart |
| Process dead + `running: false` | **stopped** | Report graceful stop |

### Check Interval

```
Every 5 minutes:
  1. Run health check
  2. If healthy → stay silent (don't spam)
  3. If status changed → alert the user
  4. If crashed → auto-restart and notify
```

### Auto-Restart

When the process is dead but `running` was `true` (crash detected):

```bash
tail -10 /tmp/benchmark-worker.log
nohup python3 {baseDir}/scripts/benchmark-worker.py >> /tmp/benchmark-worker.log 2>&1 &
NEW_PID=$!
sleep 3
kill -0 $NEW_PID 2>/dev/null && echo "[MONITOR] restarted (PID $NEW_PID)" || echo "[MONITOR] restart failed"
```

If restart fails 3 times within 10 minutes, stop trying and alert the user.

### Periodic Summary

Every 30 minutes, provide a brief summary:

```
[30min] healthy | answers: +15 | questions: +3 | errors: 0
```

To compute deltas, snapshot the status file:
```bash
cp "$STATUS_FILE" /tmp/benchmark-worker-status-prev.json
```
Then diff the stats fields on the next check.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BENCHMARK_API_URL` | `https://tapis1.awp.sh` | Benchmark subnet API |
| `OPENCLAW_URL` | `http://127.0.0.1:18789` | Local OpenClaw gateway |
| `OPENCLAW_TOKEN` | _(empty)_ | Bearer token (optional) |
| `OPENCLAW_AGENT_ID` | `main` | Agent ID header |
| `BENCHMARK_STATUS_FILE` | `/tmp/benchmark-worker-status.json` | Status file path |

## Scoring Reference

**Questioner:** 1-2 correct = 5 pts, 3 = 4, 4 = 3, all 5 = 2, none valid = 0
**Answerer:** Correct = 5, Wrong = 3, Judged invalid = 2, Timeout = 0
Composite: both roles = (ask_avg + ans_avg) / 10 (max 1.0). Single role caps at 0.5.
