import asyncio
import json
import sys

import pytest

from agentrelay.adapters.litellm import LiteLLMRuntime
from agentrelay.runtime import RuntimeConfig, RuntimeFailure
from agentrelay.store import Store


async def test_native_session_rejected(tmp_path):
    runtime = LiteLLMRuntime(
        RuntimeConfig(
            "a", "openai/test", str(tmp_path), "unused", [], {}, resume_session="native-id"
        )
    )
    with pytest.raises(RuntimeFailure):
        await runtime.start()


def config(tmp_path, **kwargs):
    path = tmp_path / "mail.sqlite"
    store = Store(path)
    store.register("alpha", "secret-a", ["beta"])
    store.register("beta", "secret-b", ["alpha"])
    store.conversation("test", ["alpha", "beta"])
    return RuntimeConfig(
        "alpha",
        "openai/test",
        str(tmp_path),
        sys.executable,
        ["-m", "agentrelay.mcp_server"],
        {"AGENTRELAY_DB": str(path), "AGENTRELAY_PEER": "alpha", "AGENTRELAY_TOKEN": "secret-a"},
        **kwargs,
    )


def response(calls=None, finish=None, content=None):
    return {
        "choices": [
            {
                "finish_reason": finish or ("tool_calls" if calls else "stop"),
                "message": {"content": content, "tool_calls": calls},
            }
        ]
    }


