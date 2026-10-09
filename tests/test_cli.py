import json
import os
import subprocess
import sys

import pytest

from agentrelay.cli import initialize, load, verify_demo


def test_init_is_private_and_never_overwrites_config(tmp_path):
    directory = tmp_path / "state"
    path = initialize(directory, "codex-test-model", "claude-test-model")
    assert path.is_file()
    assert os.stat(path).st_mode & 0o777 == 0o600
    config, store = load(path)
    assert config["peers"]["cedar"]["model"] == "codex-test-model"
    assert len(store.messages("demo")) == 0
    with pytest.raises(FileExistsError):
        initialize(directory, "different", "different")


def test_inspect_has_no_secrets_and_does_not_call_provider(tmp_path):
    path = initialize(tmp_path / "state", "not-real", "not-real")
    config = json.loads(path.read_text())
    result = subprocess.run(
        [sys.executable, "-m", "agentrelay", "inspect", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == []
    assert all(p["token"] not in result.stdout + result.stderr for p in config["peers"].values())


def test_invalid_configuration_fails_before_runtime_launch(tmp_path):
    path = initialize(tmp_path / "state", "model", "model")
    config = json.loads(path.read_text())
    config["peers"]["cedar"]["runtime"] = "unknown"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="runtime"):
        load(path)


def test_demo_requires_full_acknowledged_completed_exchange():
    messages = []
    texts = [
        "Please ask me 'Which two numbers should I add?' Then add the two numbers I provide "
        "and reply using 'The sum is <number>.' with final=false. "
        "Wait for my final verification, then acknowledge it and stop.",
        "Which two numbers should I add?",
        "Add 2 and 3.",
        "The sum is 5.",
        "Verified: 5.",
    ]
    for n in range(5):
        messages.append(
            {
                "id": str(n),
                "sender": "cedar" if n % 2 == 0 else "birch",
                "recipient": "birch" if n % 2 == 0 else "cedar",
                "conversation_id": "demo",
                "reply_to": str(n - 1) if n else None,
                "final": n == 4,
                "status": "completed",
                "acknowledged": True,
                "text": texts[n],
            }
        )
    assert verify_demo(messages)
    messages[-1]["text"] = "Wrong: 15"
    assert not verify_demo(messages)
    messages[-1]["text"] = "Verified: 5."
    messages[3]["text"] = "nonsense"
    assert not verify_demo(messages)
    messages[3]["text"] = "The sum is 5."
    assert not verify_demo(messages[:1])
    messages[-1]["acknowledged"] = False
    assert not verify_demo(messages)
    messages[-1]["acknowledged"] = True
    messages[-1]["reply_to"] = "wrong"
    assert not verify_demo(messages)


def test_demo_accepts_consumed_mailbox_messages_but_rejects_uncertain_delivery():
    from agentrelay.cli import DEMO_TEXTS

    messages = [
        {
            "id": str(index),
            "sender": "cedar" if index % 2 == 0 else "birch",
            "recipient": "birch" if index % 2 == 0 else "cedar",
            "conversation_id": "demo",
            "reply_to": str(index - 1) if index else None,
            "final": index == 4,
            "status": "stored" if index == 1 else "completed",
            "acknowledged": True,
            "text": text,
        }
        for index, text in enumerate(DEMO_TEXTS)
    ]
    # A peer may consume and ACK the mailbox within its active turn, before a new dispatch.
    assert verify_demo(messages)
    messages[1]["acknowledged"] = False
    assert not verify_demo(messages)
    messages[1]["acknowledged"] = True
    for state in ("uncertain", "failed", "submitted"):
        messages[1]["status"] = state
        assert not verify_demo(messages)
    messages[1]["status"] = "completed"
    messages[3]["final"] = True
    assert not verify_demo(messages)
