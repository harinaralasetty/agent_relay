"""Initialize a private local workspace, run peers, and inspect a conversation."""

import argparse
import asyncio
import json
import os
import secrets
import sys
from dataclasses import replace
from pathlib import Path

from peercourier.adapters.claude import ClaudeRuntime
from peercourier.adapters.codex import CodexRuntime
from peercourier.runtime import RuntimeConfig
from peercourier.store import Store, identifier
from peercourier.supervisor import Supervisor

DEFAULT_CODEX_MODEL = "gpt-6-luna"
DEFAULT_CLAUDE_MODEL = "haiku"
RUNTIMES = {"codex": CodexRuntime, "claude": ClaudeRuntime}
DEMO_TEXTS = [
    "Please ask me 'Which two numbers should I add?' Then add the two numbers I provide "
    "and reply using 'The sum is <number>.'",
    "Which two numbers should I add?",
    "Add 2 and 3.",
    "The sum is 5.",
    "Verified: 5.",
]


def initialize(directory: Path, codex_model: str, claude_model: str) -> Path:
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "config.json"
    config = {
        "conversation_id": "demo",
        "max_messages": 8,
        "limits": {"max_turns": 6, "max_seconds": 600, "max_cost_usd": 1.0},
        "peers": {
            "cedar": {
                "runtime": "codex",
                "model": codex_model,
                "token": secrets.token_urlsafe(32),
                "allowed": ["birch"],
            },
            "birch": {
                "runtime": "claude",
                "model": claude_model,
                "token": secrets.token_urlsafe(32),
                "allowed": ["cedar"],
            },
        },
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(config, output, indent=2)
        output.write("\n")
    return path


def load(path: Path) -> tuple[dict, Store]:
    config = json.loads(path.read_text())
    peers = config.get("peers")
    if not isinstance(peers, dict) or len(peers) < 2:
        raise ValueError("Configure at least two peers")
    identifier(config["conversation_id"])
    for peer, definition in peers.items():
        identifier(peer)
        if definition.get("runtime") not in RUNTIMES:
            raise ValueError("Unknown runtime; supported runtime adapters: codex, claude")
        if not definition.get("model") or not definition.get("token"):
            raise ValueError("Every peer needs a model and a private token")
        for target in definition["allowed"]:
            if target not in peers:
                raise ValueError("Allowed recipient does not exist")
    store = Store(path.parent / "mail.sqlite")
    for peer, definition in peers.items():
        store.register(peer, definition["token"], definition["allowed"])
    store.conversation(config["conversation_id"], list(peers), config.get("max_messages", 12))
    return config, store


def make_runtimes(config: dict, store: Store) -> dict:
    runtimes = {}
    for peer, definition in config["peers"].items():
        workspace = store.path.parent / "workspaces" / peer
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        with store.connect() as db:
            previous = db.execute("SELECT session_id FROM peers WHERE id=?", (peer,)).fetchone()
        options = RuntimeConfig(
            peer_id=peer,
            model=definition["model"],
            cwd=str(workspace),
            mcp_command=sys.executable,
            mcp_args=["-m", "peercourier.mcp_server"],
            mcp_env={
                "PEERCOURIER_DB": str(store.path),
                "PEERCOURIER_PEER": peer,
                "PEERCOURIER_TOKEN": definition["token"],
            },
            timeout=definition.get("timeout", 90),
            effort="low",
            max_budget_usd=config.get("limits", {}).get("max_cost_usd", 1.0),
            executable=definition.get("executable"),
        )
        if previous["session_id"]:
            options = replace(options, resume_session=previous["session_id"])
        runtimes[peer] = RUNTIMES[definition["runtime"]](options)
    return runtimes


def demo_prompt(config: dict, initiator: str) -> str:
    target = next(peer for peer in config["peers"] if peer != initiator)
    return (
        f"In conversation {config['conversation_id']}, send {target} this small test request: "
        f"{DEMO_TEXTS[0]!r} Use idempotency_key='start'. "
        "When you receive the clarification, reply exactly 'Add 2 and 3.' with its reply_to ID. "
        "When you receive 'The sum is 5.', reply exactly 'Verified: 5.' "
        "with final=true. The other peer should acknowledge the final and stop. "
        "Use explicit tools on each step; keep all prose and messages brief."
    )


async def run(path: Path, initial: dict[str, str] | None, watch: bool = False) -> dict:
    config, store = load(path)
    supervisor = Supervisor(store, make_runtimes(config, store), **config.get("limits", {}))
    if watch:
        return await supervisor.run_forever(initial)
    return await supervisor.run_until_idle(initial)


def verify_demo(messages: list[dict]) -> bool:
    """A successful demo proves the whole exchange, not just a final message send."""
    if len(messages) != 5 or not messages[-1]["final"]:
        return False
    for index, message in enumerate(messages):
        if message["status"] != "completed" or not message["acknowledged"]:
            return False
        if message["text"].strip() != DEMO_TEXTS[index]:
            return False
        if index:
            parent = messages[index - 1]
            if (
                message["reply_to"] != parent["id"]
                or message["sender"] != parent["recipient"]
                or message["recipient"] != parent["sender"]
                or message["conversation_id"] != parent["conversation_id"]
            ):
                return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="PeerCourier local agent conversations")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create private local config (never overwrites)")
    init.add_argument("directory", type=Path)
    init.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL)
    init.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)
    inspect = commands.add_parser("inspect", help="Print administrative mailbox transcript")
    inspect.add_argument("config", type=Path)
    resolve = commands.add_parser("resolve", help="Record reviewed uncertain outcome; never retry")
    resolve.add_argument("config", type=Path)
    resolve.add_argument("message_id")
    resolve.add_argument("--outcome", choices=["completed", "failed"], required=True)
    execute = commands.add_parser("run", help="Deliver stored messages, or initiate one peer")
    execute.add_argument("config", type=Path)
    execute.add_argument("--peer")
    execute.add_argument("--prompt")
    execute.add_argument("--watch", action="store_true")
    demo = commands.add_parser("demo", help="Small live two-way conversation (uses model quota)")
    demo.add_argument("directory", type=Path)
    demo.add_argument("--initiator", choices=["cedar", "birch"], default="cedar")
    demo.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL)
    demo.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)
    commands.add_parser("mcp", help="Run per-peer stdio MCP using PEERCOURIER_* environment")
    args = parser.parse_args()
    try:
        if args.command == "mcp":
            from peercourier.mcp_server import main as mcp_main

            mcp_main()
        elif args.command == "init":
            print(initialize(args.directory, args.codex_model, args.claude_model))
        elif args.command == "inspect":
            config, store = load(args.config)
            print(json.dumps(store.messages(config["conversation_id"]), indent=2))
        elif args.command == "resolve":
            _, store = load(args.config)
            store.resolve(args.message_id, args.outcome)
            print(json.dumps({"message_id": args.message_id, "resolved": args.outcome}))
        elif args.command == "demo":
            path = initialize(args.directory, args.codex_model, args.claude_model)
            config, store = load(path)
            report = asyncio.run(run(path, {args.initiator: demo_prompt(config, args.initiator)}))
            report["messages"] = store.messages(config["conversation_id"])
            print(json.dumps(report, indent=2))
            if report["errors"] or not verify_demo(report["messages"]):
                raise SystemExit(1)
        else:
            if bool(args.peer) != bool(args.prompt):
                parser.error("Use --peer and --prompt together")
            initial = {args.peer: args.prompt} if args.peer else None
            report = asyncio.run(run(args.config, initial, args.watch))
            print(json.dumps(report, indent=2))
            if report["errors"]:
                raise SystemExit(1)
    except (ValueError, OSError) as exc:
        print(f"PeerCourier: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