def call(name="peer_list", args="{}", identifier="call-1"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": args}}


async def test_real_mcp_send_and_resume(tmp_path, monkeypatch):
    runtime = LiteLLMRuntime(config(tmp_path))
    requests = []

    async def complete(**kwargs):
        requests.append(json.loads(json.dumps(kwargs)))
        if len(requests) == 1:
            return response(
                [
                    call(
                        "peer_send",
                        json.dumps(
                            {
                                "to": "beta",
                                "conversation_id": "test",
                                "text": "hello",
                                "idempotency_key": "one",
                            }
                        ),
                    )
                ]
            )
        return response(content="sent")

    monkeypatch.setattr("agentrelay.adapters.litellm._completion", complete)
    await runtime.start()
    # Different tasks are deliberate: no MCP cancel scope may escape a turn.
    result = await asyncio.create_task(runtime.turn("send hello"))
    assert result.text == "sent"
    assert len(requests) == 2
    assert requests[0]["num_retries"] == requests[0]["max_retries"] == 0
    assert "effort" not in requests[0]
    assert requests[0]["caching"] is False
    assert requests[0]["cache"] == {"no-cache": True, "no-store": True}
    assert requests[0]["fallbacks"] == []
    assert requests[0]["no-log"] is True
    assert "secret-a" not in json.dumps(requests)
    state = runtime._path
    assert state.stat().st_mode & 0o077 == 0
    await asyncio.create_task(runtime.close())
    from dataclasses import replace

    resumed = LiteLLMRuntime(replace(runtime.config, resume_session=result.session_id))
    await resumed.start()
    assert resumed._state["messages"][-1]["content"] == "sent"
    wrong = LiteLLMRuntime(
        replace(runtime.config, model="openai/other", resume_session=result.session_id)
    )
    with pytest.raises(RuntimeFailure):
        await wrong.start()
    await resumed.close()


@pytest.mark.parametrize(
    "bad",
    [
        response([call("shell")]),
        response([call(args="[")]),
        response([call(), call()]),
        response(finish="length"),
        response([call(args="[]")]),
    ],
)
async def test_malformed_response_dirty_and_no_retry(tmp_path, monkeypatch, bad):
    runtime = LiteLLMRuntime(config(tmp_path))
    attempts = []

    async def complete(**kwargs):
        attempts.append(1)
        return bad

    monkeypatch.setattr("agentrelay.adapters.litellm._completion", complete)
    await runtime.start()
    with pytest.raises(RuntimeFailure, match="no retry"):
        await runtime.turn("hello")
    assert len(attempts) == 1
    assert json.loads(runtime._path.read_text())["dirty"]
    from dataclasses import replace

    with pytest.raises(RuntimeFailure):
        await LiteLLMRuntime(replace(runtime.config, resume_session=runtime.session_id)).start()


async def test_provider_error_redacted(tmp_path, monkeypatch):
    runtime = LiteLLMRuntime(config(tmp_path, api_key_env="TEST_RELAY_KEY"))
    monkeypatch.setenv("TEST_RELAY_KEY", "credential-value")

    async def complete(**kwargs):
        assert kwargs["api_key"] == "credential-value"
        raise ValueError("credential-value secret-a")

    monkeypatch.setattr("agentrelay.adapters.litellm._completion", complete)
    await runtime.start()
    with pytest.raises(RuntimeFailure) as caught:
        await runtime.turn("hello")
    assert "credential-value" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert "credential-value" not in runtime._path.read_text()


async def test_round_bound(tmp_path, monkeypatch):
    runtime = LiteLLMRuntime(config(tmp_path, max_tool_rounds=1))
    attempts = []

    async def complete(**kwargs):
        attempts.append(1)
        return response([call(identifier=str(len(attempts)))])

    monkeypatch.setattr("agentrelay.adapters.litellm._completion", complete)
    await runtime.start()
    with pytest.raises(RuntimeFailure):
        await runtime.turn("hello")
    assert len(attempts) == 2


async def test_timeout_leaves_dirty_history(tmp_path, monkeypatch):
    runtime = LiteLLMRuntime(config(tmp_path, timeout=0.3))

    async def complete(**kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr("agentrelay.adapters.litellm._completion", complete)
    await runtime.start()
    with pytest.raises(RuntimeFailure):
        await runtime.turn("hello")
    assert json.loads(runtime._path.read_text())["dirty"]


async def test_interruption_after_effect_fails_closed(tmp_path, monkeypatch):
    runtime = LiteLLMRuntime(config(tmp_path))
    entered = asyncio.Event()
    count = 0

    async def complete(**kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return response(
                [
                    call(
                        "peer_send",
                        json.dumps(
                            {
                                "to": "beta",
                                "conversation_id": "test",
                                "text": "hello",
                                "idempotency_key": "one",
                            }
                        ),
                    )
                ]
            )
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("agentrelay.adapters.litellm._completion", complete)
    await runtime.start()
    task = asyncio.create_task(runtime.turn("send"))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert json.loads(runtime._path.read_text())["dirty"]
    assert count == 2
    with pytest.raises(RuntimeFailure):
        await runtime.turn("retry")


async def test_output_bound(tmp_path, monkeypatch):
    runtime = LiteLLMRuntime(config(tmp_path))

    async def complete(**kwargs):
        return response(content="x" * (1024 * 1024))

    monkeypatch.setattr("agentrelay.adapters.litellm._completion", complete)
    await runtime.start()
    with pytest.raises(RuntimeFailure):
        await runtime.turn("hello")


async def test_stale_loaded_history_rejected(tmp_path, monkeypatch):
    from dataclasses import replace

    runtime = LiteLLMRuntime(config(tmp_path))
    await runtime.start()
    stale = LiteLLMRuntime(replace(runtime.config, resume_session=runtime.session_id))
    await stale.start()

    async def complete(**kwargs):
        return response(content="done")

    monkeypatch.setattr("agentrelay.adapters.litellm._completion", complete)
    await runtime.turn("one")
    with pytest.raises(RuntimeFailure):
        await stale.turn("two")
    assert json.loads(runtime._path.read_text())["dirty"] is False


@pytest.mark.parametrize("status", [200, 429])
async def test_actual_sdk_local_http_retry_disabled(tmp_path, monkeypatch, status, capsys):
    import importlib.util
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if importlib.util.find_spec("litellm") is None:
        pytest.skip("Optional models SDK not installed")
    received = []
    authorizations = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            authorizations.append(self.headers.get("Authorization"))
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if status == 429:
                payload = {"error": {"message": "test rate limit", "type": "rate_limit_error"}}
            else:
                payload = {
                    "id": "local",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "local success"},
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_LOCAL_KEY", "dummy-local-key")
    monkeypatch.setenv("OPENAI_API_KEY", "unselected-ambient-key")
    runtime = LiteLLMRuntime(
        config(
            tmp_path,
            api_key_env="TEST_LOCAL_KEY",
            api_base=f"http://127.0.0.1:{server.server_port}/v1",
        )
    )
    try:
        await runtime.start()
        if status == 429:
            with pytest.raises(RuntimeFailure):
                await runtime.turn("offline local test")
        else:
            assert (await runtime.turn("offline local test")).text == "local success"
        assert len(received) == 1
        assert authorizations == ["Bearer dummy-local-key"]
        assert received[0]["max_tokens"] == runtime.config.max_output_tokens
        assert len(received[0]["tools"]) == 5
        assert "dummy-local-key" not in json.dumps(received)
        captured = capsys.readouterr()
        assert "dummy-local-key" not in captured.out + captured.err
        assert "offline local test" not in captured.out + captured.err
    finally:
        await runtime.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "tool,args",
    [
        (
            "peer_send",
            {
                "to": "outsider",
                "conversation_id": "test",
                "text": "denied",
                "idempotency_key": "denied",
            },
        ),
        ("peer_send", {}),
        ("peer_ack", {"message_id": "unknown"}),
    ],
)
async def test_mailbox_rejections_are_generic(tmp_path, monkeypatch, tool, args):
    runtime = LiteLLMRuntime(config(tmp_path))
    requests = []

    async def complete(**kwargs):
        requests.append(json.loads(json.dumps(kwargs)))
        if len(requests) == 1:
            return response([call(tool, json.dumps(args))])
        assert json.loads(kwargs["messages"][-1]["content"]) == {
            "error": "Mailbox tool rejected request"
        }
        return response(content="rejected")

    monkeypatch.setattr("agentrelay.adapters.litellm._completion", complete)
    await runtime.start()
    assert (await runtime.turn("try")).text == "rejected"
    await runtime.close()


@pytest.mark.parametrize(
    "messages",
    [
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": None, "tool_calls": [call()]},
        ],
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": None, "tool_calls": [call()]},
            {"role": "tool", "tool_call_id": "wrong", "content": "{}"},
            {"role": "assistant", "content": "done"},
        ],
        [{"role": "assistant", "content": "orphan"}],
        [
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "done"},
        ],
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": None, "tool_calls": [call(), call()]},
            {"role": "assistant", "content": "done"},
        ],
    ],
)
async def test_resume_rejects_incomplete_or_unordered_history(tmp_path, messages):
    from dataclasses import replace

    runtime = LiteLLMRuntime(config(tmp_path))
    await runtime.start()
    state = json.loads(runtime._path.read_text())
    state["messages"] = messages
    runtime._path.write_text(json.dumps(state))
    resumed = LiteLLMRuntime(replace(runtime.config, resume_session=runtime.session_id))
    with pytest.raises(RuntimeFailure):
        await resumed.start()


