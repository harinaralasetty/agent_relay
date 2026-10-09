"""Exercise both relay directions with the real SDK and MCP, without model billing."""

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentrelay.adapters.litellm import LiteLLMRuntime
from agentrelay.cli import DEMO_TEXTS, verify_demo
from agentrelay.runtime import RuntimeConfig
from agentrelay.store import Store
from agentrelay.supervisor import Supervisor


@pytest.mark.parametrize("initiator", ["alpha", "beta"])
async def test_sdk_and_mcp_two_way_conversation(tmp_path, monkeypatch, initiator):
    if importlib.util.find_spec("litellm") is None:
        pytest.skip("Optional models SDK not installed")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            peer = request["model"]
            target = "beta" if peer == "alpha" else "alpha"
            calls = []

            def call(name, args):
                calls.append(
                    {
                        "id": f"call-{len(requests)}-{len(calls)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                )

            if request["messages"][-1]["role"] == "user":
                prompt = request["messages"][-1]["content"]
                if "Received mailbox message:\n" in prompt:
                    message = json.loads(prompt.split("Received mailbox message:\n", 1)[1])
                    call("peer_ack", {"message_id": message["id"]})
                    index = DEMO_TEXTS.index(message["text"]) + 1
                    if not message["final"]:
                        call(
                            "peer_send",
                            {
                                "to": target,
                                "conversation_id": "demo",
                                "text": DEMO_TEXTS[index],
                                "reply_to": message["id"],
                                "idempotency_key": f"step-{index}",
                                "final": index == 4,
                            },
                        )
                else:
                    call(
                        "peer_send",
                        {
                            "to": target,
                            "conversation_id": "demo",
                            "text": DEMO_TEXTS[0],
                            "idempotency_key": "start",
                        },
                    )
            payload = {
                "id": f"response-{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": peer,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if calls else "stop",
                        "message": {
                            "role": "assistant",
                            "content": None if calls else "done",
                            "tool_calls": calls or None,
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LOCAL_TEST_KEY", "dummy-key")
    store = Store(tmp_path / "mail.sqlite")
    runtimes = {}
    for peer, target in [("alpha", "beta"), ("beta", "alpha")]:
        store.register(peer, peer, [target])
        workspace = tmp_path / peer
        workspace.mkdir()
        runtimes[peer] = LiteLLMRuntime(
            RuntimeConfig(
                peer,
                f"openai/{peer}",
                str(workspace),
                sys.executable,
                ["-m", "agentrelay.mcp_server"],
                {
                    "AGENTRELAY_DB": str(store.path),
                    "AGENTRELAY_PEER": peer,
                    "AGENTRELAY_TOKEN": peer,
                },
                api_key_env="LOCAL_TEST_KEY",
                api_base=f"http://127.0.0.1:{server.server_port}/v1",
            )
        )
    store.conversation("demo", ["alpha", "beta"], 8)
    try:
        report = await Supervisor(store, runtimes, poll_interval=0.01).run_until_idle(
            {initiator: "Start the scripted conversation"}
        )
        assert report["errors"] == report["unresolved_messages"] == []
        assert verify_demo(store.messages("demo"))
        assert len(requests) == 12
        for peer in runtimes:
            sessions = {
                output["session_id"] for output in report["outputs"] if output["peer"] == peer
            }
            assert len(sessions) == 1
        assert "dummy-key" not in json.dumps(requests)
    finally:
        for runtime in runtimes.values():
            await runtime.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
