#!/usr/bin/env python3
"""Standalone benchmark worker — polls, answers, and asks in a loop.

Delegates signing to benchmark-sign.sh, LLM reasoning to local OpenClaw.
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

import requests

# ---------------------------------------------------------------------------
# Configuration (environment variables with defaults)
# ---------------------------------------------------------------------------

BENCHMARK_API_URL: str = os.environ.get("BENCHMARK_API_URL", "https://tapis1.awp.sh")
OPENCLAW_URL: str = os.environ.get("OPENCLAW_URL", "http://127.0.0.1:18789")
OPENCLAW_TOKEN: str = os.environ.get("OPENCLAW_TOKEN", "")
OPENCLAW_AGENT_ID: str = os.environ.get("OPENCLAW_AGENT_ID", "main")

SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
SIGN_SCRIPT: str = os.path.join(SCRIPT_DIR, "benchmark-sign.sh")
BENCHMARK_API_URL = BENCHMARK_API_URL.rstrip("/")

POLL_SLEEP: int = 5  # seconds between idle polls
NET_RETRY_SLEEP: int = 10  # seconds after network error
SUSPEND_SLEEP: int = 60  # seconds when suspended
UNLOCK_INTERVAL: int = 25 * 60  # re-unlock every 25 minutes
ASK_EVERY_N: int = 6  # ask a question every N idle polls
STATUS_FILE: str = os.environ.get(
    "BENCHMARK_STATUS_FILE", "/tmp/benchmark-worker-status.json"
)

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
    _last_action_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
        match = re.search(r'"token":"([^"]+)"', result.stdout)
        if match:
            sub_env["AWP_SESSION_TOKEN"] = match.group(1)
            return True
        # Fallback: if output is just a raw token string
        token = result.stdout.strip()
        if token:
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
# OpenClaw integration
# ---------------------------------------------------------------------------


def call_openclaw(prompt: str, timeout: float = 120) -> str | None:
    """Call local OpenClaw /v1/responses and return the text output, or None."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if OPENCLAW_TOKEN:
        headers["Authorization"] = f"Bearer {OPENCLAW_TOKEN}"
    headers["x-openclaw-agent-id"] = OPENCLAW_AGENT_ID

    payload = {"model": "openclaw", "input": prompt}
    try:
        resp = requests.post(
            f"{OPENCLAW_URL}/v1/responses",
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        return extract_text_from_response(resp.json())
    except requests.RequestException as e:
        log.warning("[OPENCLAW] request failed: %s", e)
        return None


def extract_text_from_response(data: dict) -> str | None:
    """Walk OpenClaw response JSON to find the text output."""
    # Try output array (OpenAI Responses API format)
    for item in reversed(data.get("output", [])):
        # item with content array
        for block in reversed(item.get("content", [])):
            if block.get("type") == "output_text" and "text" in block:
                return block["text"]
            if "text" in block:
                return block["text"]
        # item with direct text
        if "text" in item:
            return item["text"]
    # Fallback: choices array (Chat Completions format)
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


def parse_answer_response(text: str) -> tuple[bool, str]:
    """Parse answer from LLM response. Returns (valid, answer)."""
    data = parse_json_response(text)
    if data and "answer" in data:
        return bool(data.get("valid", True)), str(data["answer"])
    log.warning("[PARSE] failed to parse answer JSON, using raw text")
    return True, text.strip()[:1000]


def parse_question_response(text: str) -> tuple[str, str] | None:
    """Parse question from LLM response. Returns (question, answer) or None."""
    data = parse_json_response(text)
    if data and "question" in data and "answer" in data:
        return str(data["question"]), str(data["answer"])
    log.warning("[PARSE] failed to parse question JSON")
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
        "1. Judge whether the question is valid (meets requirements, has exactly one correct answer)"
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
        "You are an AI worker in the Benchmark Subnet. Generate an original question for the following benchmark set."
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
    """Answer an assigned question with deadline awareness."""
    qid = assigned.get("question_id", "?")
    question_text = assigned.get("question", "")
    log.info('[Q#%s] "%s"', qid, question_text[:60])

    # Calculate timeout from deadline
    timeout = 120.0
    reply_ddl = assigned.get("reply_ddl", "")
    if reply_ddl:
        try:
            deadline = datetime.fromisoformat(reply_ddl.replace("Z", "+00:00"))
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds() - 15
            timeout = min(max(remaining, 30), 300)
        except (ValueError, TypeError):
            pass

    prompt = build_answer_prompt(assigned)
    llm_text = call_openclaw(prompt, timeout=timeout)

    if llm_text is not None:
        valid, answer = parse_answer_response(llm_text)
    else:
        # Fallback: wrong answer beats timeout
        log.warning("[A#%s] OpenClaw timeout/error, submitting fallback", qid)
        valid, answer = True, "unknown"

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
    action = f'[A#{qid}] {validity} "{answer[:40]}" -> {status}'
    log.info("%s", action)
    _stats["answers"] += 1
    if status == "ERR":
        _stats["errors"] += 1
    _record_action(action)
    _write_status()


def _handle_ask() -> None:
    """Generate and submit a new question."""
    try:
        resp = requests.get(
            f"{BENCHMARK_API_URL}/api/v1/benchmark-sets",
            timeout=10,
        )
        resp.raise_for_status()
        sets = resp.json().get("data", [])
    except requests.RequestException as e:
        log.warning("[ASK] failed to fetch benchmark sets: %s", e)
        return

    if not sets:
        return

    chosen = random.choice(sets)
    bs_id = chosen.get("bs_id", "unknown")

    prompt = build_question_prompt(chosen)
    llm_text = call_openclaw(prompt)
    if llm_text is None:
        log.warning("[ASK] OpenClaw failed, skipping")
        return

    parsed = parse_question_response(llm_text)
    if parsed is None:
        return

    question, answer = parsed
    log.info('[ASK] %s "%s"', bs_id, question[:60])

    body = json.dumps({"bs_id": bs_id, "question": question, "answer": answer})
    result = signed_request("POST", "/api/v1/questions", body)
    result_lower = result.lower()

    # Handle duplicate: regenerate once
    if "duplicate" in result_lower or "similar" in result_lower:
        log.info("[ASK] duplicate, retrying once")
        llm_text2 = call_openclaw(prompt)
        if llm_text2 is not None:
            parsed2 = parse_question_response(llm_text2)
            if parsed2 is not None:
                q2, a2 = parsed2
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


def run_loop() -> None:
    """Main worker loop: poll -> answer or ask -> repeat."""
    counter = 0
    last_unlock = time.monotonic()

    while running:
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
                        "error": "Not registered on AWP RootNet. Please register via AWP skill first.",
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
        if counter % ASK_EVERY_N == 0:
            _handle_ask()

        counter += 1
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
                    "error": "Wallet not initialized. Please run awp-wallet init and unlock first.",
                }
            )
        )
        sys.exit(1)

    global _worker_address
    _worker_address = address
    sub_env["WALLET_ADDRESS"] = address
    sub_env["BENCHMARK_API_URL"] = BENCHMARK_API_URL

    # 2. Unlock wallet
    if not unlock_wallet():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Failed to unlock wallet. Please run awp-wallet unlock --duration 3600.",
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
                    "error": "Not registered on AWP RootNet. Please register via AWP skill first.",
                }
            )
        )
        sys.exit(1)

    short_addr = f"{address[:6]}...{address[-4:]}"
    log.info("[SETUP] wallet %s | api connected | ready", short_addr)
    print(json.dumps({"ok": True, "message": "worker started", "address": address}))

    # 4. Write initial status and start main loop
    _record_action("[SETUP] ready")
    _write_status()
    run_loop()


if __name__ == "__main__":
    main()
