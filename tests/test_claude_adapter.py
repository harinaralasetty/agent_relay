import json
import os
from pathlib import Path

import pytest

from agent_relay.runtime import RuntimeConfig, RuntimeFailure


@pytest.fixture
def fake_cli(tmp_path):
    script = tmp_path / "claude"
    script.write_text("""#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
session_flag = '--session-id' if '--session-id' in args else '--resume'
sid = args[args.index(session_flag) + 1]
with open('calls.jsonl', 'a') as f:
    f.write(json.dumps({'args': args, 'stdin': sys.stdin.read(),
        'env_secret': os.environ.get('PEER_TEST_SECRET')}) + '\\n')
mode = os.environ.get('FAKE_CLAUDE_MODE', '')
if mode == 'timeout':
    import subprocess
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    with open('child.pid', 'w') as f: f.write(str(child.pid))
    time.sleep(60)
if mode == 'overflow':
    sys.stdout.write(json.dumps({'type': 'result', 'subtype': 'success',
        'session_id': sid, 'result': 'x' * (1024 * 1024 + 1)}))
    sys.stdout.flush()
    sys.exit(0)
if mode == 'stderr':
    sys.stderr.write('x' * (2 * 1024 * 1024))
    sys.stderr.flush()
if mode == 'malformed': print('not JSON'); sys.exit(0)
print(json.dumps({'type': 'result',
    'subtype': 'error_max_turns' if mode == 'max_turns' else 'success',
    'is_error': mode == 'error', 'result': 'reply',
    'session_id': 'wrong' if mode == 'mismatch' else sid,
    'total_cost_usd': 0.012}))
if mode == 'exit': sys.exit(2)
""")
    script.chmod(0o700)
    return RuntimeConfig(
        "alice",
        "haiku",
        str(tmp_path),
        "/usr/bin/python3",
        ["-m", "agent_relay.mcp"],
        {"PEER_TEST_SECRET": "mcp-only"},
        executable=str(script),
        timeout=1.0,
    )


@pytest.mark.asyncio
async def test_session_and_tool_isolation(fake_cli, monkeypatch):
    from agent_relay.adapters.claude import ClaudeRuntime

    monkeypatch.delenv("PEER_TEST_SECRET", raising=False)
    runtime = ClaudeRuntime(fake_cli)
    await runtime.start()
    first = await runtime.turn("hello")
    second = await runtime.turn("again")
    await runtime.close()
    assert first.session_id == second.session_id == runtime.session_id
    assert first.text == "reply"
    assert first.cost_usd == 0.012
    assert first.metadata["cost_scope"] == "conversation_total"
    calls = [
        json.loads(line) for line in Path(fake_cli.cwd, "calls.jsonl").read_text().splitlines()
    ]
    args = calls[0]["args"]
    assert args[args.index("--session-id") + 1] == first.session_id
    assert calls[1]["args"][calls[1]["args"].index("--resume") + 1] == first.session_id
    assert "--session-id" not in calls[1]["args"]
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in args
    assert "--restricted" in args
    assert "peer mailbox" in args[args.index("--system-prompt") + 1]
    assert "--dangerously-skip-permissions" not in args
    mcp = json.loads(args[args.index("--mcp-config") + 1])
    assert list(mcp["mcpServers"]) == ["agent_relay"]
    assert mcp["mcpServers"]["agent_relay"]["env"]["PEER_TEST_SECRET"] == "mcp-only"
    assert calls[0]["env_secret"] is None
    assert calls[0]["stdin"] == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["malformed", "mismatch", "error", "max_turns", "exit"])
async def test_failed_turn_is_not_retried(fake_cli, monkeypatch, mode):
    from agent_relay.adapters.claude import ClaudeRuntime

    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    runtime = ClaudeRuntime(fake_cli)
    await runtime.start()
    with pytest.raises(RuntimeFailure):
        await runtime.turn("hello")
    with pytest.raises(RuntimeFailure):
        await runtime.turn("retry")
    await runtime.close()
    assert len(Path(fake_cli.cwd, "calls.jsonl").read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_timeout_stops_owned_process_group(fake_cli, monkeypatch):
    from agent_relay.adapters.claude import ClaudeRuntime

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "timeout")
    runtime = ClaudeRuntime(fake_cli)
    await runtime.start()
    with pytest.raises(RuntimeFailure, match="timed out"):
        await runtime.turn("hello")
    await runtime.close()
    child = int(Path(fake_cli.cwd, "child.pid").read_text())
    import asyncio

    for _ in range(20):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("owned child survived timeout")


@pytest.mark.asyncio
async def test_cancellation_closes_process_and_marks_session_uncertain(fake_cli, monkeypatch):
    import asyncio

    from agent_relay.adapters.claude import ClaudeRuntime

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "timeout")
    runtime = ClaudeRuntime(fake_cli)
    await runtime.start()
    turn = asyncio.create_task(runtime.turn("hello"))
    for _ in range(100):
        if Path(fake_cli.cwd, "child.pid").exists():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("fake process did not start")
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    with pytest.raises(RuntimeFailure):
        await runtime.turn("retry")
    await runtime.close()
    child = int(Path(fake_cli.cwd, "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


@pytest.mark.asyncio
async def test_explicit_resume_uses_recorded_session_on_first_turn(fake_cli):
    from dataclasses import replace

    from agent_relay.adapters.claude import ClaudeRuntime

    sid = "572f3406-1f1e-4162-ade9-b77aee9fc701"
    runtime = ClaudeRuntime(replace(fake_cli, resume_session=sid))
    await runtime.start()
    result = await runtime.turn("resume safely")
    await runtime.close()
    assert result.session_id == sid
    args = json.loads(Path(fake_cli.cwd, "calls.jsonl").read_text())["args"]
    assert args[args.index("--resume") + 1] == sid
    assert "--session-id" not in args


@pytest.mark.asyncio
async def test_invalid_resume_uuid_fails_without_launch(fake_cli):
    from dataclasses import replace

    from agent_relay.adapters.claude import ClaudeRuntime

    runtime = ClaudeRuntime(replace(fake_cli, resume_session="not-a-session"))
    with pytest.raises(RuntimeFailure, match="UUID"):
        await runtime.start()
    assert not Path(fake_cli.cwd, "calls.jsonl").exists()


@pytest.mark.asyncio
async def test_large_stdout_fails_closed(fake_cli, monkeypatch):
    from agent_relay.adapters.claude import ClaudeRuntime

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "overflow")
    runtime = ClaudeRuntime(fake_cli)
    await runtime.start()
    with pytest.raises(RuntimeFailure, match="output.*limit"):
        await runtime.turn("hello")
    with pytest.raises(RuntimeFailure):
        await runtime.turn("retry")
    await runtime.close()


@pytest.mark.asyncio
async def test_large_stderr_is_drained_without_blocking(fake_cli, monkeypatch):
    from agent_relay.adapters.claude import ClaudeRuntime

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stderr")
    runtime = ClaudeRuntime(fake_cli)
    await runtime.start()
    assert (await runtime.turn("hello")).text == "reply"
    await runtime.close()
