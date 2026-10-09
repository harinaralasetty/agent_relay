"""Optional stateless provider inference with a private local conversation history."""

import asyncio
import copy
import fcntl
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agentrelay.runtime import RuntimeConfig, RuntimeFailure, TurnResult

_MAILBOX_TOOLS = {"peer_list", "peer_send", "peer_inbox", "peer_ack", "peer_status"}
_MAX_MESSAGE_BYTES = 1024 * 1024
_MAX_HISTORY_BYTES = 4 * _MAX_MESSAGE_BYTES
_MAX_CALLS = 32


async def _completion(**kwargs):
    # SDK import and credentials stay outside ordinary CLI-only runs.
    # Use the shipped cost map: importing the SDK must not fetch remote metadata.
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import litellm

    litellm.suppress_debug_info = True
    litellm.set_verbose = False
    litellm.global_disable_no_log_param = False
    for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
        logging.getLogger(name).disabled = True
    return await litellm.acompletion(**kwargs)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode()


def _validate_history(messages):
    """Accept only complete user/assistant turns with fully matched tool results."""
    if not isinstance(messages, list):
        raise ValueError()
    phase = "user"
    pending = set()
    seen = set()
    for message in messages:
        if not isinstance(message, dict) or len(_encoded(message)) > _MAX_MESSAGE_BYTES:
            raise ValueError()
        role = message.get("role")
        content = message.get("content")
        if phase == "user":
            if role != "user" or not isinstance(content, str) or message.get("tool_calls"):
                raise ValueError()
            phase = "assistant"
            seen = set()
        elif phase == "tools":
            identifier = message.get("tool_call_id")
            if role != "tool" or not isinstance(content, str) or identifier not in pending:
                raise ValueError()
            pending.remove(identifier)
            if not pending:
                phase = "assistant"
        else:
            if role != "assistant" or (content is not None and not isinstance(content, str)):
                raise ValueError()
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > _MAX_CALLS:
                raise ValueError()
            if not calls:
                if not isinstance(content, str):
                    raise ValueError()
                phase = "user"
                continue
            for call in calls:
                identifier = call["id"]
                function = call["function"]
                if (
                    call.get("type") != "function"
                    or not isinstance(identifier, str)
                    or not identifier
                    or identifier in seen
                    or function["name"] not in _MAILBOX_TOOLS
                    or not isinstance(json.loads(function["arguments"]), dict)
                ):
                    raise ValueError()
                seen.add(identifier)
                pending.add(identifier)
            phase = "tools"
    if phase != "user":
        raise ValueError()