async def test_resume_accepts_complete_multiple_rounds_and_turns(tmp_path):
    from dataclasses import replace

    runtime = LiteLLMRuntime(config(tmp_path))
    await runtime.start()
    state = json.loads(runtime._path.read_text())
    state["messages"] = [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [call(identifier="a"), call(identifier="b")],
        },
        {"role": "tool", "tool_call_id": "b", "content": "{}"},
        {"role": "tool", "tool_call_id": "a", "content": "{}"},
        {"role": "assistant", "content": None, "tool_calls": [call(identifier="c")]},
        {"role": "tool", "tool_call_id": "c", "content": "{}"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": None, "tool_calls": [call(identifier="a")]},
        {"role": "tool", "tool_call_id": "a", "content": "{}"},
        {"role": "assistant", "content": "done again"},
    ]
    runtime._path.write_text(json.dumps(state))
    resumed = LiteLLMRuntime(replace(runtime.config, resume_session=runtime.session_id))
    await resumed.start()
    assert resumed._state["messages"] == state["messages"]
    await resumed.close()


@pytest.mark.parametrize(
    "bad_call",
    [
        call("shell"),
        call(args="["),
        call(args="[]"),
        {"id": "call-1", "type": "other", "function": {"name": "peer_list", "arguments": "{}"}},
    ],
)
async def test_resume_rejects_invalid_persisted_tool_calls(tmp_path, bad_call):
    from dataclasses import replace

    runtime = LiteLLMRuntime(config(tmp_path))
    await runtime.start()
    state = json.loads(runtime._path.read_text())
    state["messages"] = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": None, "tool_calls": [bad_call]},
        {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
        {"role": "assistant", "content": "done"},
    ]
    runtime._path.write_text(json.dumps(state))
    with pytest.raises(RuntimeFailure):
        await LiteLLMRuntime(replace(runtime.config, resume_session=runtime.session_id)).start()
