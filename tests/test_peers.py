import json
import os
import subprocess
import sys

import pytest

from agent_relay.cli import initialize, load, make_runtimes
from agent_relay.peers import add_peer


def test_add_api_peer_preserves_clis_and_adds_explicit_routes(tmp_path):
    path = initialize(tmp_path / "state", "codex-model", "claude-model")
    original = json.loads(path.read_text())
    add_peer(
        path,
        "maple",
        "litellm",
        "openai/example",
        ["cedar"],
        reciprocal=True,
        api_key_env="MODEL_TEST_KEY",
        api_base="http://localhost:1234/v1",
    )
    config, store = load(path)
    assert config["peers"]["birch"] == original["peers"]["birch"]
    assert config["peers"]["cedar"]["runtime"] == "codex"
    assert config["peers"]["cedar"]["allowed"] == ["birch", "maple"]
    assert config["peers"]["maple"]["allowed"] == ["cedar"]
    assert os.stat(path).st_mode & 0o777 == 0o600
    runtimes = make_runtimes(config, store)
    assert runtimes["maple"].config.api_key_env == "MODEL_TEST_KEY"
    assert runtimes["maple"].config.max_tool_rounds == 8


def test_registered_workspace_cannot_be_edited(tmp_path):
    path = initialize(tmp_path / "state", "model", "model")
    load(path)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="registered"):
        add_peer(path, "maple", "litellm", "openai/example", ["cedar"])
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "options",
    [
        {"peer_id": "cedar"},
        {"runtime": "unknown"},
        {"allowed": ["missing"]},
        {"api_key_env": "secret-with-dash"},
        {"api_base": "https://user:pass@host/v1"},
        {"api_base": "https://host/v1?key=secret"},
        {"max_tool_rounds": 0},
        {"max_output_tokens": True},
        {"model": ""},
        {"allowed": ["maple"]},
    ],
)
def test_failed_edit_leaves_original_bytes_and_no_database(tmp_path, options):
    path = initialize(tmp_path / "state", "model", "model")
    original = path.read_bytes()
    args = dict(peer_id="maple", runtime="litellm", model="openai/example", allowed=["cedar"])
    args.update(options)
    with pytest.raises(ValueError):
        add_peer(path, **args)
    assert path.read_bytes() == original
    assert not (path.parent / "mail.sqlite").exists()


def test_invalid_manual_api_config_fails_before_registration(tmp_path):
    path = initialize(tmp_path / "state", "model", "model")
    config = json.loads(path.read_text())
    config["peers"]["cedar"].update(runtime="litellm", api_key="plaintext-secret")
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="environment"):
        load(path)
    assert not (path.parent / "mail.sqlite").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_messages", 0),
        ("max_messages", True),
        ("max_messages", 129),
        ("conversation_id", None),
        ("max_turns", 0),
        ("max_seconds", float("nan")),
        ("max_cost_usd", float("inf")),
        ("unknown_limit", 1),
    ],
)
def test_invalid_limits_do_not_register_workspace(tmp_path, field, value):
    path = initialize(tmp_path / "state", "model", "model")
    config = json.loads(path.read_text())
    if field == "conversation_id":
        del config[field]
    elif field == "max_messages":
        config[field] = value
    else:
        config["limits"][field] = value
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load(path)
    assert not (path.parent / "mail.sqlite").exists()


def test_add_peer_command_and_optional_import_are_lazy(tmp_path):
    path = initialize(tmp_path / "state", "model", "model")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_relay",
            "add-peer",
            str(path),
            "maple",
            "--runtime",
            "litellm",
            "--model",
            "openai/example",
            "--allowed",
            "cedar",
            "--reciprocal",
            "--api-key-env",
            "EXAMPLE_KEY",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "token" not in result.stdout
    code = "from agent_relay.cli import main; import sys; assert 'litellm' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
    assert result.returncode == 0
