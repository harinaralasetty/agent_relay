# Verification record — initial local release

Validated on macOS arm64 with Python 3.12.13, MCP Python SDK 1.30.0,
Codex CLI 0.157.1 and Claude Code 2.1.294. Live models: GPT-6 Luna at low
reasoning and Claude Haiku. Models and CLI behavior can differ between accounts.

| Check | Observed result |
|---|---|
| Automated behavioral suite | 47 passed |
| Lint and formatting | Passed |
| Source distribution and wheel build | Passed |
| Wheel installation in clean environment; CLI entry point | Passed |
| Real stdio MCP with two independent client/server connections | Shared mailbox; identity, recipient ACK and visibility enforced |
| GPT-initiated live conversation | 5/5 expected messages completed and explicitly acknowledged; 3 turns per peer |
| Claude-initiated live conversation | 5/5 expected messages completed and explicitly acknowledged; 3 turns per peer |
| Live session continuity | Both directions retained one provider session per peer |
| Shutdown and healthy restart | 2/2 additional messages completed/ACKed; both peers resumed their original IDs |
| Independent review of code and live results | Confirmed fixes rechecked; no unresolved delivery finding within the reviewed local scope |

The live conversation required this sequence, with linked replies:

1. Request a clarification before adding two numbers.
2. Ask which numbers to add.
3. Supply 2 and 3.
4. Return exactly 5.
5. Send final verification, acknowledge it and stop.

Both CLI demo processes were observed to exit 0 after their checks. Private
result artifacts retain the message/turn records; exit codes were observed in
the invoking process results. They are distinct evidence. Raw transcripts,
provider session IDs and local credentials are intentionally excluded from Git.

## Fix and verification loop

Tests were written before new behavior. Confirmed review/runtime failures received
regression checks, fixes, and fresh tests. Findings retained during development:

- A cleanup exception skipped subsequent runtimes: each close is now attempted.
- An acknowledged stored message could be delivered again: claims skip it.
- Interrupted delivery appeared as successful recovery: uncertainty is reported
  and held; administrator resolution does not redispatch.
- A seed for a recovery-blocked peer could hang: it is refused promptly.
- A new session ID was saved too late: it is now recorded before its first turn.
- The demo accepted “15” or unrelated text: exact stage content, links, successful
  turns and recipient acknowledgments are now required.
- Generated Codex MCP overrides and the sandbox enum did not match this installed
  CLI: actual CLI parsing/startup checked after correction.
- Disabling Codex's code-mode host prevented MCP calls: it stays enabled for tool
  orchestration, with unrelated features disabled.
- Read-only Codex denied default MCP approval: only the five mailbox tools are
  explicitly allowed and approved per process; general requests remain denied.
- Claude subprocess output was unbounded: stdout is capped and stderr drained.

GPT-6.1 Sol was accepted by the desktop agent surface but rejected by this CLI's
ChatGPT account. The live tests switched to the available GPT-6 Luna after user
selection. Failed preflight/initiator attempts stored no peer messages and were
retained separately; they are not counted as successful conversations.

## Coverage limits

Automated tests cover real database concurrency/idempotency/limits, three-peer
routing separation, real MCP transport, serialized busy-peer delivery, ownership
locks, uncertainty, stale/mismatched session responses, denied approvals, timeouts,
malformed/oversized output and owned descendant cleanup. External protocol fault
cases use fake runtimes; they do not establish a live provider crash or outage test.

Live tests cover two managed peers, both initiators, linked replies, final closure,
explicit ACKs and healthy session resume. They do not prove native desktop/subagent
attachment, hostile multi-user isolation, large-team load, Windows support, or a
universal provider dollar cap. Linux CI runs the credential-free checks; live
provider behavior was exercised on macOS only.

This record describes measured evidence, not a guarantee that all defects are absent.
