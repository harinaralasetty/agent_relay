import concurrent.futures

import pytest

from agent_relay.store import CourierError, Store


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "mail.sqlite")
    db.register("alpha", "a-secret", ["beta"])
    db.register("beta", "b-secret", ["alpha"])
    db.register("outsider", "x-secret", [])
    db.conversation("topic", ["alpha", "beta"], max_messages=4)
    return db


def send(store, **kwargs):
    return store.send("alpha", "a-secret", "beta", "topic", "Hello?", "key-1", **kwargs)


def test_shared_database_and_idempotency(store):
    first = send(store)
    other = Store(store.path)
    assert other.inbox("beta", "b-secret")[0]["id"] == first["id"]
    assert send(other)["id"] == first["id"]
    assert len(other.messages("topic")) == 1
    with pytest.raises(CourierError, match="idempotency"):
        other.send("alpha", "a-secret", "beta", "topic", "Different", "key-1")


def test_identity_routing_and_ack(store):
    message = send(store)
    with pytest.raises(CourierError, match="identity"):
        store.inbox("beta", "a-secret")
    assert store.inbox("alpha", "a-secret") == []
    with pytest.raises(CourierError):
        store.ack("alpha", "a-secret", message["id"])
    store.ack("beta", "b-secret", message["id"])
    store.ack("beta", "b-secret", message["id"])
    assert store.inbox("beta", "b-secret") == []
    assert store.status("alpha", "a-secret", message["id"])["acknowledged"]
    with pytest.raises(CourierError):
        store.status("outsider", "x-secret", message["id"])


def test_reply_binding_and_final_closure(store):
    question = send(store)
    reply = store.send("beta", "b-secret", "alpha", "topic", "42", "reply", question["id"])
    assert reply["hop"] == 1
    with pytest.raises(CourierError, match="reply"):
        store.send("beta", "b-secret", "alpha", "topic", "Oops", "bad", reply["id"])
    store.send("alpha", "a-secret", "beta", "topic", "Done", "final", reply["id"], True)
    with pytest.raises(CourierError, match="closed"):
        store.send("beta", "b-secret", "alpha", "topic", "Again", "again")


def test_atomic_limits_across_connections(store):
    def attempt(n):
        try:
            return Store(store.path).send(
                "alpha", "a-secret", "beta", "topic", f"Message {n}", f"key-{n}"
            )
        except CourierError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(12)))
    assert len([r for r in results if r is not None]) == 4
    assert len(store.messages("topic")) == 4


def test_claim_completion_and_uncertain_recovery(store):
    first = send(store)
    claimed = store.claim("beta")
    assert claimed["id"] == first["id"]
    assert claimed["status"] == "submitted"
    assert store.claim("beta") is None
    assert store.recover_uncertain() == 1
    assert store.claim("beta") is None
    assert store.status("alpha", "a-secret", first["id"])["status"] == "uncertain"
    store.finish(first["id"], "completed")
    assert not store.status("alpha", "a-secret", first["id"])["acknowledged"]


def test_reject_oversized_invalid_and_unpermitted_messages(store):
    for text in ["", " " * 4, "x" * 16001]:
        with pytest.raises(CourierError):
            store.send("alpha", "a-secret", "beta", "topic", text, "invalid")
    with pytest.raises(CourierError):
        store.send("alpha", "a-secret", "outsider", "topic", "hello", "no-route")
    with pytest.raises(CourierError):
        store.send("alpha", "a-secret", "alpha", "topic", "hello", "self")


def test_reregister_does_not_overwrite_identity(store):
    with pytest.raises(CourierError):
        store.register("alpha", "new-secret", ["outsider"])
    assert store.peers("alpha", "a-secret")[0]["id"] == "beta"


def test_already_acknowledged_message_is_not_delivered_again(store):
    message = send(store)
    store.ack("beta", "b-secret", message["id"])
    assert store.claim("beta") is None


def test_administrator_can_resolve_uncertainty_without_redelivery(store):
    message = send(store)
    with pytest.raises(CourierError):
        store.resolve(message["id"], "completed")
    store.claim("beta")
    store.recover_uncertain()
    store.resolve(message["id"], "failed")
    assert store.uncertain() == [] and store.claim("beta") is None
    assert store.messages("topic")[0]["status"] == "failed"


def test_three_peers_cannot_cross_conversation_or_recipient_routes(tmp_path):
    db = Store(tmp_path / "three.sqlite")
    for peer in ["alpha", "beta", "gamma"]:
        db.register(peer, peer, [p for p in ["alpha", "beta", "gamma"] if p != peer])
    db.conversation("ab", ["alpha", "beta"])
    db.conversation("ac", ["alpha", "gamma"])
    ab = db.send("alpha", "alpha", "beta", "ab", "for beta", "ab")
    ac = db.send("alpha", "alpha", "gamma", "ac", "for gamma", "ac")
    assert [m["id"] for m in db.inbox("beta", "beta")] == [ab["id"]]
    assert [m["id"] for m in db.inbox("gamma", "gamma")] == [ac["id"]]
    with pytest.raises(CourierError):
        db.send("alpha", "alpha", "gamma", "ab", "wrong conversation", "cross")
    with pytest.raises(CourierError):
        db.send("gamma", "gamma", "alpha", "ac", "wrong parent", "bad", ab["id"])
