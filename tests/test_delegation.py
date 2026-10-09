"""Behavioral checks for a manager delegating two independent tasks."""

import copy
import json
import os

import pytest

from agent_relay.cli import load
from agent_relay.delegation import initialize_delegation, verify_delegation


@pytest.mark.parametrize("manager_runtime", ["claude", "codex"])
def test_manager_config_has_two_opposite_provider_workers_and_star_routes(
    tmp_path, manager_runtime
):
    path = initialize_delegation(tmp_path / "state", manager_runtime, "codex-small", "claude-small")
    config, _ = load(path)
    assert os.stat(path).st_mode & 0o777 == 0o600
    peers = config["peers"]
    assert set(peers) == {"manager", "worker-a", "worker-b"}
    assert peers["manager"]["runtime"] == manager_runtime
    assert peers["manager"]["allowed"] == ["worker-a", "worker-b"]
    for worker in ("worker-a", "worker-b"):
        assert peers[worker]["runtime"] != manager_runtime
        assert peers[worker]["allowed"] == ["manager"]
    assert len({p["token"] for p in peers.values()}) == 3
    previous = path.read_bytes()
    with pytest.raises(FileExistsError):
        initialize_delegation(path.parent, manager_runtime, "new", "new")
    assert path.read_bytes() == previous
    assert json.loads(previous)["max_messages"] == 5


def transcript(order):
    definitions = {
        "a": (
            "manager",
            "worker-a",
            "Task A: add 2 and 3. Reply exactly 'Result A: 5.' "
            "with reply_to pointing to this assignment and final=false. Then return.",
            None,
        ),
        "b": (
            "manager",
            "worker-b",
            "Task B: multiply 4 and 5. Reply exactly 'Result B: 20.' "
            "with reply_to pointing to this assignment and final=false. Then return.",
            None,
        ),
        "ra": ("worker-a", "manager", "Result A: 5.", "a"),
        "rb": ("worker-b", "manager", "Result B: 20.", "b"),
        "close": ("manager", "worker-a", "Verified: A=5; B=20.", "ra"),
    }
    messages = []
    for key in order:
        sender, recipient, text, parent = definitions[key]
        messages.append(
            {
                "id": key,
                "sender": sender,
                "recipient": recipient,
                "conversation_id": "delegation",
                "text": text,
                "reply_to": parent,
                "final": key == "close",
                "status": "completed",
                "acknowledged": True,
            }
        )
    return messages


@pytest.mark.parametrize(
    "order",
    [
        ["a", "b", "ra", "rb", "close"],
        ["a", "b", "rb", "ra", "close"],
        ["a", "ra", "b", "rb", "close"],
        ["b", "rb", "a", "ra", "close"],
    ],
)
def test_results_may_arrive_in_either_order(order):
    messages = transcript(order)
    assert verify_delegation(messages)
    messages[1]["status"] = "stored"
    assert verify_delegation(messages)  # Active-turn mailbox consumption is legitimate.


@pytest.mark.parametrize(
    "field,value,index",
    [
        ("text", "Result B: 99.", 3),
        ("reply_to", "a", 3),
        ("recipient", "worker-a", 3),
        ("conversation_id", "other", 3),
        ("final", True, 2),
        ("acknowledged", False, 1),
        ("status", "uncertain", 1),
        ("status", "submitted", 1),
        ("status", "failed", 1),
        ("id", "a", 3),
        ("reply_to", "rb", 4),
        ("final", False, 4),
    ],
)
def test_incomplete_wrong_or_uncertain_work_is_rejected(field, value, index):
    messages = transcript(["a", "b", "ra", "rb", "close"])
    messages[index][field] = value
    assert not verify_delegation(messages)


def test_cannot_close_before_both_results_or_accept_extra_messages():
    assert not verify_delegation(transcript(["a", "b", "ra", "close", "rb"]))
    messages = transcript(["a", "b", "ra", "rb", "close"])
    assert not verify_delegation(messages[:-1])
    assert not verify_delegation(messages + [copy.deepcopy(messages[-1])])


@pytest.mark.parametrize("manager_runtime", ["claude", "codex"])
def test_cli_can_initialize_a_manager_team_without_calling_models(
    tmp_path, manager_runtime, monkeypatch
):
    import sys

    from agent_relay.cli import main

    path = tmp_path / "team"
    monkeypatch.setattr(
        sys, "argv", ["agent_relay", "init", str(path), "--manager", manager_runtime]
    )
    main()
    config, _ = load(path / "config.json")
    assert config["peers"]["manager"]["runtime"] == manager_runtime
    assert config["max_messages"] == 12


def test_delegation_cli_fails_when_idle_without_worker_results(tmp_path, monkeypatch, capsys):
    import sys

    from agent_relay import cli

    async def empty_run(*args, **kwargs):
        return {"errors": [], "outputs": [], "unresolved_messages": []}

    monkeypatch.setattr(cli, "run", empty_run)
    monkeypatch.setattr(sys, "argv", ["agent_relay", "delegation-demo", str(tmp_path / "empty")])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["verified"] is False


def test_delegation_cli_rejects_runtime_errors_even_with_valid_messages(
    tmp_path, monkeypatch, capsys
):
    import sys

    from agent_relay import cli
    from agent_relay.store import Store

    async def failed_run(*args, **kwargs):
        return {"errors": [{"error": "Cleanup failed"}], "outputs": [], "unresolved_messages": []}

    monkeypatch.setattr(cli, "run", failed_run)
    monkeypatch.setattr(
        Store, "messages", lambda *args: transcript(["a", "b", "ra", "rb", "close"])
    )
    monkeypatch.setattr(sys, "argv", ["agent_relay", "delegation-demo", str(tmp_path / "failed")])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["verified"] is False


def test_manager_prompt_supplies_the_registered_conversation_id_explicitly():
    from agent_relay.delegation import manager_prompt

    prompt = manager_prompt()
    assert "conversation_id='delegation'" in prompt
    assert "EVERY peer_send call, including the final" in prompt
    assert "Send both assignments in your initial turn" in prompt


@pytest.mark.parametrize("limit", [0, 129])
def test_invalid_team_limits_fail_before_creating_private_state(tmp_path, limit):
    with pytest.raises(ValueError, match="message limit"):
        initialize_delegation(tmp_path / "invalid", "claude", "codex", "haiku", max_messages=limit)
    assert not (tmp_path / "invalid").exists()
