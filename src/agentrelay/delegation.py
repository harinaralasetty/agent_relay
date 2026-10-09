"""Small provider-neutral manager/worker qualification workflow."""

import json
import os
import secrets
from pathlib import Path

from agentrelay.store import MAX_MESSAGES

MANAGER = "manager"
CONVERSATION = "delegation"
TASKS = (
    (
        "worker-a",
        "Task A: add 2 and 3. Reply exactly 'Result A: 5.' "
        "with reply_to pointing to this assignment and final=false. Then return.",
        "Result A: 5.",
    ),
    (
        "worker-b",
        "Task B: multiply 4 and 5. Reply exactly 'Result B: 20.' "
        "with reply_to pointing to this assignment and final=false. Then return.",
        "Result B: 20.",
    ),
)
VERIFICATION = "Verified: A=5; B=20."
MESSAGE_COUNT = 2 * len(TASKS) + 1


def initialize_delegation(
    directory: Path,
    manager_runtime: str,
    codex_model: str,
    claude_model: str,
    *,
    max_messages: int = MESSAGE_COUNT,
) -> Path:
    """Create a private star topology without overwriting an existing config."""
    if manager_runtime not in {"codex", "claude"}:
        raise ValueError("Manager runtime must be codex or claude")
    if not 1 <= max_messages <= MAX_MESSAGES:
        raise ValueError("Invalid message limit")
    models = {"codex": codex_model, "claude": claude_model}
    worker_runtime = "codex" if manager_runtime == "claude" else "claude"
    workers = [worker for worker, _, _ in TASKS]
    peers = {
        MANAGER: {
            "runtime": manager_runtime,
            "model": models[manager_runtime],
            "token": secrets.token_urlsafe(32),
            "allowed": workers,
        }
    }
    for worker in workers:
        peers[worker] = {
            "runtime": worker_runtime,
            "model": models[worker_runtime],
            "token": secrets.token_urlsafe(32),
            "allowed": [MANAGER],
        }
    config = {
        "conversation_id": CONVERSATION,
        "max_messages": max_messages,
        "limits": {"max_turns": 6, "max_seconds": 600, "max_cost_usd": 1.0},
        "peers": peers,
    }
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "config.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(config, output, indent=2)
        output.write("\n")
    return path


def manager_prompt() -> str:
    assignments = " ".join(
        f"Send {worker} exactly {text!r}, conversation_id='{CONVERSATION}', "
        "final=false, reply_to omitted, "
        f"idempotency_key='{worker}-assignment'."
        for worker, text, _ in TASKS
    )
    return (
        f"You are the manager. The pre-registered conversation_id is '{CONVERSATION}'. "
        f"Use conversation_id='{CONVERSATION}' in EVERY peer_send call, including the final. "
        "Do not infer an ID from the inbox or create another ID. Call peer_list to discover "
        f"your two workers. {assignments} Send both assignments in your initial turn, "
        "then return. In later turns read and ACK worker results. Results can arrive in "
        "either order; remember each result's ID. Verify both computations yourself: "
        "2+3=5 and 4*5=20. Do not close on the first result. If either result is wrong, "
        "return a failure explanation without closing. Only after BOTH correct results "
        f"have arrived and been ACKed, send worker-a exactly {VERIFICATION!r}, "
        "reply_to pointing to worker-a's result, final=true, "
        "idempotency_key='verification'. Worker-a must ACK the final and stop. "
        "Worker-b is done after sending its result. Do not send any other messages."
    )


def verify_delegation(messages: list[dict]) -> bool:
    """Check results before closure and eventual ACKs, not ACK timing or reasoning."""
    if len(messages) != MESSAGE_COUNT:
        return False
    ids = [message["id"] for message in messages]
    if len(set(ids)) != len(ids):
        return False
    if any(
        message["conversation_id"] != CONVERSATION
        or not message["acknowledged"]
        or message["status"] not in {"stored", "completed"}
        or message["final"] != (index == MESSAGE_COUNT - 1)
        for index, message in enumerate(messages)
    ):
        return False
    results = {}
    for worker, assignment_text, result_text in TASKS:
        assignments = [
            (index, message)
            for index, message in enumerate(messages)
            if message["sender"] == MANAGER
            and message["recipient"] == worker
            and message["text"] == assignment_text
            and message["reply_to"] is None
        ]
        replies = [
            (index, message)
            for index, message in enumerate(messages)
            if message["sender"] == worker
            and message["recipient"] == MANAGER
            and message["text"] == result_text
        ]
        if len(assignments) != 1 or len(replies) != 1:
            return False
        assignment_index, assignment = assignments[0]
        reply_index, reply = replies[0]
        if reply_index <= assignment_index or reply["reply_to"] != assignment["id"]:
            return False
        results[worker] = reply
    final = messages[-1]
    return (
        final["sender"] == MANAGER
        and final["recipient"] == TASKS[0][0]
        and final["text"] == VERIFICATION
        and final["reply_to"] == results[TASKS[0][0]]["id"]
    )
