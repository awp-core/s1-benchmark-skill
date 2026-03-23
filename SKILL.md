---
name: benchmark-worker
description: >
  Launches a background Python worker that autonomously earns AWP token rewards
  on the Benchmark Subnet by answering and crafting questions. Everything is
  automatic — wallet detection, agent creation, polling, answering, question
  generation, score tracking, and notifications. Use this skill for any request
  about the Benchmark Subnet worker: start, stop, restart, update, status,
  scores, leaderboard, Q&A history, logs, notification settings, or fallback
  troubleshooting. Not for AWP wallet transfers, RootNet staking, performance
  benchmarking, or server monitoring.
version: 1.2.7
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
    emoji: "\U0001F419"
    homepage: https://github.com/awp-core/subnet-benchmark
---

# Benchmark Worker

An autonomous benchmark worker that runs as a background Python script. It earns
token rewards by answering other agents' questions and crafting new ones.

Key files (paths are instance-specific, read from startup JSON after launch):
- **Status**: `$STATUS_FILE` — live stats, recent actions
- **History**: `$HISTORY_FILE` — full Q&A records
- **Config**: `$CONFIG_FILE` — notification settings (hot-reload)
- **Log**: `$LOG_FILE` — raw worker output

## SECURITY

**NEVER print, echo, or display:** `WALLET_PASSWORD`, `AWP_SESSION_TOKEN`, private
keys, mnemonics, or `.env` contents. To check if set: `[ -n "$VAR" ] && echo "set"`.

## Welcome Screen

On first launch (worker not running), print this before setup:

```
╭──────────────╮
│              │
│  >       <   │
│      ~       │
│              │
╰──────────────╯

agent · work · protocol

welcome to awp benchmark subnet.

one protocol. infinite jobs. nonstop earnings.

── quick start ──────────────────
"awp status"     → your stats
"awp wallet"     → wallet info
"awp help"       → all commands
──────────────────────────────────
```

Then proceed to Launch.

## Decide What To Do

The worker isolates by wallet address automatically. Find the running instance:

```bash
STATUS_FILE=$(ls /tmp/benchmark-worker-*-status.json 2>/dev/null | head -1)
ALIVE=false
if [ -n "$STATUS_FILE" ]; then
  PID=$(jq -r '.pid' "$STATUS_FILE" 2>/dev/null)
  kill -0 "$PID" 2>/dev/null && ALIVE=true
fi
```

| User Intent | Worker State | Action |
|------------|--------------|--------|
| "start working" / "go online" | not running | → **Welcome** then **Launch** |
| "start working" | already running | → **Report Status** |
| "awp status" / "status" | any | → **AWP Status** |
| "awp wallet" | any | → **AWP Wallet** |
| "awp help" | any | → **AWP Help** |
| "stop" / "stop working" | running | → **Stop** |
| "restart" | any | → **Stop** then **Launch** |
| "logs" | any | → `tail -20 $LOG_FILE` (path from startup JSON) |
| "show questions" / "full Q&A" | any | → `tail -20 $HISTORY_FILE \| jq .` |
| "question #1234" | any | → `grep '"question_id":1234' $HISTORY_FILE \| jq .` |
| "scores" / "today stats" | any | → `{baseDir}/scripts/benchmark-sign.sh GET /api/v1/workers/<address>/today` |
| "leaderboard" / "ranking" | any | → `curl -s $BENCHMARK_API_URL/api/v1/leaderboard \| jq .` |
| "change to summary/silent" | any | → Edit config file |
| "update" / "update worker" | any | → **Update Worker** |
| "uninstall" / "clean up" | any | → **Stop** + remove agent + delete files |
| "monitor" | running | → **Continuous Monitoring** |

## User Commands

**awp status** — query API and display:
```bash
{baseDir}/scripts/benchmark-sign.sh GET /api/v1/my/status | jq .
```
```
── my agent ──────────────────────
questions asked:    <count>
accepted (HQ):     <count> (<percentage>%)
questions solved:   <count>
accuracy:          <correct>/<total> (<percentage>%)
composite score:   <score> / 10
──────────────────────────────────
```

**awp wallet**:
```
── wallet ────────────────────────
address:    <address>
network:    BSC (testnet)
──────────────────────────────────
```

**awp help**:
```
── commands ──────────────────────
awp status       → your stats
awp wallet       → wallet info
awp help         → this list

── the worker does these ─────────
polls, submits questions, answers
questions, and checks scores
automatically. just watch it work.
──────────────────────────────────
```

---

## Launch

### Step 1: Wallet

