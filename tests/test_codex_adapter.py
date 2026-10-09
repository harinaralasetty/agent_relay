import asyncio
import json
import tomllib
from dataclasses import replace

import pytest

from agentrelay.runtime import RuntimeConfig, RuntimeFailure

# Allow cold CLI process startup; timeout behavior tests set a short turn deadline separately.
FAKE_STARTUP_TIMEOUT = 10


@pytest.fixture
def fake_codex(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        + r"""
import json, os, sys, time
from pathlib import Path

log = Path(os.environ["FAKE_LOG"])


def record(value):
    with log.open("a") as f:
        f.write(json.dumps(value) + "\n")


record({"argv": sys.argv[1:]})
if sys.argv[1:3] == ["mcp", "list"]:
    print(json.dumps([{"name": os.environ.get("FAKE_MCP", "code-review")}]))
    sys.exit(0)


def send(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    m = json.loads(line)
    record(m)
    method = m.get("method")
    if method == "initialize":
        send({"id": m["id"], "result": {}})
    elif method == "thread/resume":
        if m["params"]["threadId"] == "stale":
            send({"id": m["id"], "error": {"code": -32000, "message": "not found"}})
        else:
            root = "wrong" if m["params"]["threadId"] == "mismatch" else "root"
            send({"id": m["id"], "result": {"thread": {"id": root, "sessionId": "session"}}})
    elif method == "thread/start":
        send({"id": m["id"], "result": {"thread": {"id": "root", "sessionId": "session"}}})
    elif method == "turn/start":
        prompt = m["params"]["input"][0]["text"]
        if prompt == "rpc-error":
            send({"id": m["id"], "error": {"code": -32000, "message": "failed"}})
            continue
        send({"id": m["id"], "result": {"turn": {"id": "turn", "status": "inProgress"}}})
        if prompt == "timeout":
            continue
        if prompt == "child":
            import subprocess
            code = "import time; from pathlib import Path; time.sleep(0.7); Path('escaped').touch()"
            subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        if prompt == "failed":
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "root",
                        "turn": {"id": "turn", "status": "failed", "error": {"message": "failed"}},
                    },
                }
            )
            continue
        sys.stderr.write("x" * 100000)
        sys.stderr.flush()
        send({"id": "approval", "method": "item/commandExecution/requestApproval", "params": {}})
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "root",
                    "turnId": "turn",
                    "item": {"id": "message", "type": "agentMessage", "text": "reply " + prompt},
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {"threadId": "root", "turn": {"id": "turn", "status": "completed"}},
            }
        )
"""
    )
    executable.chmod(0o755)
    return executable


def config(fake_codex, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "wire.jsonl"))
    return RuntimeConfig(
        "alice",
        "model",
        str(tmp_path),
        "python",
        ["-m", "peer"],
        {"AGENTRELAY_PEER_ID": "alice", "TOKEN": "private"},
        timeout=FAKE_STARTUP_TIMEOUT,
        executable=str(fake_codex),
    )


async def test_one_root_persists_with_private_mcp_and_denied_approval(
    fake_codex, tmp_path, monkeypatch
):
    # Removing the thread reuse, MCP overrides, or approval reply breaks this boundary contract.
    from agentrelay.adapters.codex import CodexRuntime

    runtime = CodexRuntime(config(fake_codex, tmp_path, monkeypatch))
    try:
        await runtime.start()
        assert (await runtime.turn("one")).text == "reply one"
        result = await runtime.turn("two")
        assert result.text == "reply two"
        assert result.session_id == runtime.session_id == "root"
        assert result.metadata["provider_session_id"] == "session"
    finally:
        await runtime.close()
    wire = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
    methods = [m["method"] for m in wire if "method" in m]
    assert methods == ["initialize", "initialized", "thread/start", "turn/start", "turn/start"]
    argv = next(m["argv"] for m in wire if m.get("argv", [""])[0] == "app-server")
    assert argv[:3] == ["app-server", "--listen", "stdio://"]
    mcp_override = next(arg for arg in argv if arg.startswith("mcp_servers="))
    mcp_config = tomllib.loads(mcp_override)["mcp_servers"]
    assert mcp_config["code-review"] == {"command": "/usr/bin/false", "enabled": False}
    assert mcp_config["agentrelay"]["command"] == "python"
    assert mcp_config["agentrelay"]["args"] == ["-m", "peer"]
    assert mcp_config["agentrelay"]["env"] == {"AGENTRELAY_PEER_ID": "alice", "TOKEN": "private"}
    expected_tools = ["peer_list", "peer_send", "peer_inbox", "peer_ack", "peer_status"]
    assert mcp_config["agentrelay"]["enabled_tools"] == expected_tools
    assert mcp_config["agentrelay"]["tools"] == {
        name: {"approval_mode": "approve"} for name in expected_tools
    }
    assert 'approval_policy="never"' in argv
    assert "features.shell_tool=false" in argv
    assert "features.unified_exec=false" in argv
    assert 'web_search="disabled"' in argv
    assert "features.apps=false" in argv
    assert "features.code_mode_host=true" in argv
    for feature in (
        "multi_agent",
        "multi_agent_v2",
        "hooks",
        "plugins",
        "remote_plugin",
        "memories",
        "chronicle",
        "goals",
    ):
        assert f"features.{feature}=false" in argv
    assert "features.skip_host_skill_discovery=true" in argv
    assert all(m["params"]["threadId"] == "root" for m in wire if m.get("method") == "turn/start")
    assert any(m.get("result") == {"decision": "decline"} for m in wire)
    thread = next(m["params"] for m in wire if m.get("method") == "thread/start")
    assert thread["sandbox"] == "read-only"
    assert thread["approvalPolicy"] == "never"


