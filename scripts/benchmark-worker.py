#!/usr/bin/env python3
"""Standalone benchmark worker — polls, answers, and asks in a loop.

Delegates signing to benchmark-sign.sh.
When LLM reasoning is needed, writes task files to a queue directory.
An external agent (OpenClaw) processes those tasks on a cron schedule.
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

BENCHMARK_API_URL: str = os.environ.get("BENCHMARK_API_URL", "https://tapis1.awp.sh")

SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
SIGN_SCRIPT: str = os.path.join(SCRIPT_DIR, "benchmark-sign.sh")
BENCHMARK_API_URL = BENCHMARK_API_URL.rstrip("/")

POLL_SLEEP: int = 5  # seconds between idle polls
NET_RETRY_SLEEP: int = 10  # seconds after network error
SUSPEND_SLEEP: int = 60  # seconds when suspended
UNLOCK_INTERVAL: int = 25 * 60  # re-unlock every 25 minutes
ASK_INTERVAL: int = 60  # seconds between question submissions (API rate limit: 1/min)
ANSWER_CLI_TIMEOUT: int = 120  # seconds for CLI answering (single attempt)
STATUS_FILE: str = os.environ.get(
    "BENCHMARK_STATUS_FILE", "/tmp/benchmark-worker-status.json"
)
OPENCLAW_AGENT: str = os.environ.get("OPENCLAW_AGENT", "")  # auto-detected at startup
NOTIFY_CHANNEL: str = os.environ.get("NOTIFY_CHANNEL", "")  # e.g. "telegram"
NOTIFY_TARGET: str = os.environ.get("NOTIFY_TARGET", "")  # e.g. chat_id
NOTIFY_INTERVAL: int = int(
    os.environ.get("NOTIFY_INTERVAL", "300")
)  # seconds between notifications
CLI_TIMEOUT: int = 120  # max seconds for a single CLI call

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
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


def _write_status() -> None:
    """Write current worker status to a JSON file for external monitoring."""
    status = {
        "running": running,
        "pid": os.getpid(),
        "uptime_seconds": int(time.monotonic() - _start_time),
        "address": _worker_address,
        "stats": {**_stats},
        "last_action": _last_action,
        "last_action_at": _last_action_at,
    }
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f, indent=2)
        os.replace(tmp, STATUS_FILE)
    except OSError as e:
        log.warning("[STATUS] failed to write status file: %s", e)


def _record_action(action: str) -> None:
    """Record the latest action for status reporting."""
    global _last_action, _last_action_at
    _last_action = action
    _last_action_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    """Unlock the wallet for 3600s and cache the session token."""
    try:
        result = subprocess.run(
            ["awp-wallet", "unlock", "--duration", "3600"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            log.warning("[WALLET] unlock failed: %s", result.stderr.strip())
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
    """Execute a signed API request via benchmark-sign.sh. Returns raw stdout."""
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
        return result.stdout
    except subprocess.TimeoutExpired:
        return '{"ok":false,"error":"sign request timeout"}'
    except FileNotFoundError:
        return '{"ok":false,"error":"benchmark-sign.sh not found"}'


# ---------------------------------------------------------------------------
# OpenClaw agent: direct CLI call (preferred) with file queue fallback
# ---------------------------------------------------------------------------

_agent_id: str = ""  # detected at startup
_cli_available: bool = True  # set to False after repeated failures
_cli_fail_count: int = 0
_CLI_FAIL_THRESHOLD: int = 3  # disable CLI after this many consecutive failures


def detect_agent() -> str:
    """Detect available OpenClaw agent ID at startup."""
    global _agent_id

    # If explicitly set via env, use that
    if OPENCLAW_AGENT:
        _agent_id = OPENCLAW_AGENT
        log.info("[AGENT] using configured agent: %s", _agent_id)
        return _agent_id

    # Try openclaw agents list
    try:
        result = subprocess.run(
            ["openclaw", "agents", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Parse agent list — look for first agent ID
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line or line.startswith(("-", "#", "NAME", "name")):
                    continue
                # Try JSON format
                try:
                    data = json.loads(line)
                    if isinstance(data, list) and data:
                        _agent_id = str(data[0].get("id", data[0].get("name", "main")))
                        log.info("[AGENT] detected agent: %s", _agent_id)
                        return _agent_id
                except json.JSONDecodeError:
                    pass
                # Try plain text: first word is agent ID
                agent_id = line.split()[0].strip()
                if agent_id:
                    _agent_id = agent_id
                    log.info("[AGENT] detected agent: %s", _agent_id)
                    return _agent_id
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Default to "main"
    _agent_id = "main"
    log.info("[AGENT] defaulting to agent: %s", _agent_id)
    return _agent_id


def _probe_cli() -> bool:
    """Quick probe to check if openclaw agent CLI is responsive."""
    try:
        result = subprocess.run(
            ["openclaw", "agent", "--agent", _agent_id, "--message", "ping"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def call_agent(prompt: str, timeout: float = CLI_TIMEOUT) -> str | None:
    """Call OpenClaw agent via CLI. Returns text response or None on failure.

    This is the fast path — direct synchronous call, no file queue.
    """
    global _cli_available, _cli_fail_count

    if not _cli_available:
        return None

    try:
        result = subprocess.run(
            ["openclaw", "agent", "--agent", _agent_id, "--message", prompt],
            capture_output=True,
            text=True,
            timeout=int(timeout) + 10,
        )
        if result.returncode == 0 and result.stdout.strip():
            _cli_fail_count = 0  # reset on success
            text = result.stdout.strip()
            # Try to extract from JSON if the response is structured
            try:
                data = json.loads(text)
                # If it's a structured response, return the text content
                if isinstance(data, dict) and "output" in data:
                    extracted = _extract_text_from_agent_response(data)
                    if extracted:
                        return extracted
            except json.JSONDecodeError:
                pass
            return text
        # CLI returned error
        _cli_fail_count += 1
        if result.stderr.strip():
            log.warning("[AGENT] CLI stderr: %s", result.stderr.strip()[:200])
        log.warning(
            "[AGENT] CLI failed (exit %d, fail %d/%d)",
            result.returncode,
            _cli_fail_count,
            _CLI_FAIL_THRESHOLD,
        )
    except subprocess.TimeoutExpired:
        _cli_fail_count += 1
        log.warning(
            "[AGENT] CLI timeout (fail %d/%d)",
            _cli_fail_count,
            _CLI_FAIL_THRESHOLD,
        )
    except FileNotFoundError:
        log.warning("[AGENT] 'openclaw' command not found, disabling CLI")
        _cli_available = False
        return None

    # Disable CLI after too many consecutive failures
    if _cli_fail_count >= _CLI_FAIL_THRESHOLD:
        log.warning(
            "[AGENT] CLI disabled after %d consecutive failures", _cli_fail_count
        )
        _cli_available = False
    return None


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


def re_enable_cli() -> None:
    """Periodically try to re-enable CLI if it was disabled."""
    global _cli_available, _cli_fail_count
    if _cli_available:
        return
    log.info("[AGENT] probing CLI availability...")
    if _probe_cli():
        _cli_available = True
        _cli_fail_count = 0
        log.info("[AGENT] CLI re-enabled")
    else:
        log.info("[AGENT] CLI still unavailable, using file queue")


# ---------------------------------------------------------------------------
# Task queue: file-based communication with OpenClaw agent (fallback)
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
    parts.append(f"- ID: {bench_set.get('bs_id', 'N/A')}")
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

    # Calculate timeout from deadline (cap at ANSWER_CLI_TIMEOUT)
    timeout = float(ANSWER_CLI_TIMEOUT)
    reply_ddl = assigned.get("reply_ddl", "")
    if reply_ddl:
        try:
            deadline_dt = datetime.fromisoformat(reply_ddl.replace("Z", "+00:00"))
            remaining = (deadline_dt - datetime.now(timezone.utc)).total_seconds() - 15
            timeout = min(max(remaining, 20), float(ANSWER_CLI_TIMEOUT))
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
    _record_action(action)
    _write_status()


def _handle_ask() -> None:
    """Generate a new question. Non-blocking: tries CLI, then writes to file queue.

    Unlike answering (time-critical), question generation is not urgent.
    If CLI fails, we write the task and move on — cron will handle it.
    The cron agent processes the task and submits the question directly.
    """
    raw = signed_request("GET", "/api/v1/benchmark-sets")
    try:
        sets = json.loads(raw).get("data", [])
    except (json.JSONDecodeError, AttributeError):
        log.warning("[ASK] failed to fetch benchmark sets")
        return

    if not sets:
        return

    chosen = random.choice(sets)
    bs_id = chosen.get("bs_id", "unknown")
    prompt = build_question_prompt(chosen)
    response: dict | None = None

    # Try CLI first (fast, synchronous)
    if _cli_available:
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
            log.info("[ASK] ok #%s", new_id)
            _stats["questions_asked"] += 1
            _record_action(f"[ASK] ok #{new_id}")
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


def _notify_user(message: str) -> None:
    """Send a notification to the user via openclaw message send."""
    if not NOTIFY_CHANNEL or not NOTIFY_TARGET:
        return
    try:
        subprocess.run(
            [
                "openclaw",
                "message",
                "send",
                "--channel",
                NOTIFY_CHANNEL,
                "--target",
                NOTIFY_TARGET,
                "--message",
                message,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("[NOTIFY] failed to send notification")


def _build_status_summary() -> str:
    """Build a one-line status summary for notifications."""
    uptime = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime, 3600)
    minutes = remainder // 60
    ai = _stats.get("answers_ai", 0)
    fb = _stats.get("answers_fallback", 0)
    total = _stats["answers"]
    asked = _stats["questions_asked"]
    errors = _stats["errors"]
    parts = [
        f"Answers: {total} ({ai} ai / {fb} fallback)",
        f"Questions: {asked}",
        f"Errors: {errors}",
        f"Uptime: {hours}h {minutes}m",
    ]
    if total > 0 and fb > ai:
        parts.append("⚠ high fallback ratio")
    return "[Benchmark] " + " | ".join(parts)


def run_loop() -> None:
    """Main worker loop: poll -> answer or ask -> repeat."""
    last_unlock = time.monotonic()
    last_cli_probe = time.monotonic()
    last_ask = 0.0  # trigger ask on first opportunity
    last_notify = time.monotonic()

    while running:
        # -- Periodic notification to user -----------------------------------
        if NOTIFY_CHANNEL and time.monotonic() - last_notify >= NOTIFY_INTERVAL:
            _notify_user(_build_status_summary())
            last_notify = time.monotonic()

        # -- Periodically try to re-enable CLI if disabled -------------------
        if time.monotonic() - last_cli_probe > 300:  # every 5 minutes
            re_enable_cli()
            last_cli_probe = time.monotonic()

        # -- Wallet refresh --------------------------------------------------
        if time.monotonic() - last_unlock > UNLOCK_INTERVAL:
            if unlock_wallet():
                log.info("[WALLET] refreshed")
            else:
                log.warning("[WALLET] refresh failed, continuing with current token")
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
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "Not registered on AWP RootNet. "
                        "Please register via AWP skill first.",
                    }
                )
            )
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

    # 4. Detect OpenClaw agent and probe CLI
    agent_id = detect_agent()
    cli_ok = _probe_cli()
    log.info(
        "[SETUP] agent: %s | CLI: %s",
        agent_id,
        "available" if cli_ok else "unavailable",
    )
    if not cli_ok:
        global _cli_available
        _cli_available = False
        log.warning("[SETUP] CLI unavailable — answers will fallback to 'unknown'")

    print(json.dumps({"ok": True, "message": "worker started", "address": address}))

    # 5. Write initial status and start main loop
    _record_action("[SETUP] ready")
    _write_status()
    run_loop()


if __name__ == "__main__":
    main()