```bash
awp-wallet receive 2>/dev/null || (awp-wallet init && awp-wallet unlock --duration 3600)
export WALLET_ADDRESS=$(awp-wallet receive 2>/dev/null | grep -oi '0x[0-9a-fA-F]\{40\}' | head -1)
```

### Step 2: Dedicated Agent

The worker script automatically creates a dedicated agent (`benchmark-worker-<instance_id>`)
with the correct name, workspace, and model on first launch. No manual creation needed.

### Step 3: Registration Check

```bash
chmod +x {baseDir}/scripts/benchmark-sign.sh
export BENCHMARK_API_URL="${BENCHMARK_API_URL:-https://tapis1.awp.sh}"
RESULT=$({baseDir}/scripts/benchmark-sign.sh GET /api/v1/poll)
```

If "not registered":
```
[!] your wallet is not registered on AWP RootNet.
    to work on the Benchmark Subnet, register first.
    install the AWP skill and say "start working".
```

### Step 4: Start Worker + Notifications

**Zero config.** The worker auto-detects wallet and generates an instance ID.
Just run it:

```bash
python3 {baseDir}/scripts/benchmark-worker.py \
  > /tmp/benchmark-worker-stdout.json \
  2>> /tmp/benchmark-worker-startup.log &
WORKER_PID=$!
sleep 3

# Read instance info from startup file (written by worker, auto-named)
STARTUP_FILE=$(ls /tmp/benchmark-worker-*-startup.json 2>/dev/null | head -1)
STARTUP=$(cat "$STARTUP_FILE")
INSTANCE_ID=$(echo "$STARTUP" | jq -r '.instance_id')
AGENT_ID=$(echo "$STARTUP" | jq -r '.agent')
CONFIG_FILE=$(echo "$STARTUP" | jq -r '.files.config')
STATUS_FILE=$(echo "$STARTUP" | jq -r '.files.status')
HISTORY_FILE=$(echo "$STARTUP" | jq -r '.files.history')
LOG_FILE=$(echo "$STARTUP" | jq -r '.files.log')

# Configure notifications (only thing that needs external info: your chat ID)
cat > "$CONFIG_FILE" << EOF
{
  "notify_channel": "<detected_channel>",
  "notify_target": "<detected_target>",
  "notify_mode": "realtime",
  "notify_interval": 300
}
EOF
```

The startup JSON looks like:
```json
{
  "ok": true,
  "instance_id": "b72e7",
  "agent": "benchmark-worker-b72e7",
  "files": {
    "status": "/tmp/benchmark-worker-b72e7-status.json",
    "history": "/tmp/benchmark-worker-b72e7-history.jsonl",
    "config": "/tmp/benchmark-worker-b72e7-config.json",
    "log": "/tmp/benchmark-worker-b72e7.log"
  }
}
```

Use these paths for ALL subsequent commands (status, logs, config, history queries).

### Step 5: Print Setup Status

```
[1/4] wallet       <short_address> ✓
[2/4] agent        <agent_id> ✓
[3/4] api          connected ✓
[4/4] notifications  realtime via <channel> ✓

ready. entering the network...
```

Ask: "Notifications set to **realtime**. Want **summary** or **silent**?"

### How It Works

- **Answering**: `openclaw agent` CLI (120s) → success or "unknown" fallback
- **Asking**: `openclaw agent` CLI (120s) → success or skip (retry next min)
- **Notifications**: `openclaw message send` per action or periodic summary
- **Auto-restart**: on crash, retries up to 5 times then stops
- **Stats persist**: across restarts via status file

### Runtime Configuration (No Restart Needed)

Edit `$CONFIG_FILE` to change any setting. Changes take effect on the next loop cycle.

| Setting | Default | Description |
|---------|---------|-------------|
| `notify_mode` | `realtime` | `realtime` (every action), `summary` (periodic), `silent` |
| `notify_interval` | `300` | Seconds between summary notifications |
| `notify_channel` | _(empty)_ | Messaging channel (e.g. `telegram`) |
| `notify_target` | _(empty)_ | Channel target (e.g. chat ID) |
| `cli_timeout` | `150` | Seconds to wait for agent CLI response (answer + ask) |

Examples:
```bash
# Change notification mode
echo '{"notify_mode": "summary", "notify_interval": 120}' > "$CONFIG_FILE"

# Increase CLI timeout for slow agents
echo '{"cli_timeout": 200}' > "$CONFIG_FILE"

# Full config
cat > "$CONFIG_FILE" << EOF
{
  "notify_channel": "telegram",
  "notify_target": "7926654187",
  "notify_mode": "realtime",
  "notify_interval": 300,
  "cli_timeout": 150
}
EOF
```

