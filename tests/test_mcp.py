import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from peercourier.store import Store


async def test_two_stdio_servers_share_authenticated_mailbox(tmp_path):
    path = tmp_path / "mail.sqlite"
    store = Store(path)
    store.register("alpha", "secret-a", ["beta"])
    store.register("beta", "secret-b", ["alpha"])
    store.conversation("test", ["alpha", "beta"])

    def params(peer, token):
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "peercourier.mcp_server"],
            env={"PEERCOURIER_DB": str(path), "PEERCOURIER_PEER": peer, "PEERCOURIER_TOKEN": token},
        )

    async with stdio_client(params("alpha", "secret-a")) as (ar, aw):
        async with ClientSession(ar, aw) as alpha:
            await alpha.initialize()
            tools = await alpha.list_tools()
            assert {t.name for t in tools.tools} == {
                "peer_list",
                "peer_send",
                "peer_inbox",
                "peer_ack",
                "peer_status",
            }
            schema = next(t for t in tools.tools if t.name == "peer_send").inputSchema
            assert "sender" not in schema["properties"]
            sent = await alpha.call_tool(
                "peer_send",
                {
                    "to": "beta",
                    "conversation_id": "test",
                    "text": "What is 2+2?",
                    "idempotency_key": "question",
                },
            )
            assert not sent.isError
            message = json.loads(sent.content[0].text)
            async with stdio_client(params("beta", "secret-b")) as (br, bw):
                async with ClientSession(br, bw) as beta:
                    await beta.initialize()
                    inbox = await beta.call_tool("peer_inbox", {})
                    assert json.loads(inbox.content[0].text)["messages"][0]["id"] == message["id"]
                    denied = await alpha.call_tool("peer_ack", {"message_id": message["id"]})
                    assert denied.isError
                    ack = await beta.call_tool("peer_ack", {"message_id": message["id"]})
                    assert not ack.isError
                    status = await alpha.call_tool("peer_status", {"message_id": message["id"]})
                    assert json.loads(status.content[0].text)["acknowledged"]


async def test_invalid_mcp_identity_cannot_read_mail(tmp_path):
    path = tmp_path / "mail.sqlite"
    Store(path).register("alpha", "correct-secret", [])
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "peercourier.mcp_server"],
        env={
            "PEERCOURIER_DB": str(path),
            "PEERCOURIER_PEER": "alpha",
            "PEERCOURIER_TOKEN": "wrong-secret",
        },
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool("peer_inbox", {})
            assert result.isError
            assert "correct-secret" not in str(result)
