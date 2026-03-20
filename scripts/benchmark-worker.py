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
from pathlib import Path

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
ASK_EVERY_N: int = 6  # ask a question every N idle polls
TASK_WAIT_TIMEOUT: int = 180  # max seconds to wait for agent response (> cron interval)
TASK_POLL_INTERVAL: int = 2  # seconds between checks for agent response

STATUS_FILE: str = os.environ.get(
    "BENCHMARK_STATUS_FILE", "/tmp/benchmark-worker-status.json"
)
TASK_DIR: str = os.environ.get("BENCHMARK_TASK_DIR", "/tmp/benchmark-tasks")
OPENCLAW_AGENT: str = os.environ.get("OPENCLAW_AGENT", "")  # auto-detected at startup
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


def _ensure_task_dirs() -> None:
    """Create task queue directories if they don't exist."""
    Path(TASK_DIR, "pending").mkdir(parents=True, exist_ok=True)
    Path(TASK_DIR, "done").mkdir(parents=True, exist_ok=True)


def _write_task(task_id: str, task_data: dict) -> Path:
    """Write a task file to the pending directory."""
    task_data["id"] = task_id
    task_data["created_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    task_data["status"] = "pending"
    path = Path(TASK_DIR, "pending", f"{task_id}.json")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(task_data, indent=2))
    tmp.rename(path)
    log.info("[TASK] wrote %s", task_id)
    return path