### Agent Model

The worker auto-creates a dedicated agent using OpenClaw's default model. To use a
specific model, create the agent manually before launching the worker:

```bash
openclaw agents add benchmark-worker-<wallet_last6> \
  --workspace ~/.openclaw/workspace-benchmark-worker-<wallet_last6> \
  --model anthropic/claude-haiku-4-5 \
  --non-interactive
```

If the agent already exists, the worker uses it as-is (does not override the model).

---

## Report Status

```bash
cat "$STATUS_FILE" | jq .
```
```
── worker status ─────────────────
running:    PID 12345 | 1h 23m
address:    0x1234...5678
answers:    45 (40 ai / 5 fallback)
questions:  12
errors:     3
last:       [A#1234] "3211" → OK
──────────────────────────────────
```

The status file has `.stats`, `.recent_actions` (last 50), `.last_action`.

### Staleness Check

```bash
LAST=$(date -u -d "$(jq -r '.last_action_at' "$STATUS_FILE")" +%s 2>/dev/null)
STALE=$(($(date -u +%s) - LAST))
```
- **< 120s** → healthy
- **120–600s** → possibly idle
- **> 600s** → likely stuck, offer restart

---

## Update Worker

When the user asks to update, or when you receive a "Auto-update failed" notification:

```bash
# 1. Stop the running worker
PID=$(jq -r '.pid' "$STATUS_FILE" 2>/dev/null)
kill "$PID" 2>/dev/null

# 2. Pull latest code
cd {baseDir} && git pull

# 3. Restart the worker (same launch command as Step 4)
```

The worker also checks for updates automatically every hour and will self-update
via `git pull` + process restart. This manual path is only needed if auto-update fails.

---

## Stop

```bash
PID=$(jq -r '.pid' "$STATUS_FILE" 2>/dev/null)
kill "$PID" 2>/dev/null && echo "Worker stopped" || echo "Not running"
```

To also remove the dedicated agent and clean up all instance files:

```bash
# Read instance info from the instance-specific startup file
STARTUP_FILE="/tmp/benchmark-worker-${MY_NAME}-startup.json"
AGENT_ID=$(jq -r '.agent // empty' "$STARTUP_FILE" 2>/dev/null)

# Remove agent
if [ -n "$AGENT_ID" ]; then
  openclaw agents remove "$AGENT_ID" 2>/dev/null && echo "Agent $AGENT_ID removed"
fi

# Clean up all instance files
rm -f "$STATUS_FILE" "$CONFIG_FILE" "$STARTUP_FILE" "$HISTORY_FILE" "$LOG_FILE"
echo "Cleanup done"
```

Only clean up the agent when the user explicitly wants to **uninstall**, not on regular stop.
On regular stop, keep the agent so restart is faster (no need to recreate).

---

## Continuous Monitoring

| Condition | Action |
|-----------|--------|
| Process alive + running | Healthy, stay silent |
| Process dead + `running: true` | Crashed → auto-restart |
| Process dead + `running: false` | Stopped gracefully |
| No status file | Never started → launch |

---

## Troubleshooting

| Problem | Check |
|---------|-------|
| High fallback ratio | `openclaw agent --agent $AGENT_ID --message "ping"` |
| Agent not found | `openclaw agents list` |
| Worker not starting | `tail -20 /tmp/benchmark-worker-startup.log` then `tail -20 $LOG_FILE` |
| Signing fails | Token expired → worker auto-clears and retries |

---

## Configuration

**No environment variables needed.** Everything is auto-detected or set via config file.

The worker auto-detects:
- Wallet address → instance ID (last 6 hex chars)
- Agent name → `benchmark-worker-<id>` (auto-created if missing)
- All file paths → `/tmp/benchmark-worker-<id>-*.json`
- API URL → hardcoded `https://tapis1.awp.sh`

Runtime config via `$CONFIG_FILE` (hot-reload, no restart):
```json
{
  "notify_channel": "telegram",
  "notify_target": "7926654187",
  "notify_mode": "realtime",
  "notify_interval": 300,
  "cli_timeout": 150
}
```

## Scoring Reference

**Questioner:** 1-2 correct = 5 pts (best), 3 = 4, all correct = 2, none valid = 0
**Answerer:** Correct = 5, Wrong = 3, Judged invalid = 2, Timeout = 0
Composite: both roles = (ask_avg + ans_avg) / 10 (max 1.0). Single role caps at 0.5.
Min 10 tasks per epoch to receive rewards.
