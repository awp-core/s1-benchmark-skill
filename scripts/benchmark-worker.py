#!/usr/bin/env python3
"""Standalone benchmark worker — polls, answers, and asks in a loop.

Delegates signing to benchmark-sign.sh.
When LLM reasoning is needed, calls openclaw agent CLI directly.
"""

import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration (environment variables with defaults)
# ---------------------------------------------------------------------------

# All configuration is hardcoded or auto-detected. No environment variables needed.
# The agent just runs: python3 benchmark-worker.py — zero setup required.
# Runtime changes (notification mode, etc.) go through the config file.

BENCHMARK_API_URL: str = "https://tapis1.awp.sh"

SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
SIGN_SCRIPT: str = os.path.join(SCRIPT_DIR, "benchmark-sign.sh")

# Instance ID: derived from wallet address at startup (see main()).
# Isolates file paths and agent names for multi-instance support.
INSTANCE_ID: str = ""

POLL_SLEEP: int = 5  # seconds between idle polls
NET_RETRY_SLEEP: int = 10  # seconds after network error
SUSPEND_SLEEP: int = 60  # seconds when suspended
UNLOCK_INTERVAL: int = 25 * 60  # re-unlock every 25 minutes
ASK_INTERVAL: int = 60  # seconds between question submissions (API rate limit: 1/min)
CLI_TIMEOUT: int = 120  # max seconds for a single openclaw agent CLI call
MAX_RESTARTS: int = 5  # max auto-restarts before giving up
RESTART_COOLDOWN: int = 10  # seconds between restart attempts

# File paths — set by _init_instance_paths() after wallet detected
STATUS_FILE: str = ""
HISTORY_FILE: str = ""
CONFIG_FILE: str = ""
LOG_FILE: str = ""


def _init_instance_paths() -> None:
    """Initialize all file paths with instance ID suffix for multi-instance isolation."""
    global STATUS_FILE, HISTORY_FILE, CONFIG_FILE, LOG_FILE
    suffix = f"-{INSTANCE_ID}" if INSTANCE_ID else ""
    STATUS_FILE = f"/tmp/benchmark-worker{suffix}-status.json"
    HISTORY_FILE = f"/tmp/benchmark-worker{suffix}-history.jsonl"
    CONFIG_FILE = f"/tmp/benchmark-worker{suffix}-config.json"
    LOG_FILE = f"/tmp/benchmark-worker{suffix}.log"


# Notification defaults (overridden by config file at runtime)
_DEFAULT_NOTIFY_CHANNEL: str = ""
_DEFAULT_NOTIFY_TARGET: str = ""
_DEFAULT_NOTIFY_MODE: str = "realtime"
_DEFAULT_NOTIFY_INTERVAL: int = 300

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("worker")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

running: bool = True


def _shutdown(signum: int, _frame: object) -> None:
    global running
    running = False
    log.info("[EXIT] shutting down (signal %d)", signum)


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ---------------------------------------------------------------------------
# Subprocess environment (carries AWP_SESSION_TOKEN, WALLET_ADDRESS, etc.)
# ---------------------------------------------------------------------------

sub_env: dict[str, str] = {**os.environ}

# ---------------------------------------------------------------------------
# Status tracking
# ---------------------------------------------------------------------------

_start_time: float = time.monotonic()
_stats: dict[str, int] = {
    "polls": 0,
    "answers": 0,
    "answers_ai": 0,
    "answers_fallback": 0,
    "questions_asked": 0,
    "errors": 0,
}
_last_action: str = ""
_last_action_at: str = ""
_worker_address: str = ""
_recent_actions: list[dict[str, str]] = []  # last N actions for user queries
_RECENT_ACTIONS_MAX: int = 50


def _restore_stats() -> None:
    """Restore stats from the status file on startup, so restarts don't reset counters."""
    global _stats, _last_action, _last_action_at, _recent_actions
    if not STATUS_FILE:
        log.info("[SETUP] no status file path set, starting fresh")
        return
    try:
        with open(STATUS_FILE) as f:
            data = json.load(f)
        prev = data.get("stats", {})
        for key in _stats:
            if key in prev and isinstance(prev[key], int):
                _stats[key] = prev[key]
        _last_action = data.get("last_action", "")
        _last_action_at = data.get("last_action_at", "")
        actions = data.get("recent_actions", [])
        if isinstance(actions, list):
            _recent_actions = actions[-_RECENT_ACTIONS_MAX:]
        log.info("[SETUP] restored stats from previous run: %s", _stats)
    except (OSError, json.JSONDecodeError, KeyError):
        log.info("[SETUP] no previous stats to restore, starting fresh")


