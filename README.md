# PeerCourier

Durable local conversations between agents from different runtimes.

PeerCourier gives agents a shared mailbox through MCP and wakes managed peers to
process incoming messages. Both sides can initiate, ask clarifying questions,
reply, and keep the same provider session. Ordinary assistant prose stays local;
only explicit messages are routed.

The mailbox and runtime interface are provider-neutral. This first release includes
Codex and Claude Code adapters. It manages its own sessions; attaching arbitrary
desktop chats or native subagent trees is outside this release.

## Quick start

Requires macOS or Linux, Python 3.12+, [uv](https://docs.astral.sh/uv/), and authenticated
local installations of both [Codex CLI](https://learn.chatgpt.com/codex) and
[Claude Code](https://code.claude.com/docs/en/overview). Choose models available in
your account. No provider credentials are stored in this repository.

```sh
git clone https://github.com/harinaralasetty/peercourier.git
cd peercourier
uv sync --locked
uv run peercourier demo .peercourier/first --initiator cedar \
  --codex-model gpt-6-luna --claude-model haiku
```

This uses provider quota. `cedar` is the Codex peer, `birch` the Claude peer.
The demo exchanges a request, clarification, numbers, calculated answer, and final
message; it exits successfully only after all five messages are completed and
explicitly acknowledged. Run the reverse direction in a **new** directory:

```sh
uv run peercourier demo .peercourier/reverse --initiator birch \
  --codex-model gpt-6-luna --claude-model haiku
```

Private tokens, the mailbox and provider-session records stay under that ignored
state directory. Initialization never overwrites a config. The repository is ready
to install from source or a locally built wheel; no PyPI release is claimed.

## Use your own task

```sh
uv run peercourier init .peercourier/my-task
uv run peercourier run .peercourier/my-task/config.json --peer cedar \
  --prompt "In conversation demo, send birch a question about a naming convention."
uv run peercourier inspect .peercourier/my-task/config.json
```

Edit the generated config to choose models, allowed recipients and limits before
the first run. `run` stops when owned queues drain; `run --watch` polls idle
mailboxes until cancelled or the configured wall-time limit expires. Healthy
sessions resume on later runs. Uncertain deliveries are held for administrator
review and produce a nonzero exit; they are never automatically retried.

After investigating an uncertain outcome, record your decision without redispatch:

```sh
uv run peercourier resolve .peercourier/my-task/config.json MESSAGE_ID --outcome failed
```

Use `completed` only when your review establishes completion. This administrative
command does not invent a recipient acknowledgment or rerun a provider turn.

## MCP tools

| Tool | Purpose |
|---|---|
| `peer_list` | List permitted peers and their runtime state |
| `peer_send` | Persist an addressed message with a unique idempotency key |
| `peer_inbox` | Read this peer's unacknowledged messages |
| `peer_ack` | Explicitly acknowledge a received message |
| `peer_status` | Inspect delivery separately from acknowledgment |

To connect another MCP-capable runtime, launch a per-peer server with:

```json
{
  "mcpServers": {
    "peercourier": {
      "command": "/absolute/path/to/peercourier/.venv/bin/python",
      "args": ["-m", "peercourier.mcp_server"],
      "env": {
        "PEERCOURIER_DB": "/absolute/path/to/state/mail.sqlite",
        "PEERCOURIER_PEER": "registered-peer-id",
        "PEERCOURIER_TOKEN": "generated-private-peer-token"
      }
    }
  }
}
```

The administrator must register that identity, permitted routes and conversation
first. Each client connects to the **same database** using its own identity. MCP
access provides mailbox tools; idle wake also requires a runtime adapter/supervisor.
For Codex, use its TOML MCP configuration equivalent rather than this Claude-style
JSON configuration.

## Development and boundaries

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

Tests do not call models. See [development workflow](CONTRIBUTING.md),
[architecture and limitations](docs/architecture.md), and
[verification record](docs/verification.md).

This is an initial local release. Tokens are intended for peers under one trusted
OS user, not hostile multi-tenant isolation. Codex cost is bounded by activity
limits, not an enforced dollar budget. Peer messages never grant extra permissions.
Native desktop/subagent integration remains an extension point.

MIT licensed.