class LiteLLMRuntime:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.session_id = None
        self._state = None
        self._path = None
        self._closed = False
        self._lock = asyncio.Lock()

    def _identity(self):
        return {
            "backend": "litellm",
            "peer": self.config.peer_id,
            "model": self.config.model,
            "api_base": self.config.api_base,
        }

    def _save(self):
        data = _encoded(self._state)
        if len(data) > _MAX_HISTORY_BYTES:
            raise RuntimeFailure("LiteLLM history exceeded bound")
        fd, name = tempfile.mkstemp(dir=self._path.parent, prefix=".pending-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._path)
            directory_fd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    async def start(self):
        async with self._lock:
            if self._closed:
                raise RuntimeFailure("LiteLLM runtime is closed")
            if self.session_id:
                return
            try:
                directory = Path(self.config.cwd) / ".agentrelay-litellm"
                if directory.is_symlink():
                    raise ValueError()
                directory.mkdir(mode=0o700, exist_ok=True)
                if directory.stat().st_mode & 0o077:
                    raise ValueError()
                resume = self.config.resume_session
                if resume is not None:
                    if not resume.startswith("litellm:"):
                        raise ValueError()
                    identifier = str(uuid.UUID(resume[8:]))
                    if resume != "litellm:" + identifier:
                        raise ValueError()
                else:
                    identifier = str(uuid.uuid4())
                self._path = directory / (identifier + ".json")
                if resume:
                    if self._path.is_symlink() or self._path.stat().st_mode & 0o077:
                        raise ValueError()
                    if self._path.stat().st_size > _MAX_HISTORY_BYTES:
                        raise ValueError()
                    state = json.loads(self._path.read_bytes())
                    if (
                        state.get("identity") != self._identity()
                        or state.get("dirty") is not False
                        or state.get("session_id") != resume
                        or state.get("version") != 1
                    ):
                        raise ValueError()
                    _validate_history(state.get("messages"))
                    self._state = state
                else:
                    self._state = {
                        "version": 1,
                        "identity": self._identity(),
                        "dirty": False,
                        "session_id": "litellm:" + identifier,
                        "messages": [],
                    }
                    self._save()
                self.session_id = self._state["session_id"]
            except Exception:
                self._closed = True
                raise RuntimeFailure(
                    "LiteLLM startup failed; invalid or incomplete history"
                ) from None

    async def turn(self, prompt: str) -> TurnResult:
        async with self._lock:
            if self._closed or self._state is None or self._state["dirty"]:
                raise RuntimeFailure("LiteLLM runtime is unavailable")
            lease = None
            try:
                lease = os.open(
                    str(self._path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
                )
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if json.loads(self._path.read_bytes()) != self._state:
                    raise ValueError()
                if not isinstance(prompt, str) or len(prompt.encode()) > _MAX_MESSAGE_BYTES:
                    raise ValueError()
                key = None
                if self.config.api_key_env:
                    key = os.environ.get(self.config.api_key_env)
                    if not key:
                        raise ValueError()
                self._state["dirty"] = True
                self._save()  # Durable uncertainty marker precedes any provider or mailbox effect.
                messages = copy.deepcopy(self._state["messages"])
                messages.append({"role": "user", "content": prompt})
                async with asyncio.timeout(self.config.timeout):
                    params = StdioServerParameters(
                        command=self.config.mcp_command,
                        args=self.config.mcp_args,
                        env=self.config.mcp_env,
                        cwd=self.config.cwd,
                    )
                    with open(os.devnull, "w") as diagnostics:
                        async with stdio_client(params, errlog=diagnostics) as (reader, writer):
                            async with ClientSession(reader, writer) as session:
                                await session.initialize()
                                listed = (await session.list_tools()).tools
                                if {tool.name for tool in listed} != _MAILBOX_TOOLS or len(
                                    listed
                                ) != 5:
                                    raise ValueError()
                                tools = [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": tool.name,
                                            "description": tool.description or "",
                                            "parameters": tool.inputSchema,
                                        },
                                    }
                                    for tool in listed
                                ]
                                seen = set()
                                for round_index in range(self.config.max_tool_rounds + 1):
                                    if len(_encoded(messages)) > _MAX_HISTORY_BYTES:
                                        raise ValueError()
                                    kwargs = dict(
                                        model=self.config.model,
                                        messages=messages,
                                        tools=tools,
                                        max_tokens=self.config.max_output_tokens,
                                        num_retries=0,
                                        max_retries=0,
                                        caching=False,
                                        cache={"no-cache": True, "no-store": True},
                                        fallbacks=[],
                                    )
                                    # A nonempty placeholder blocks SDK ambient credential lookup.
                                    kwargs["api_key"] = key or "agentrelay-no-credential"
                                    kwargs["no-log"] = True
                                    if self.config.api_base:
                                        kwargs["api_base"] = self.config.api_base
                                    response = await _completion(**kwargs)
                                    if hasattr(response, "model_dump"):
                                        response = response.model_dump()
                                    if len(_encoded(response)) > _MAX_MESSAGE_BYTES:
                                        raise ValueError()
                                    choices = response["choices"]
                                    if len(choices) != 1:
                                        raise ValueError()
                                    choice = choices[0]
                                    message = choice["message"]
                                    calls = message.get("tool_calls") or []
                                    content = message.get("content")
                                    if content is not None and not isinstance(content, str):
                                        raise ValueError()
                                    if not calls:
                                        if choice["finish_reason"] != "stop":
                                            raise ValueError()
                                        messages.append(
                                            {"role": "assistant", "content": content or ""}
                                        )
                                        _validate_history(messages)
                                        self._state["messages"] = messages
                                        self._state["dirty"] = False
                                        self._save()
                                        return TurnResult(content or "", self.session_id)
                                    if (
                                        choice["finish_reason"] != "tool_calls"
                                        or round_index >= self.config.max_tool_rounds
                                        or len(calls) > _MAX_CALLS
                                    ):
                                        raise ValueError()
                                    validated = []
                                    for call in calls:
                                        identifier = call["id"]
                                        function = call["function"]
                                        if (
                                            call.get("type") != "function"
                                            or not isinstance(identifier, str)
                                            or not identifier
                                            or identifier in seen
                                            or function["name"] not in _MAILBOX_TOOLS
                                        ):
                                            raise ValueError()
                                        seen.add(identifier)
                                        args = json.loads(function["arguments"])
                                        if not isinstance(args, dict):
                                            raise ValueError()
                                        validated.append((identifier, function["name"], args))
                                    messages.append(
                                        {
                                            "role": "assistant",
                                            "content": content,
                                            "tool_calls": calls,
                                        }
                                    )
                                    for identifier, name, args in validated:
                                        result = await session.call_tool(name, args)
                                        # Hide protocol diagnostics from inference.
                                        value = (
                                            {"error": "Mailbox tool rejected request"}
                                            if result.isError
                                            else result.model_dump(mode="json")
                                        )
                                        encoded = _encoded(value)
                                        if len(encoded) > _MAX_MESSAGE_BYTES:
                                            raise ValueError()
                                        messages.append(
                                            {
                                                "role": "tool",
                                                "tool_call_id": identifier,
                                                "content": encoded.decode(),
                                            }
                                        )
                raise ValueError()
            except BaseException as exc:
                self._closed = True
                # Even a failed final atomic write must leave this object unusable.
                self._state["dirty"] = True
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise RuntimeFailure("LiteLLM turn failed; outcome uncertain; no retry") from None

            finally:
                if lease is not None:
                    os.close(lease)

    async def close(self):
        self._closed = True