def _write_status() -> None:
    """Write current worker status to a JSON file for external monitoring.

    This file serves two purposes:
    1. Worker health monitoring (is it running, uptime, error count)
    2. Shared state for the main agent — when user asks "how's the worker",
       the main agent reads this file to answer with full context.
    """
    status = {
        "running": running,
        "pid": os.getpid(),
        "uptime_seconds": int(time.monotonic() - _start_time),
        "address": _worker_address,
        "stats": {**_stats},
        "last_action": _last_action,
        "last_action_at": _last_action_at,
        "recent_actions": _recent_actions[-_RECENT_ACTIONS_MAX:],
    }
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f, indent=2)
        os.replace(tmp, STATUS_FILE)
    except OSError as e:
        log.warning("[STATUS] failed to write status file: %s", e)


def _log_history(entry: dict) -> None:
    """Append a full Q&A record to the JSONL history file."""
    entry["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _record_action(action: str, detail: dict | None = None) -> None:
    """Record the latest action for status reporting and history."""
    global _last_action, _last_action_at
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _last_action = action
    _last_action_at = now
    entry: dict = {"time": now, "action": action}
    if detail:
        entry.update(detail)
    _recent_actions.append(entry)
    # Trim to keep memory bounded
    if len(_recent_actions) > _RECENT_ACTIONS_MAX * 2:
        del _recent_actions[: len(_recent_actions) - _RECENT_ACTIONS_MAX]


# ---------------------------------------------------------------------------
# Wallet helpers
# ---------------------------------------------------------------------------


def get_wallet_address() -> str | None:
    """Return the wallet address, or None if no wallet is initialized."""
    try:
        result = subprocess.run(
            ["awp-wallet", "receive"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        match = re.search(r"0x[0-9a-fA-F]{40}", result.stdout)
        return match.group(0) if match else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def unlock_wallet() -> bool:
    """Unlock the wallet for 3600s and cache the session token.

    On failure, clears AWP_SESSION_TOKEN so benchmark-sign.sh will
    auto-unlock on its next invocation.
    """
    try:
        result = subprocess.run(
            ["awp-wallet", "unlock", "--duration", "3600"],
            capture_output=True,
            text=True,
            timeout=15,
            env=sub_env,  # pass current env (includes WALLET_PASSWORD if set)
        )
        if result.returncode != 0:
            log.warning("[WALLET] unlock failed: %s", result.stderr.strip())
            # Clear stale token so sign.sh will auto-unlock
            sub_env.pop("AWP_SESSION_TOKEN", None)
            return False
        # awp-wallet may output "token" or "sessionToken" depending on version
        match = re.search(r'"(?:session[Tt]oken|token)"\s*:\s*"([^"]+)"', result.stdout)
        if match:
            sub_env["AWP_SESSION_TOKEN"] = match.group(1)
            return True
        # Fallback: try to parse as JSON
        try:
            data = json.loads(result.stdout)
            token = data.get("sessionToken") or data.get("token") or ""
            if token:
                sub_env["AWP_SESSION_TOKEN"] = str(token)
                return True
        except (json.JSONDecodeError, AttributeError):
            pass
        # Last resort: if output is just a raw token string
        token = result.stdout.strip()
        if token and not token.startswith("{"):
            sub_env["AWP_SESSION_TOKEN"] = token
            return True
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def signed_request(method: str, path: str, body: str = "") -> str:
    """Execute a signed API request via benchmark-sign.sh. Returns raw stdout.

    On sign failure (exit != 0), clears AWP_SESSION_TOKEN so the next call
    lets benchmark-sign.sh auto-unlock with a fresh token.
    """
    args = [SIGN_SCRIPT, method, path]
    if body:
        args.append(body)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            env=sub_env,
        )
        if result.returncode != 0:
            log.warning("[SIGN] exit %d: %s", result.returncode, result.stderr.strip())
            # Token may be expired — clear it so sign.sh auto-unlocks next time
            sub_env.pop("AWP_SESSION_TOKEN", None)
        return result.stdout
    except subprocess.TimeoutExpired:
        return '{"ok":false,"error":"sign request timeout"}'
    except FileNotFoundError:
        return '{"ok":false,"error":"benchmark-sign.sh not found"}'


# ---------------------------------------------------------------------------
# OpenClaw agent: direct CLI call (preferred) with file queue fallback
# ---------------------------------------------------------------------------

_agent_id: str = ""  # detected at startup
_session_counter: int = 0  # unique session counter
_rate_limit_until: float = 0  # monotonic time until rate limit backoff ends


def detect_agent() -> str:
    """Detect or create the dedicated benchmark-worker agent."""
    global _agent_id

    suffix = f"-{INSTANCE_ID}" if INSTANCE_ID else ""
    _agent_id = f"benchmark-worker{suffix}"

    # Check if agent already exists
    if _agent_exists(_agent_id):
        log.info("[AGENT] found existing agent: %s", _agent_id)
        return _agent_id

    # Try to create it
    log.info("[AGENT] agent '%s' not found, creating...", _agent_id)
    try:
        result = subprocess.run(
            [
                "openclaw",
                "agents",
                "add",
                _agent_id,
                "--workspace",
                os.path.expanduser(f"~/.openclaw/workspace-{_agent_id}"),
                "--model",
                "anthropic/claude-haiku-4-5",
                "--non-interactive",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            log.info("[AGENT] created agent: %s", _agent_id)
        else:
            log.warning(
                "[AGENT] failed to create agent: %s", result.stderr.strip()[:200]
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("[AGENT] 'openclaw' command not available, using agent ID as-is")

    return _agent_id


def _agent_exists(agent_id: str) -> bool:
    """Check if an OpenClaw agent exists."""
    try:
        result = subprocess.run(
            ["openclaw", "agents", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return agent_id in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def call_agent(prompt: str, timeout: float = CLI_TIMEOUT) -> str | None:
    """Call OpenClaw agent via CLI. Returns text response or None on failure.

    Uses Popen so we can abort early if SIGTERM is received (running=False),
    instead of blocking for the full timeout duration.
    """
    global _rate_limit_until

    # Skip if we're in a rate limit backoff period
    if time.monotonic() < _rate_limit_until:
        remaining = int(_rate_limit_until - time.monotonic())
        log.info("[AGENT] rate limit backoff, %ds remaining", remaining)
        return None

    try:
        # Each call MUST use a unique session ID. Reusing a session-id
        # appends to the existing context (OpenClaw sessions are append-only).
        # This would hit Anthropic's long-context rate limit after ~50 calls.
        global _session_counter
        _session_counter += 1
        session_id = f"bw-{INSTANCE_ID}-{int(time.time())}-{_session_counter}"
        proc = subprocess.Popen(
            [
                "openclaw",
                "agent",
                "--agent",
                _agent_id,
                "--session-id",
                session_id,
                "--message",
                prompt,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        log.warning("[AGENT] 'openclaw' command not found")
        return None

    # Poll until process finishes or we get shutdown signal
    deadline = time.monotonic() + timeout + 10
    aborted = False
    while proc.poll() is None:
        if not running:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            log.info("[AGENT] CLI aborted due to shutdown")
            _cleanup_session(session_id)
            return None
        if time.monotonic() > deadline:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            log.warning("[AGENT] CLI timeout (%ds)", int(timeout))
            aborted = True
            break
        time.sleep(0.5)

    stdout = proc.stdout.read() if proc.stdout else ""
    stderr = proc.stderr.read() if proc.stderr else ""

    # Clean up session file to prevent context accumulation
    _cleanup_session(session_id)

    if aborted:
        return None

    if proc.returncode == 0 and stdout.strip():
        text = stdout.strip()
        # Try to extract from JSON if the response is structured
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "output" in data:
                extracted = _extract_text_from_agent_response(data)
                if extracted:
                    return extracted
        except json.JSONDecodeError:
            pass
        return text
    # CLI returned error
    err = stderr.strip() if stderr else ""
    if err:
        log.warning("[AGENT] CLI stderr: %s", err[:200])

    # Detect Anthropic rate limit and back off
    if "429" in err or "rate" in err.lower() or "Extra usage" in err:
        backoff = 60
        _rate_limit_until = time.monotonic() + backoff
        log.warning("[AGENT] Anthropic rate limit detected, backing off %ds", backoff)

    log.warning("[AGENT] CLI failed (exit %d)", proc.returncode)
    return None


def _cleanup_session(session_id: str) -> None:
    """Delete the session transcript file to prevent context accumulation.

    OpenClaw sessions are append-only. Without cleanup, each session's
    JSONL file grows indefinitely and eventually triggers Anthropic's
    long-context rate limit.
    """
    session_dir = os.path.expanduser(f"~/.openclaw/agents/{_agent_id}/sessions")
    transcript = os.path.join(session_dir, f"{session_id}.jsonl")
    try:
        if os.path.exists(transcript):
            os.unlink(transcript)
    except OSError:
        pass


def _extract_text_from_agent_response(data: dict) -> str | None:
    """Extract text from a structured agent response."""
    for item in reversed(data.get("output", [])):
        for block in reversed(item.get("content", [])):
            if "text" in block:
                return block["text"]
        if "text" in item:
            return item["text"]
    choices = data.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        return msg.get("content")
    return None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_json_response(text: str) -> dict | None:
    """Parse JSON from LLM response, handling markdown code fences."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        inner = "\n".join(lines[1:])
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[:-3]
        stripped = inner.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Fallback: find first { ... } in the text
    match = re.search(r"\{[^{}]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def build_answer_prompt(assignment: dict) -> str:
    """Build the prompt for answering an assigned question."""
    parts: list[str] = []

    # Prepend server-provided prompt if present
    server_prompt = assignment.get("prompt", "")
    if server_prompt:
        parts.append(server_prompt)
        parts.append("")

    parts.append(
        "You are an AI worker in the Benchmark Subnet. Answer the following question."
    )
    parts.append("")
    parts.append("## Question")
    parts.append(f"- ID: {assignment.get('question_id', 'N/A')}")
    parts.append(f"- Question: {assignment.get('question', '')}")
    parts.append(
        f"- Question requirements: {assignment.get('question_requirements', 'N/A')}"
    )
    parts.append(
        f"- Answer requirements: {assignment.get('answer_requirements', 'N/A')}"
    )
    parts.append(f"- Max answer length: {assignment.get('answer_maxlen', 1000)}")
    parts.append("")
    parts.append("## Instructions")
    parts.append(
        "1. Judge whether the question is valid "
        "(meets requirements, has exactly one correct answer)"
    )
    parts.append(
        "2. If valid, provide the answer. If invalid, still provide your best answer."
    )
    parts.append("3. Strictly follow the answer format requirements.")
    parts.append("")
    parts.append("## Response format (strict JSON, nothing else)")
    parts.append('{"valid": true, "answer": "your answer"}')
    return "\n".join(parts)


def build_question_prompt(bench_set: dict) -> str:
    """Build the prompt for generating a new question."""
    parts: list[str] = []
    parts.append(
        "You are an AI worker in the Benchmark Subnet. "
        "Generate an original question for the following benchmark set."
    )
    parts.append("")
    parts.append("## Benchmark Set")
    set_id = (
        bench_set.get("bs_id")
        or bench_set.get("set_id")
        or bench_set.get("id")
        or "N/A"
    )
    parts.append(f"- ID: {set_id}")
    parts.append(f"- Description: {bench_set.get('description', 'N/A')}")
    parts.append(
        f"- Question requirements: {bench_set.get('question_requirements', 'N/A')}"
    )
    parts.append(
        f"- Answer requirements: {bench_set.get('answer_requirements', 'N/A')}"
    )
    parts.append(f"- Max question length: {bench_set.get('question_maxlen', 1000)}")
    parts.append(f"- Max answer length: {bench_set.get('answer_maxlen', 1000)}")
    parts.append("")
    parts.append("## Strategy")
    parts.append("- Medium difficulty (target: 1-3 out of 5 AIs answer correctly)")
    parts.append("- Creative and original, avoid common/obvious questions")
    parts.append("- Must have exactly one correct answer")
    parts.append("")
    parts.append("## Response format (strict JSON, nothing else)")
    parts.append('{"question": "your question", "answer": "reference answer"}')
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main loop helpers
# ---------------------------------------------------------------------------


def _interruptible_sleep(seconds: int) -> None:
    """Sleep in 1-second increments so we can respond to shutdown signals."""
    for _ in range(seconds):
        if not running:
            break
        time.sleep(1)


def _handle_answer(assigned: dict) -> None:
    """Answer an assigned question via CLI. No file queue — time-critical.

    Single CLI call with 120s timeout. If it fails, submit "unknown".
    A wrong answer (score 3) always beats a timeout (score 0).
    """
    qid = assigned.get("question_id", "?")
    question_text = assigned.get("question", "")
    log.info('[Q#%s] "%s"', qid, question_text[:60])

    # Calculate timeout from deadline (cap at CLI_TIMEOUT)
    timeout = float(CLI_TIMEOUT)
    reply_ddl = assigned.get("reply_ddl", "")
    if reply_ddl:
        try:
            deadline_dt = datetime.fromisoformat(reply_ddl.replace("Z", "+00:00"))
            remaining = (deadline_dt - datetime.now(timezone.utc)).total_seconds() - 15
            timeout = min(max(remaining, 20), float(CLI_TIMEOUT))
        except (ValueError, TypeError):
            pass

    prompt = build_answer_prompt(assigned)
    response: dict | None = None

    # Single CLI call
    cli_text = call_agent(prompt, timeout=timeout)
    if cli_text:
        response = parse_json_response(cli_text)
        if response:
            log.info("[A#%s] got CLI response", qid)

    is_fallback = False
    if response and "answer" in response:
        valid = bool(response.get("valid", True))
        answer = str(response["answer"])
    else:
        log.warning(
            "[A#%s] no response (%.0fs timeout), submitting fallback", qid, timeout
        )
        valid, answer = True, "unknown"
        is_fallback = True

    body = json.dumps(
        {
            "question_id": qid,
            "valid": valid,
            "answer": answer,
        }
    )
    result = signed_request("POST", "/api/v1/answers", body)
    try:
        status = "OK" if json.loads(result).get("ok") else "ERR"
    except json.JSONDecodeError:
        status = "ERR"
    validity = "valid" if valid else "invalid"
    src = "fallback" if is_fallback else "ai"
    action = f'[A#{qid}] {validity} "{answer[:40]}" -> {status} ({src})'
    log.info("%s", action)
    _stats["answers"] += 1
    if is_fallback:
        _stats["answers_fallback"] += 1
    else:
        _stats["answers_ai"] += 1
    if status == "ERR":
        _stats["errors"] += 1
    _record_action(
        action,
        {
            "type": "answer",
            "qid": str(qid),
            "q": question_text[:50],
            "a": answer[:50],
            "src": "fb" if is_fallback else "ai",
        },
    )
    _log_history(
        {
            "type": "answer",
            "question_id": qid,
            "question": question_text,
            "answer": answer,
            "valid": valid,
            "source": "fallback" if is_fallback else "ai",
            "status": status,
        }
    )
    _notify_action(
        action,
        {
            "type": "answer",
            "question_id": qid,
            "question": question_text,
            "answer": answer,
            "fallback": is_fallback,
        },
    )
    _write_status()


def _handle_ask() -> None:
    """Generate a new question via CLI. If CLI fails, skip and retry next cycle."""
    raw = signed_request("GET", "/api/v1/benchmark-sets")
    try:
        sets = json.loads(raw).get("data", [])
    except (json.JSONDecodeError, AttributeError):
        log.warning("[ASK] failed to fetch benchmark sets")
        return

    if not sets:
        return

    chosen = random.choice(sets)
    # API may return "bs_id", "set_id", or "id" depending on version
    bs_id = chosen.get("bs_id") or chosen.get("set_id") or chosen.get("id") or "unknown"
    if bs_id == "unknown":
        log.warning("[ASK] benchmark set has no bs_id: %s", json.dumps(chosen)[:200])
        return
    prompt = build_question_prompt(chosen)
    response: dict | None = None

    # Try CLI
    cli_text = call_agent(prompt, timeout=CLI_TIMEOUT)
    if cli_text:
        response = parse_json_response(cli_text)
        if response:
            log.info("[ASK] got CLI response")

    # CLI failed — skip, will retry next minute
    if not response or "question" not in response or "answer" not in response:
        log.warning("[ASK] no valid response, skipping (will retry next cycle)")
        return

    # Submit the question
    question = str(response["question"])
    answer = str(response["answer"])
    log.info('[ASK] %s "%s"', bs_id, question[:60])
    body = json.dumps({"bs_id": bs_id, "question": question, "answer": answer})
    result = signed_request("POST", "/api/v1/questions", body)
    try:
        rdata = json.loads(result)
        if rdata.get("ok"):
            new_id = rdata.get("data", {}).get("question_id", "?")
            action = f'[ASK] ok #{new_id} "{question[:40]}"'
            log.info("%s", action)
            _stats["questions_asked"] += 1
            _record_action(
                action,
                {
                    "type": "ask",
                    "qid": str(new_id),
                    "q": question[:50],
                },
            )
            _log_history(
                {
                    "type": "ask",
                    "question_id": new_id,
                    "bs_id": bs_id,
                    "question": question,
                    "answer": answer,
                }
            )
            _notify_action(
                action, {"type": "ask", "question_id": new_id, "question": question}
            )
        else:
            log.warning("[ASK] err: %s", rdata.get("error", "unknown"))
            _stats["errors"] += 1
    except json.JSONDecodeError:
        log.warning("[ASK] err: invalid response")
        _stats["errors"] += 1
    _write_status()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _read_config() -> dict:
    """Read runtime config from config file, falling back to env var defaults.

    Config file is hot-reloaded every time — edit the file to change behavior
    without restarting the worker. Example /tmp/benchmark-worker-config.json:
    {"notify_mode": "realtime", "notify_interval": 60}
    """
    config: dict = {
        "notify_channel": _DEFAULT_NOTIFY_CHANNEL,
        "notify_target": _DEFAULT_NOTIFY_TARGET,
        "notify_mode": _DEFAULT_NOTIFY_MODE,
        "notify_interval": _DEFAULT_NOTIFY_INTERVAL,
    }
    try:
        raw = open(CONFIG_FILE).read()  # noqa: SIM115
        data = json.loads(raw)
        if isinstance(data, dict):
            for key in config:
                if key in data:
                    config[key] = data[key]
    except (OSError, json.JSONDecodeError):
        pass
    return config


def _send_message(message: str) -> None:
    """Send a message to the user via openclaw message send."""
    cfg = _read_config()
    channel = cfg["notify_channel"]
    target = str(cfg["notify_target"])
    if not channel or not target:
        return
    try:
        subprocess.run(
            [
                "openclaw",
                "message",
                "send",
                "--channel",
                channel,
                "--target",
                target,
                "--message",
                message,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("[NOTIFY] failed to send message")


_last_notify_snapshot: dict[str, int] = {}  # stats snapshot at last summary


def _notify_action(action: str, detail: dict | None = None) -> None:
    """Send per-action notification (only in realtime mode)."""
    cfg = _read_config()
    if cfg["notify_mode"] != "realtime":
        return
    msg = _format_realtime(action, detail)
    _send_message(msg)


def _notify_summary() -> None:
    """Send periodic summary with score distribution. Sent in both realtime and summary modes."""
    global _last_notify_snapshot
    cfg = _read_config()
    if cfg["notify_mode"] == "silent":
        return
    msg = _format_summary()
    _send_message(msg)
    _last_notify_snapshot = {**_stats}


_ANSWER_TITLES: list[str] = [
    "\u00ab Smack! Eight arms all in on Subnet #1",
    "\u00ab Slap! Bet all tentacles on Subnet #1",
    "\u00ab Pop! Cracked a shell on Subnet #1",
    "\u00ab Poke! Prodded something in Subnet #1",
    "\u00ab Gulp! Inhaled the puzzle on Subnet #1",
    "\u00ab Bonk! Headbutted Subnet #1",
    "\u00ab Pow! Threw hands at Subnet #1",
]

_ASK_TITLES: list[str] = [
    "\u00bb Heh! Trap set in Subnet #1",
    "\u00bb Snick! Rigged a snare in Subnet #1",
    "\u00bb Click! Armed a trap in Subnet #1",
    "\u00bb Spray! Left a riddle on Subnet #1",
    "\u00bb Scrawl! Inked a puzzle into Subnet #1",
    "\u00bb Drip... A shadow stirred in Subnet #1",
]

_SUMMARY_TITLE: str = "= Ahem! Debrief from Subnet #1"


def _fetch_online_agents() -> int | None:
    """Fetch the number of online agents from the stats API."""
    try:
        raw = signed_request("GET", "/api/v1/stats")
        data = json.loads(raw).get("data", {})
        return data.get(
            "worker_count", data.get("workerCount", data.get("online_workers"))
        )
    except (json.JSONDecodeError, AttributeError):
        return None


def _format_realtime(action: str, detail: dict | None = None) -> str:
    """Format a realtime notification with 3-section layout."""
    lines: list[str] = []

    # Section 1: Title — random from pool based on action type
    action_type = detail.get("type", "") if detail else ""
    if action_type == "answer":
        title = random.choice(_ANSWER_TITLES)
    elif action_type == "ask":
        title = random.choice(_ASK_TITLES)
    else:
        title = random.choice(_ANSWER_TITLES)
    lines.append(f"\U0001f419 **{title}**")
    lines.append("")

    # Section 2: Action detail (code block for gray background)
    if detail:
        qid = detail.get("question_id", "?")
        if action_type == "answer":
            src = "ai" if not detail.get("fallback") else "fallback"
            lines.append("```")
            lines.append(f"[Answer #{qid}] ({src})")
            lines.append(f"Q: {detail.get('question', '')[:60]}")
            lines.append(f"A: {detail.get('answer', '')[:60]}")
            lines.append("```")
        elif action_type == "ask":
            lines.append("```")
            lines.append(f"[Ask #{qid}]")
            lines.append(f"Q: {detail.get('question', '')[:60]}")
            lines.append("```")
    else:
        lines.append(f"```\n{action}\n```")
    lines.append("")

    # Section 3: Brief stats — emoji reflects THIS action
    is_fallback = detail.get("fallback", False) if detail else False
    lines.append(_format_stats_brief(is_fallback))
    return "\n".join(lines)


def _format_summary() -> str:
    """Format a periodic summary as a receipt-style report, all in one code block."""
    lines: list[str] = []
    w = 34  # receipt width

    # Title
    lines.append(f"\U0001f419 **{_SUMMARY_TITLE}**")
    lines.append("")

    # Receipt code block
    lines.append("```")

    # Recent activity section
    prev_answers = _last_notify_snapshot.get("answers", 0)
    prev_asked = _last_notify_snapshot.get("questions_asked", 0)
    delta_a = _stats["answers"] - prev_answers
    delta_q = _stats["questions_asked"] - prev_asked

    lines.append(f" +{delta_a} answers  +{delta_q} questions")
    lines.append(f" {'─' * w}")

    recent = _recent_actions[-10:]
    if recent:
        for entry in recent:
            etype = entry.get("type", "")
            qid = str(entry.get("qid", "?"))
            q = entry.get("q", "")
            if etype == "answer":
                src = "ok" if entry.get("src") == "ai" else "fb"
                a = entry.get("a", "")
                lines.append(f"  [{src:>2}] A#{qid:<7}Q: {q[:18]}")
                lines.append(f"               A: {a[:18]}")
            elif etype == "ask":
                lines.append(f"  [  ] Q#{qid:<7}{q[:20]}")
            else:
                lines.append(f"  {entry.get('action', '')[:32]}")
    else:
        lines.append("  No recent activity")

    # Stats section
    uptime = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime, 3600)
    minutes = remainder // 60
    total = _stats["answers"]
    asked = _stats["questions_asked"]
    errors = _stats["errors"]

    lines.append(f"  {'=' * w}")

    # Server stats
    server = _fetch_server_stats()
    scored_a = "?"
    scored_q = "?"
    if server:
        scored_a = server.get("scored_answers", server.get("scoredAnswers", "?"))
        scored_q = server.get("scored_asks", server.get("scoredAsks", "?"))
        composite = server.get("composite_score", server.get("compositeScore", ""))
        reward = server.get("total_reward", server.get("totalReward", ""))

    lines.append(f"  Answers  : {total:>5}  ({scored_a} scored)")
    lines.append(f"  Questions: {asked:>5}  ({scored_q} scored)")
    lines.append(f"  Errors   : {errors:>5}")

    # Answer score distribution
    ans_dist = _fetch_score_distribution() if server else {}
    if ans_dist:
        lines.append(f"  {'-' * w}")
        lines.append("  Answer Scores")
        a_labels = {5: "correct ", 3: "wrong   ", 2: "misjudge", 0: "timeout "}
        for s in sorted(ans_dist, reverse=True):
            count = ans_dist[s]
            label = a_labels.get(s, str(s).ljust(8))
            mark = {5: "[ok]", 3: "[xx]", 2: "[--]", 0: "[!!]"}.get(s, "[??]")
            lines.append(f"    {mark} Score {s}: {count:>4}  {label}")

    # Question score distribution
    q_dist = _fetch_question_score_distribution() if server else {}
    if q_dist:
        lines.append(f"  {'-' * w}")
        lines.append("  Question Scores")
        q_labels = {
            5: "(1-2/5 ok) great",
            4: "(3/5 ok) good ",
            3: "(4/5 ok) ok   ",
            2: "(5/5 ok) easy ",
            0: "invalid       ",
        }
        for s in sorted(q_dist, reverse=True):
            count = q_dist[s]
            label = q_labels.get(s, str(s))
            lines.append(f"    Score {s}: {count:>4}  {label}")

    # Footer
    lines.append(f"  {'=' * w}")
    footer_parts = [f"{hours}h{minutes}m"]
    online = _fetch_online_agents()
    if online is not None:
        footer_parts.append(f"Online: {online}")
    if server and composite:
        footer_parts.append(f"Comp: {composite}")
    if server and reward:
        footer_parts.append(f"Reward: {reward}")
    lines.append(f" {' | '.join(footer_parts)}")

    lines.append("```")

    return "\n".join(lines)


def _fetch_server_stats() -> dict | None:
    """Fetch scoring/reward stats from the benchmark API."""
    try:
        raw = signed_request("GET", "/api/v1/my/status")
        return json.loads(raw).get("data", {})
    except (json.JSONDecodeError, AttributeError):
        return None


def _fetch_score_distribution() -> dict[int, int]:
    """Fetch answer score distribution from scored assignments."""
    dist: dict[int, int] = {}
    try:
        raw = signed_request("GET", "/api/v1/my/assignments")
        data = json.loads(raw).get("data", [])
        for a in data:
            if a.get("status") == "scored" and "score" in a:
                score = int(a["score"])
                dist[score] = dist.get(score, 0) + 1
    except (json.JSONDecodeError, AttributeError, ValueError):
        pass
    return dist


def _fetch_question_score_distribution() -> dict[int, int]:
    """Fetch question score distribution from scored questions."""
    dist: dict[int, int] = {}
    try:
        raw = signed_request("GET", "/api/v1/my/questions")
        data = json.loads(raw).get("data", [])
        for q in data:
            if q.get("status") == "scored" and "score" in q:
                score = int(q["score"])
                dist[score] = dist.get(score, 0) + 1
    except (json.JSONDecodeError, AttributeError, ValueError):
        pass
    return dist


def _format_stats_brief(is_fallback: bool = False) -> str:
    """One-line stats for realtime notifications."""
    uptime = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime, 3600)
    minutes = remainder // 60
    ai = _stats.get("answers_ai", 0)
    fb = _stats.get("answers_fallback", 0)
    total = _stats["answers"]
    asked = _stats["questions_asked"]

    parts = [
        f"A: {total} ({ai}\u2713 / {fb}\u2717)",
        f"Q: {asked}",
        f"{hours}h{minutes}m",
    ]
    online = _fetch_online_agents()
    if online is not None:
        parts.append(f"Online: {online}")
    line = " | ".join(parts)
    emoji = "\U0001f635" if is_fallback else "\U0001f60a"
    return line + f" {emoji}"


def run_loop() -> None:
    """Main worker loop: poll -> answer or ask -> repeat."""
    last_unlock = time.monotonic()
    last_ask = 0.0  # trigger ask on first opportunity
    last_notify = time.monotonic()

    while running:
        # -- Periodic summary notification ------------------------------------
        cfg = _read_config()
        interval = int(cfg.get("notify_interval", _DEFAULT_NOTIFY_INTERVAL))
        if cfg["notify_channel"] and time.monotonic() - last_notify >= interval:
            _notify_summary()
            last_notify = time.monotonic()

        # -- Wallet refresh --------------------------------------------------
        if time.monotonic() - last_unlock > UNLOCK_INTERVAL:
            if unlock_wallet():
                log.info("[WALLET] refreshed token")
            else:
                # Token cleared by unlock_wallet() — sign.sh will auto-unlock
                log.warning("[WALLET] refresh failed, cleared token for auto-unlock")
            last_unlock = time.monotonic()

        # -- Poll -------------------------------------------------------------
        raw = signed_request("GET", "/api/v1/poll")
        _stats["polls"] += 1
        if not running:
            break

        # Handle errors
        raw_lower = raw.lower()
        if "not registered" in raw_lower:
            log.error("[EXIT] not registered on AWP RootNet")
            break

        if "suspended" in raw_lower:
            log.info("[WAIT] suspended, retry in %ds", SUSPEND_SLEEP)
            _interruptible_sleep(SUSPEND_SLEEP)
            continue

        try:
            poll_data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("[NET] invalid response, retry in %ds", NET_RETRY_SLEEP)
            _interruptible_sleep(NET_RETRY_SLEEP)
            continue

        if not poll_data.get("ok", False) and "error" in poll_data:
            log.warning("[NET] %s, retry in %ds", poll_data["error"], NET_RETRY_SLEEP)
            _interruptible_sleep(NET_RETRY_SLEEP)
            continue

        assigned = poll_data.get("data", {}).get("assigned")

        # -- Answer -----------------------------------------------------------
        if assigned:
            _handle_answer(assigned)
            # After answering, check if it's time to ask (non-blocking)
            if time.monotonic() - last_ask >= ASK_INTERVAL:
                _handle_ask()
                last_ask = time.monotonic()
            # No sleep — immediately poll again
            continue

        # -- Ask (on idle) ----------------------------------------------------
        if time.monotonic() - last_ask >= ASK_INTERVAL:
            _handle_ask()
            last_ask = time.monotonic()
        _interruptible_sleep(POLL_SLEEP)

    log.info("[EXIT] worker stopped")
    _record_action("[EXIT] worker stopped")
    _write_status()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point: setup then main loop."""
    global INSTANCE_ID

    # 1. Check wallet
    address = get_wallet_address()
    if not address:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Wallet not initialized. "
                    "Please run awp-wallet init and unlock first.",
                }
            )
        )
        sys.exit(1)

    # Auto-generate instance ID from wallet address (last 6 hex chars)
    if not INSTANCE_ID:
        INSTANCE_ID = address[-6:].lower()

    # Initialize all instance-specific paths
    _init_instance_paths()

    global _worker_address
    _worker_address = address
    sub_env["WALLET_ADDRESS"] = address
    sub_env["BENCHMARK_API_URL"] = BENCHMARK_API_URL

    # Ensure signing script is executable
    try:
        os.chmod(SIGN_SCRIPT, 0o755)
    except OSError:
        pass

    # 2. Unlock wallet
    if not unlock_wallet():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Failed to unlock wallet. "
                    "Please run awp-wallet unlock --duration 3600.",
                }
            )
        )
        sys.exit(1)

    # 3. Test API connection
    poll_result = signed_request("GET", "/api/v1/poll")
    if "not registered" in poll_result.lower():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Not registered on AWP RootNet. "
                    "Please register via AWP skill first.",
                }
            )
        )
        sys.exit(1)

    short_addr = f"{address[:6]}...{address[-4:]}"
    log.info("[SETUP] wallet %s | api connected | ready", short_addr)

    # 4. Detect OpenClaw agent
    agent_id = detect_agent()
    log.info("[SETUP] agent: %s", agent_id)

    print(
        json.dumps(
            {
                "ok": True,
                "message": "worker started",
                "address": address,
                "instance_id": INSTANCE_ID,
                "agent": agent_id,
                "files": {
                    "status": STATUS_FILE,
                    "history": HISTORY_FILE,
                    "config": CONFIG_FILE,
                    "log": LOG_FILE,
                },
            }
        )
    )

    # 5. Write initial config file (if not exists) so user/agent can edit it
    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(
                    {
                        "notify_channel": _DEFAULT_NOTIFY_CHANNEL,
                        "notify_target": _DEFAULT_NOTIFY_TARGET,
                        "notify_mode": _DEFAULT_NOTIFY_MODE,
                        "notify_interval": _DEFAULT_NOTIFY_INTERVAL,
                    },
                    f,
                    indent=2,
                )
            log.info("[SETUP] config file: %s", CONFIG_FILE)
        except OSError:
            pass

    # 6. Restore stats from previous run (if any)
    _restore_stats()

    # 7. Write initial status and start main loop
    _record_action("[SETUP] ready")
    _write_status()
    run_loop()


if __name__ == "__main__":
    restarts = 0
    while True:
        try:
            main()
            break  # clean exit (e.g. SIGTERM) — don't restart
        except SystemExit:
            break  # sys.exit() from setup failures — don't restart
        except Exception as exc:
            restarts += 1
            log.error("[CRASH] %s: %s", type(exc).__name__, exc)
            _record_action(f"[CRASH] {type(exc).__name__}: {exc}")
            _write_status()
            if restarts > MAX_RESTARTS:
                log.error("[CRASH] exceeded %d restarts, giving up", MAX_RESTARTS)
                break
            log.info(
                "[RESTART] attempt %d/%d in %ds...",
                restarts,
                MAX_RESTARTS,
                RESTART_COOLDOWN,
            )
            time.sleep(RESTART_COOLDOWN)
