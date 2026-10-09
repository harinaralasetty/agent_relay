import asyncio

import pytest

from agent_relay.runtime import RuntimeFailure, TurnResult
from agent_relay.store import Store
from agent_relay.supervisor import Supervisor


class TestRuntime:
    __test__ = False

    def __init__(self, callback=None, fail=False):
        self.session_id = "stable-session"
        self.callback = callback
        self.fail = fail
        self.prompts = []
        self.active = 0
        self.max_active = 0
        self.closed = False

    async def start(self):
        pass

    async def turn(self, prompt):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.prompts.append(prompt)
            await asyncio.sleep(0.01)
            if self.fail:
                raise RuntimeFailure("uncertain synthetic failure")
            if self.callback:
                self.callback()
            return TurnResult("Turn done", self.session_id)
        finally:
            self.active -= 1

    async def close(self):
        self.closed = True


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "mail.sqlite")
    for peer, other in [("alpha", "beta"), ("beta", "alpha")]:
        db.register(peer, peer, [other])
    db.conversation("topic", ["alpha", "beta"], max_messages=10)
    return db


async def test_serializes_busy_messages_and_wakes_idle_peer(store):
    runtime = TestRuntime()
    first = store.send("alpha", "alpha", "beta", "topic", "one", "one")
    second = store.send("alpha", "alpha", "beta", "topic", "two", "two")
    supervisor = Supervisor(store, {"beta": runtime}, poll_interval=0.01)
    report = await supervisor.run_until_idle()
    assert len(runtime.prompts) == 2
    assert runtime.max_active == 1
    assert first["id"] in runtime.prompts[0] and second["id"] in runtime.prompts[1]
    assert report["turns"] == {"beta": 2}
    assert all(m["status"] == "completed" for m in store.messages("topic"))
    assert not any(m["acknowledged"] for m in store.messages("topic"))
    assert runtime.closed


async def test_reply_delivered_after_initial_turn_with_stable_sessions(store):
    a = TestRuntime(lambda: store.send("alpha", "alpha", "beta", "topic", "question", "q"))

    def answer():
        q = store.messages("topic")[0]
        store.ack("beta", "beta", q["id"])
        store.send("beta", "beta", "alpha", "topic", "answer", "a", q["id"], True)

    b = TestRuntime(answer)
    # Initial turn sends, second turn just reads final: do not replay seed callback.
    original = a.turn

    async def a_turn(prompt):
        result = await original(prompt)
        a.callback = None
        return result

    a.turn = a_turn
    report = await Supervisor(store, {"alpha": a, "beta": b}, poll_interval=0.01).run_until_idle(
        initial={"alpha": "Begin"}
    )
    assert report["errors"] == []
    assert report["turns"] == {"alpha": 2, "beta": 1}
    assert a.closed and b.closed


async def test_failure_is_uncertain_and_not_retried(store):
    runtime = TestRuntime(fail=True)
    store.send("alpha", "alpha", "beta", "topic", "do work", "work")
    report = await Supervisor(store, {"beta": runtime}, poll_interval=0.01).run_until_idle()
    assert len(runtime.prompts) == 1 and report["errors"]
    assert store.messages("topic")[0]["status"] == "uncertain"
    again = TestRuntime()
    await Supervisor(store, {"beta": again}, poll_interval=0.01).run_until_idle()
    assert again.prompts == []


async def test_hard_turn_cap_stops_pending_work(store):
    runtime = TestRuntime()
    for n in range(3):
        store.send("alpha", "alpha", "beta", "topic", "loop", str(n))
    report = await Supervisor(
        store, {"beta": runtime}, max_turns=2, poll_interval=0.01
    ).run_until_idle()
    assert len(runtime.prompts) == 2 and report["errors"]
    assert store.messages("topic")[-1]["status"] == "failed"


async def test_second_supervisor_cannot_recover_or_deliver_while_first_owns_store(store):
    runtime = TestRuntime()
    first = Supervisor(store, {"beta": runtime}, poll_interval=0.01)
    task = asyncio.create_task(first.run_forever())
    await asyncio.sleep(0.05)
    try:
        with pytest.raises(RuntimeFailure, match="supervisor"):
            await Supervisor(store, {}, poll_interval=0.01).run_until_idle()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert runtime.closed


async def test_cleanup_failure_does_not_leave_other_runtime_open(store):
    bad = TestRuntime()
    good = TestRuntime()

    async def failed_close():
        raise OSError("synthetic close failure")

    bad.close = failed_close
    report = await Supervisor(store, {"alpha": bad, "beta": good}).run_until_idle()
    assert good.closed
    assert any(error["error"] == "Runtime cleanup failed" for error in report["errors"])


async def test_session_is_recorded_before_first_turn_can_fail(store):
    def observe_session():
        with store.connect() as db:
            row = db.execute("SELECT session_id FROM peers WHERE id='beta'").fetchone()
        assert row["session_id"] == "stable-session"

    runtime = TestRuntime(observe_session)
    store.send("alpha", "alpha", "beta", "topic", "hello", "hello")
    report = await Supervisor(store, {"beta": runtime}, poll_interval=0.01).run_until_idle()
    assert report["errors"] == []


async def test_recovered_uncertain_work_is_reported_without_retry(store):
    store.send("alpha", "alpha", "beta", "topic", "work", "work")
    claimed = store.claim("beta")
    runtime = TestRuntime()
    report = await Supervisor(store, {"beta": runtime}, poll_interval=0.01).run_until_idle()
    assert report["errors"]
    assert report["unresolved_messages"] == [claimed["id"]]
    assert runtime.prompts == []


async def test_seed_to_recovery_blocked_peer_returns_without_hanging(store):
    store.send("alpha", "alpha", "beta", "topic", "work", "work")
    store.claim("beta")
    runtime = TestRuntime()
    report = await asyncio.wait_for(
        Supervisor(store, {"beta": runtime}, poll_interval=0.01).run_until_idle(
            initial={"beta": "New seed must wait for uncertain outcome review"}
        ),
        0.2,
    )
    assert report["errors"] and runtime.prompts == []