@pytest.mark.parametrize("prompt", ["timeout", "rpc-error"])
async def test_uncertain_turn_closes_process_without_retry(
    fake_codex, tmp_path, monkeypatch, prompt
):
    from agentrelay.adapters.codex import CodexRuntime

    runtime = CodexRuntime(config(fake_codex, tmp_path, monkeypatch))
    await runtime.start()
    process = runtime._process
    runtime.config = replace(runtime.config, timeout=0.1)
    with pytest.raises(RuntimeFailure):
        await runtime.turn(prompt)
    assert process.returncode is not None
    with pytest.raises(RuntimeFailure):
        await runtime.turn("again")
    await runtime.close()
    wire = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
    assert sum(m.get("method") == "turn/start" for m in wire) == 1


async def test_existing_peer_server_configuration_fails_closed(fake_codex, tmp_path, monkeypatch):
    from agentrelay.adapters.codex import CodexRuntime

    monkeypatch.setenv("FAKE_MCP", "agentrelay")
    runtime = CodexRuntime(config(fake_codex, tmp_path, monkeypatch))
    with pytest.raises(RuntimeFailure):
        await runtime.start()
    wire = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
    assert all("app-server" not in m.get("argv", []) for m in wire)


async def test_close_never_signals_released_process_group(fake_codex, tmp_path, monkeypatch):
    from agentrelay.adapters.codex import CodexRuntime

    runtime = CodexRuntime(config(fake_codex, tmp_path, monkeypatch))
    await runtime.start()
    await runtime.close()

    def unexpected_signal(*args):
        pytest.fail("close signaled a released process group")

    monkeypatch.setattr("agentrelay.adapters.codex.os.killpg", unexpected_signal)
    await runtime.close()


async def test_configured_effort_is_sent_to_turn(fake_codex, tmp_path, monkeypatch):
    from dataclasses import replace

    from agentrelay.adapters.codex import CodexRuntime

    runtime = CodexRuntime(replace(config(fake_codex, tmp_path, monkeypatch), effort="medium"))
    try:
        await runtime.start()
        await runtime.turn("effort")
    finally:
        await runtime.close()
    wire = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
    turn = next(m for m in wire if m.get("method") == "turn/start")
    assert turn["params"]["effort"] == "medium"


async def test_resume_preserves_root_without_starting_new_thread(fake_codex, tmp_path, monkeypatch):
    from agentrelay.adapters.codex import CodexRuntime

    cfg = config(fake_codex, tmp_path, monkeypatch)
    first = CodexRuntime(cfg)
    await first.start()
    root = first.session_id
    await first.close()
    resumed = CodexRuntime(replace(cfg, resume_session=root))
    try:
        await resumed.start()
        result = await resumed.turn("resumed")
        assert resumed.session_id == result.session_id == "root"
    finally:
        await resumed.close()
    wire = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
    assert sum(m.get("method") == "thread/start" for m in wire) == 1
    resume = next(m for m in wire if m.get("method") == "thread/resume")
    assert resume["params"]["threadId"] == "root"
    assert resume["params"]["sandbox"] == "read-only"
    assert resume["params"]["approvalPolicy"] == "never"


@pytest.mark.parametrize("root", ["stale", "mismatch"])
async def test_resume_rejection_never_creates_new_root(fake_codex, tmp_path, monkeypatch, root):
    from agentrelay.adapters.codex import CodexRuntime

    runtime = CodexRuntime(replace(config(fake_codex, tmp_path, monkeypatch), resume_session=root))
    with pytest.raises(RuntimeFailure) as failure:
        await runtime.start()
    if root == "stale":
        assert "-32000" in str(failure.value.__cause__)
        assert "not found" not in str(failure.value.__cause__)
    assert runtime._process is None
    wire = [json.loads(line) for line in (tmp_path / "wire.jsonl").read_text().splitlines()]
    assert sum(m.get("method") == "thread/resume" for m in wire) == 1
    assert all(m.get("method") != "thread/start" for m in wire)


async def test_owned_descendant_cannot_continue_after_close(fake_codex, tmp_path, monkeypatch):
    from agentrelay.adapters.codex import CodexRuntime

    runtime = CodexRuntime(config(fake_codex, tmp_path, monkeypatch))
    try:
        await runtime.start()
        await runtime.turn("child")
    finally:
        await runtime.close()
    await asyncio.sleep(0.8)
    assert not (tmp_path / "escaped").exists()