def _wait_for_response(task_id: str, timeout: float) -> dict | None:
    """Wait for the agent to write a response file in the done directory."""
    done_path = Path(TASK_DIR, "done", f"{task_id}.json")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and running:
        if done_path.exists():
            try:
                raw = done_path.read_text()
                # Use parse_json_response to handle markdown fences etc.
                data = parse_json_response(raw)
                if data is None:
                    data = json.loads(raw)  # strict fallback
                done_path.unlink(missing_ok=True)
                Path(TASK_DIR, "pending", f"{task_id}.json").unlink(missing_ok=True)
                log.info("[TASK] got response for %s", task_id)
                return data
            except (json.JSONDecodeError, OSError) as e:
                log.warning("[TASK] failed to read response %s: %s", task_id, e)
                done_path.unlink(missing_ok=True)
                return None
        time.sleep(TASK_POLL_INTERVAL)
    # Timeout — clean up pending file
    log.warning("[TASK] timeout waiting for %s (%ds)", task_id, int(timeout))
    Path(TASK_DIR, "pending", f"{task_id}.json").unlink(missing_ok=True)
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
    """Answer an assigned question. Write task for agent, wait for response."""
    qid = assigned.get("question_id", "?")
    question_text = assigned.get("question", "")
    log.info('[Q#%s] "%s"', qid, question_text[:60])

    # Calculate timeout from deadline
    timeout = float(TASK_WAIT_TIMEOUT)
    reply_ddl = assigned.get("reply_ddl", "")
    if reply_ddl:
        try:
            deadline = datetime.fromisoformat(reply_ddl.replace("Z", "+00:00"))
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds() - 15
            timeout = min(max(remaining, 30), float(TASK_WAIT_TIMEOUT))
        except (ValueError, TypeError):
            pass

    prompt = build_answer_prompt(assigned)
    response: dict | None = None

    # Try 1: Direct CLI call (fast path)
    if _cli_available:
        cli_text = call_agent(prompt, timeout=min(timeout, CLI_TIMEOUT))
        if cli_text:
            response = parse_json_response(cli_text)
            if response:
                log.info("[A#%s] got CLI response", qid)

    # Try 2: File queue (fallback when CLI failed or disabled)
    if response is None:
        if not _cli_available:
            log.info("[A#%s] CLI unavailable, using file queue (cron mode)", qid)
        else:
            log.info(
                "[A#%s] CLI returned no valid response, falling back to file queue", qid
            )
        task_id = f"answer-{qid}-{int(time.time())}"
        _write_task(
            task_id,
            {
                "type": "answer",
                "question_id": qid,
                "prompt": prompt,
                "deadline": reply_ddl,
                "timeout_seconds": int(timeout),
            },
        )
        response = _wait_for_response(task_id, timeout)
        if response:
            log.info("[A#%s] got file queue response", qid)

    is_fallback = False
    if response and "answer" in response:
        valid = bool(response.get("valid", True))
        answer = str(response["answer"])
    else:
        # Fallback: wrong answer beats timeout
        log.warning(
            "[A#%s] no agent response (timeout %.0fs), submitting fallback",
            qid,
            timeout,
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
    """Generate and submit a new question. Write task for agent, wait for response."""
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

    # Try 1: Direct CLI call
    if _cli_available:
        cli_text = call_agent(prompt, timeout=CLI_TIMEOUT)
        if cli_text:
            response = parse_json_response(cli_text)
            if response:
                log.info("[ASK] got CLI response")

    # Try 2: File queue fallback
    if response is None:
        if not _cli_available:
            log.info("[ASK] CLI unavailable, using file queue (cron mode)")
        else:
            log.info("[ASK] CLI returned no valid response, falling back to file queue")
        task_id = f"ask-{bs_id}-{int(time.time())}"
        _write_task(
            task_id,
            {
                "type": "ask",
                "bs_id": bs_id,
                "prompt": prompt,
                "timeout_seconds": TASK_WAIT_TIMEOUT,
            },
        )
        response = _wait_for_response(task_id, TASK_WAIT_TIMEOUT)

    if not response or "question" not in response or "answer" not in response:
        log.warning("[ASK] no valid agent response, skipping")
        return

    question = str(response["question"])
    answer = str(response["answer"])
    log.info('[ASK] %s "%s"', bs_id, question[:60])

    body = json.dumps({"bs_id": bs_id, "question": question, "answer": answer})
    result = signed_request("POST", "/api/v1/questions", body)
    result_lower = result.lower()

    # Handle duplicate: retry once via same CLI-first strategy
    if "duplicate" in result_lower or "similar" in result_lower:
        log.info("[ASK] duplicate, retrying once")
        response2: dict | None = None
        if _cli_available:
            cli_text2 = call_agent(prompt, timeout=CLI_TIMEOUT)
            if cli_text2:
                response2 = parse_json_response(cli_text2)
        if response2 is None:
            task_id2 = f"ask-{bs_id}-{int(time.time())}-retry"
            _write_task(
                task_id2,
                {
                    "type": "ask",
                    "bs_id": bs_id,
                    "prompt": prompt,
                    "timeout_seconds": TASK_WAIT_TIMEOUT,
                },
            )
            response2 = _wait_for_response(task_id2, TASK_WAIT_TIMEOUT)
        if response2 and "question" in response2 and "answer" in response2:
            q2, a2 = str(response2["question"]), str(response2["answer"])
            body2 = json.dumps({"bs_id": bs_id, "question": q2, "answer": a2})
            result = signed_request("POST", "/api/v1/questions", body2)

    # Log result
    try:
        rdata = json.loads(result)
        if rdata.get("ok"):
            new_id = rdata.get("data", {}).get("question_id", "?")
            action = f"[ASK] ok #{new_id}"
            log.info("%s", action)
            _stats["questions_asked"] += 1
            _record_action(action)
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


def _cleanup_stale_files() -> None:
    """Remove stale done/ files that were written after the worker already timed out."""
    done_dir = Path(TASK_DIR, "done")
    if not done_dir.exists():
        return
    now = time.time()
    for f in done_dir.glob("*.json"):
        try:
            age = now - f.stat().st_mtime
            if age > 300:  # older than 5 minutes
                f.unlink(missing_ok=True)
                log.info("[CLEANUP] removed stale done file: %s", f.name)
        except OSError:
            pass
    # Also clean stale pending files (agent never picked them up)
    pending_dir = Path(TASK_DIR, "pending")
    if not pending_dir.exists():
        return
    for f in pending_dir.glob("*.json"):
        try:
            age = now - f.stat().st_mtime
            if age > 300:
                f.unlink(missing_ok=True)
                log.info("[CLEANUP] removed stale pending file: %s", f.name)
        except OSError:
            pass


def run_loop() -> None:
    """Main worker loop: poll -> answer or ask -> repeat."""
    counter = 0
    last_unlock = time.monotonic()
    last_cleanup = time.monotonic()
    last_cli_probe = time.monotonic()

    while running:
        # -- Periodic cleanup of stale task files ----------------------------
        if time.monotonic() - last_cleanup > 120:  # every 2 minutes
            _cleanup_stale_files()
            last_cleanup = time.monotonic()

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
            # No sleep — immediately poll again
            continue

        # -- Ask (on idle) ----------------------------------------------------
        counter += 1
        if counter % ASK_EVERY_N == 0:
            _handle_ask()
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

    # Ensure task queue directories exist
    _ensure_task_dirs()

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
        "[SETUP] agent: %s | CLI: %s | task dir: %s",
        agent_id,
        "available" if cli_ok else "unavailable (using file queue)",
        TASK_DIR,
    )
    if not cli_ok:
        global _cli_available
        _cli_available = False
        log.info("[SETUP] will use file queue with cron for LLM tasks")

    print(json.dumps({"ok": True, "message": "worker started", "address": address}))

    # 5. Write initial status and start main loop
    _record_action("[SETUP] ready")
    _write_status()
    run_loop()


if __name__ == "__main__":
    main()
