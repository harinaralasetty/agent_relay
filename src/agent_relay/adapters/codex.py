"""One owned Codex app-server process and root thread per peer."""

import asyncio
import contextlib
import json
import os
import signal

from agent_relay import __version__
from agent_relay.runtime import RuntimeConfig, RuntimeFailure, TurnResult

_MAX_LINE = 1024 * 1024
_MAX_EVENTS = 256
_MAX_TEXT = 1024 * 1024
_MAILBOX_TOOLS = ("peer_list", "peer_send", "peer_inbox", "peer_ack", "peer_status")


class CodexRuntime:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.session_id: str | None = None
        self._thread_id: str | None = None
        self._provider_session_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue(_MAX_EVENTS)
        self._next_id = 0
        self._closed = False
        self._lock = asyncio.Lock()

    async def _terminate(self, process):
        # start_new_session makes this group exclusively owned by this adapter.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            pass
        # Children may outlive the root even if it exited on SIGTERM.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()

    async def _argv(self):
        executable = self.config.executable or "codex"
        probe = await asyncio.create_subprocess_exec(
            executable,
            "mcp",
            "list",
            "--json",
            cwd=self.config.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=_MAX_LINE,
        )
        try:
            stdout, _ = await asyncio.wait_for(probe.communicate(), self.config.timeout)
            if probe.returncode != 0 or len(stdout) > _MAX_LINE:
                raise RuntimeFailure("Cannot enumerate inherited Codex MCP configuration")
            servers = json.loads(stdout)
            if not isinstance(servers, list):
                raise RuntimeFailure("Invalid Codex MCP configuration response")
        finally:
            await self._terminate(probe)
        argv = [executable, "app-server", "--listen", "stdio://"]
        disabled = []
        for server in servers:
            name = server["name"]
            if name == "agent_relay":
                raise RuntimeFailure("Reserved peer MCP server already configured")
            # Override paths do not parse quoted dotted keys. A single TOML map
            # handles dashed/dotted names and supplies valid disabled transports.
            disabled.append(f'{json.dumps(name)}={{command="/usr/bin/false",enabled=false}}')
        env = ",".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in self.config.mcp_env.items())
        approvals = ",".join(f'{name}={{approval_mode="approve"}}' for name in _MAILBOX_TOOLS)
        peer = (
            "agent_relay={command="
            + json.dumps(self.config.mcp_command)
            + ",args="
            + json.dumps(self.config.mcp_args)
            + ",env={"
            + env
            + "},enabled=true,required=true,enabled_tools="
            + json.dumps(_MAILBOX_TOOLS)
            + ",tools={"
            + approvals
            + "}}"
        )
        mcp_config = "{" + ",".join([*disabled, peer]) + "}"
        settings = {
            "mcp_servers": mcp_config,
            "approval_policy": '"never"',
            "sandbox_mode": '"read-only"',
            "model_reasoning_effort": json.dumps(self.config.effort),
            "web_search": '"disabled"',
            "features.skip_host_skill_discovery": "true",
            "features.code_mode_host": "true",
            **{
                f"features.{feature}": "false"
                for feature in (
                    "shell_tool",
                    "unified_exec",
                    "apps",
                    "browser_use",
                    "browser_use_external",
                    "computer_use",
                    "code_mode",
                    "view_image",
                    "multi_agent",
                    "multi_agent_v2",
                    "hooks",
                    "plugins",
                    "remote_plugin",
                    "memories",
                    "chronicle",
                    "goals",
                )
            },
        }
        for key, value in settings.items():
            argv += ["-c", f"{key}={value}"]
        return argv

    async def _send(self, message):
        if self._process is None or self._process.stdin is None:
            raise RuntimeFailure("Codex runtime is unavailable")
        self._process.stdin.write((json.dumps(message) + "\n").encode())
        await self._process.stdin.drain()

    async def _request(self, method, params):
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _read(self):
        try:
            while line := await self._process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise RuntimeFailure("Invalid Codex protocol message")
                if "method" in message and "id" in message:
                    method = message["method"]
                    if method in (
                        "item/commandExecution/requestApproval",
                        "item/fileChange/requestApproval",
                    ):
                        result = {"decision": "decline"}
                    elif method == "item/permissions/requestApproval":
                        result = {"permissions": {}, "scope": "turn"}
                    elif method == "mcpServer/elicitation/request":
                        result = {"action": "decline", "content": None}
                    else:
                        await self._send(
                            {
                                "id": message["id"],
                                "error": {"code": -32601, "message": "Interactive request denied"},
                            }
                        )
                        continue
                    await self._send({"id": message["id"], "result": result})
                elif "id" in message:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            error = message["error"]
                            code = error.get("code") if isinstance(error, dict) else None
                            suffix = f" (code {code})" if type(code) is int else ""
                            future.set_exception(
                                RuntimeFailure("Codex JSON-RPC request failed" + suffix)
                            )
                        else:
                            future.set_result(message["result"])
                elif message.get("method") in ("item/completed", "turn/completed"):
                    self._events.put_nowait(message)
            raise RuntimeFailure("Codex app-server exited unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception:
            failure = RuntimeFailure("Codex protocol failed; outcome uncertain")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(failure)
            # Preserve boundedness even on a flooding server.
            while not self._events.empty():
                self._events.get_nowait()
            self._events.put_nowait(failure)

    async def _drain_stderr(self):
        while await self._process.stderr.read(8192):
            pass  # No secret-bearing diagnostics retained or printed.

    async def start(self):
        async with self._lock:
            if self._closed:
                raise RuntimeFailure("Codex runtime is closed")
            if self._process is not None:
                return
            try:
                async with asyncio.timeout(self.config.timeout):
                    argv = await self._argv()
                    self._process = await asyncio.create_subprocess_exec(
                        *argv,
                        cwd=self.config.cwd,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                        limit=_MAX_LINE,
                    )
                    self._tasks = [
                        asyncio.create_task(self._read()),
                        asyncio.create_task(self._drain_stderr()),
                    ]
                    await self._request(
                        "initialize",
                        {"clientInfo": {"name": "agent_relay", "version": __version__}},
                    )
                    await self._send({"method": "initialized", "params": {}})
                    params = {
                        "model": self.config.model,
                        "cwd": self.config.cwd,
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    }
                    method = "thread/start"
                    if self.config.resume_session is not None:
                        method = "thread/resume"
                        params["threadId"] = self.config.resume_session
                    response = await self._request(method, params)
                    thread = response["thread"]
                    root = thread["id"]
                    if not isinstance(root, str) or not root:
                        raise RuntimeFailure("Codex returned invalid root identity")
                    if (
                        self.config.resume_session is not None
                        and root != self.config.resume_session
                    ):
                        raise RuntimeFailure("Codex resumed an unexpected root")
                    self._thread_id = self.session_id = root
                    self._provider_session_id = thread.get("sessionId") or root
            except BaseException as exc:
                await self.close()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise RuntimeFailure("Codex startup failed; no automatic retry") from exc

    async def turn(self, prompt: str) -> TurnResult:
        async with self._lock:
            if self._closed or self._thread_id is None:
                raise RuntimeFailure("Codex runtime is not running")
            try:
                async with asyncio.timeout(self.config.timeout):
                    response = await self._request(
                        "turn/start",
                        {
                            "threadId": self._thread_id,
                            "input": [{"type": "text", "text": prompt}],
                            "effort": self.config.effort,
                        },
                    )
                    turn_id = response["turn"]["id"]
                    messages = {}
                    while True:
                        event = await self._events.get()
                        if isinstance(event, Exception):
                            raise event
                        params = event["params"]
                        if params.get("threadId") != self._thread_id:
                            continue
                        if event["method"] == "item/completed":
                            if params.get("turnId") != turn_id:
                                continue
                            item = params["item"]
                            if item["type"] == "agentMessage":
                                messages[item["id"]] = item["text"]
                                if sum(len(text) for text in messages.values()) > _MAX_TEXT:
                                    raise RuntimeFailure("Codex response exceeded bound")
                        elif params["turn"]["id"] == turn_id:
                            if params["turn"]["status"] != "completed":
                                raise RuntimeFailure("Codex turn failed; outcome uncertain")
                            return TurnResult(
                                "\n".join(messages.values()),
                                self.session_id,
                                metadata={
                                    "thread_id": self._thread_id,
                                    "turn_id": turn_id,
                                    "provider_session_id": self._provider_session_id,
                                },
                            )
            except BaseException as exc:
                await self.close()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise RuntimeFailure("Codex turn failed; outcome uncertain; no retry") from exc

    async def close(self):
        self._closed = True
        if self._process is not None:
            await self._terminate(self._process)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._process = None
        self._tasks = []
