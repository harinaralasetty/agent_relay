"""Validate and edit peer definitions before the mailbox is registered."""

import fcntl
import json
import math
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from agentrelay.runtime import DEFAULT_OUTPUT_TOKENS, DEFAULT_TOOL_ROUNDS
from agentrelay.store import MAX_MESSAGES, identifier

SUPPORTED_RUNTIMES = ("codex", "claude", "litellm")
MAX_TOOL_ROUNDS = 64
MAX_OUTPUT_TOKENS = 32768


@contextmanager
def configuration_lock(path: Path):
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def validate(config: dict) -> None:
    if not isinstance(config, dict):
        raise ValueError("Configuration must be an object")
    peers = config.get("peers")
    if not isinstance(peers, dict) or len(peers) < 2:
        raise ValueError("Configure at least two peers")
    identifier(config.get("conversation_id"))
    max_messages = config.get("max_messages", 12)
    if type(max_messages) is not int or not 1 <= max_messages <= MAX_MESSAGES:
        raise ValueError(f"max_messages must be an integer between 1 and {MAX_MESSAGES}")
    limits = config.get("limits", {})
    defaults = {"max_turns": 6, "max_seconds": 600, "max_cost_usd": 1.0, "poll_interval": 0.1}
    if not isinstance(limits, dict) or limits.keys() - defaults.keys():
        raise ValueError("Unknown supervisor limits")
    for name, default in defaults.items():
        value = limits.get(name, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            or (name == "max_turns" and type(value) is not int)
        ):
            raise ValueError(f"{name} must be positive and finite; max_turns must be an integer")
    for peer, definition in peers.items():
        identifier(peer)
        if not isinstance(definition, dict) or definition.get("runtime") not in SUPPORTED_RUNTIMES:
            raise ValueError("Supported runtime adapters: codex, claude, litellm")
        if any(
            not isinstance(definition.get(k), str) or not definition[k].strip()
            for k in ("model", "token")
        ):
            raise ValueError("Every peer needs a model and a private token")
        allowed = definition.get("allowed")
        if (
            not isinstance(allowed, list)
            or any(
                not isinstance(target, str) or target not in peers or target == peer
                for target in allowed
            )
            or len(allowed) != len(set(allowed))
        ):
            raise ValueError("Allowed recipients must be distinct existing peers")
        timeout = definition.get("timeout", 90)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Timeout must be a positive finite number")
        if "api_key" in definition:
            raise ValueError("Use an API key environment variable name, never a key in config")
        if definition["runtime"] != "litellm":
            if any(k in definition for k in ("api_key_env", "api_base")):
                raise ValueError("API options require the litellm runtime")
            continue
        name = definition.get("api_key_env")
        if name is not None and (
            not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        ):
            raise ValueError("API key environment variable name is invalid")
        base = definition.get("api_base")
        if base is not None:
            if not isinstance(base, str):
                raise ValueError("API base must be an HTTP(S) URL without credentials")
            url = urlsplit(base)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("API base must be an HTTP(S) URL without credentials")
            _ = url.port
        for key, default, maximum in (
            ("max_tool_rounds", DEFAULT_TOOL_ROUNDS, MAX_TOOL_ROUNDS),
            ("max_output_tokens", DEFAULT_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS),
        ):
            value = definition.get(key, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{key} must be an integer between 1 and {maximum}")


def add_peer(
    path: Path,
    peer_id: str,
    runtime: str,
    model: str,
    allowed: list[str],
    *,
    reciprocal: bool = False,
    api_key_env: str | None = None,
    api_base: str | None = None,
    max_tool_rounds: int = DEFAULT_TOOL_ROUNDS,
    max_output_tokens: int = DEFAULT_OUTPUT_TOKENS,
) -> None:
    path = path.expanduser().resolve()
    with configuration_lock(path):
        if (path.parent / "mail.sqlite").exists():
            raise ValueError("Workspace is already registered; initialize a fresh workspace")
        config = json.loads(path.read_text())
        validate(config)
        if peer_id in config["peers"]:
            raise ValueError("Peer already exists")
        definition = {
            "runtime": runtime,
            "model": model,
            "token": secrets.token_urlsafe(32),
            "allowed": allowed,
        }
        if api_key_env is not None:
            definition["api_key_env"] = api_key_env
        if api_base is not None:
            definition["api_base"] = api_base
        if runtime == "litellm":
            definition.update(max_tool_rounds=max_tool_rounds, max_output_tokens=max_output_tokens)
        config["peers"][peer_id] = definition
        validate(config)
        if reciprocal:
            for target in allowed:
                config["peers"][target]["allowed"].append(peer_id)
        validate(config)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(config, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
