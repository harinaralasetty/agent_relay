"""Shared SQLite mailbox. Peer tools never get administrative capabilities."""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_TEXT = 16_000
MAX_MESSAGES = 128
IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
DELIVERY_STATES = {"completed", "uncertain", "failed"}


class CourierError(ValueError):
    """A rejected mailbox operation, safe to show to a peer."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise CourierError("Invalid identifier (use 1-64 letters, digits, underscore or hyphen)")
    return value


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS peers (
                    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, allowed TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'waiting', session_id TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, participants TEXT NOT NULL,
                    max_messages INTEGER NOT NULL, closed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    sender TEXT NOT NULL, recipient TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    text TEXT NOT NULL, idempotency_key TEXT NOT NULL, reply_to TEXT,
                    final INTEGER NOT NULL, hop INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'stored', acknowledged INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT,
                    UNIQUE(sender, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS inbox ON messages(recipient, status, seq);
            """)

    @contextmanager
    def connect(self, *, write: bool = False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _auth(db, peer: str, token: str):
        row = db.execute("SELECT * FROM peers WHERE id=?", (peer,)).fetchone()
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not row or not hmac.compare_digest(row["token_hash"], digest):
            raise CourierError("Invalid peer identity")
        return row

    @staticmethod
    def _message(row):
        result = dict(row)
        result.pop("seq", None)
        result["final"] = bool(result["final"])
        result["acknowledged"] = bool(result["acknowledged"])
        return result

    def register(self, peer: str, token: str, allowed: list[str]) -> None:
        identifier(peer)
        for target in allowed:
            identifier(target)
        if not token or peer in allowed:
            raise CourierError("Invalid registration")
        digest = hashlib.sha256(token.encode()).hexdigest()
        routes = json.dumps(sorted(set(allowed)))
        with self.connect(write=True) as db:
            previous = db.execute("SELECT * FROM peers WHERE id=?", (peer,)).fetchone()
            if previous:
                if previous["token_hash"] != digest or previous["allowed"] != routes:
                    raise CourierError(
                        "Peer already registered with a different identity or routes"
                    )
                return
            db.execute(
                "INSERT INTO peers(id,token_hash,allowed) VALUES (?,?,?)", (peer, digest, routes)
            )

    def conversation(self, name: str, participants: list[str], max_messages: int = 12) -> None:
        identifier(name)
        participants = sorted(set(participants))
        if len(participants) < 2 or not 1 <= max_messages <= MAX_MESSAGES:
            raise CourierError("Invalid participants or message limit")
        with self.connect(write=True) as db:
            for peer in participants:
                if not db.execute("SELECT 1 FROM peers WHERE id=?", (peer,)).fetchone():
                    raise CourierError("Unknown participant")
            previous = db.execute("SELECT * FROM conversations WHERE id=?", (name,)).fetchone()
            if previous:
                if (
                    previous["participants"] != json.dumps(participants)
                    or previous["max_messages"] != max_messages
                ):
                    raise CourierError("Conversation already exists with a different definition")
                return
            db.execute(
                "INSERT INTO conversations(id,participants,max_messages) VALUES (?,?,?)",
                (name, json.dumps(participants), max_messages),
            )

    def peers(self, peer: str, token: str) -> list[dict]:
        with self.connect() as db:
            actor = self._auth(db, peer, token)
            allowed = set(json.loads(actor["allowed"]))
            return [
                dict(row)
                for row in db.execute("SELECT id,state,session_id,error FROM peers ORDER BY id")
                if row["id"] in allowed
            ]

    def send(
        self,
        peer: str,
        token: str,
        to: str,
        conversation_id: str,
        text: str,
        idempotency_key: str,
        reply_to: str | None = None,
        final: bool = False,
    ) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise CourierError(f"Message must contain 1-{MAX_TEXT} characters")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key) > 128
        ):
            raise CourierError("Invalid idempotency key (1-128 characters)")
        with self.connect(write=True) as db:
            actor = self._auth(db, peer, token)
            if to == peer or to not in json.loads(actor["allowed"]):
                raise CourierError("Recipient is not permitted")
            previous = db.execute(
                "SELECT * FROM messages WHERE sender=? AND idempotency_key=?",
                (peer, idempotency_key),
            ).fetchone()
            if previous:
                same = (
                    previous["recipient"],
                    previous["conversation_id"],
                    previous["text"],
                    previous["reply_to"],
                    bool(previous["final"]),
                )
                if same != (to, conversation_id, text, reply_to, final):
                    raise CourierError("Conflicting reuse of idempotency key")
                return self._message(previous)
            conv = db.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if not conv or {peer, to} - set(json.loads(conv["participants"])):
                raise CourierError("Conversation or participant is not permitted")
            if conv["closed"]:
                raise CourierError("Conversation is closed")
            count = db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)
            ).fetchone()[0]
            if count >= conv["max_messages"]:
                raise CourierError("Conversation message limit reached")
            hop = 0
            if reply_to:
                parent = db.execute("SELECT * FROM messages WHERE id=?", (reply_to,)).fetchone()
                if (
                    not parent
                    or parent["recipient"] != peer
                    or parent["sender"] != to
                    or parent["conversation_id"] != conversation_id
                    or parent["final"]
                ):
                    raise CourierError("Invalid reply binding")
                hop = parent["hop"] + 1
            mid, now = str(uuid.uuid4()), time.time()
            db.execute(
                """INSERT INTO messages
                (id,sender,recipient,conversation_id,text,idempotency_key,reply_to,final,hop,
                 created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    mid,
                    peer,
                    to,
                    conversation_id,
                    text,
                    idempotency_key,
                    reply_to,
                    int(final),
                    hop,
                    now,
                    now,
                ),
            )
            if final:
                db.execute("UPDATE conversations SET closed=1 WHERE id=?", (conversation_id,))
            return self._message(db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())

    def inbox(self, peer: str, token: str, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 50:
            raise CourierError("Inbox limit must be 1-50")
        with self.connect() as db:
            self._auth(db, peer, token)
            return [
                self._message(row)
                for row in db.execute(
                    "SELECT * FROM messages WHERE recipient=? AND acknowledged=0 "
                    "ORDER BY seq LIMIT ?",
                    (peer, limit),
                )
            ]

    def ack(self, peer: str, token: str, message_id: str) -> dict:
        with self.connect(write=True) as db:
            self._auth(db, peer, token)
            row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or row["recipient"] != peer:
                raise CourierError("Only the recipient can acknowledge this message")
            db.execute(
                "UPDATE messages SET acknowledged=1,updated_at=? WHERE id=?",
                (time.time(), message_id),
            )
            return {"message_id": message_id, "acknowledged": True}

    def status(self, peer: str, token: str, message_id: str) -> dict:
        with self.connect() as db:
            self._auth(db, peer, token)
            row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or peer not in (row["sender"], row["recipient"]):
                raise CourierError("Message is not visible to this identity")
            return self._message(row)

    def messages(self, conversation_id: str) -> list[dict]:
        """Administrative transcript; never exposed as a peer tool."""
        with self.connect() as db:
            return [
                self._message(row)
                for row in db.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY seq",
                    (conversation_id,),
                )
            ]

    def claim(self, peer: str) -> dict | None:
        with self.connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM messages WHERE recipient=? AND status='stored' "
                "AND acknowledged=0 "
                "ORDER BY seq LIMIT 1",
                (peer,),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE messages SET status='submitted',updated_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            return self._message(
                db.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()
            )

    def finish(self, message_id: str, status: str, error: str | None = None) -> None:
        if status not in DELIVERY_STATES:
            raise CourierError("Invalid delivery status")
        with self.connect(write=True) as db:
            db.execute(
                "UPDATE messages SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error, time.time(), message_id),
            )

    def recover_uncertain(self) -> int:
        with self.connect(write=True) as db:
            cursor = db.execute(
                "UPDATE messages SET status='uncertain', "
                "error='Supervisor restarted during delivery',updated_at=? "
                "WHERE status='submitted'",
                (time.time(),),
            )
            return cursor.rowcount

    def uncertain(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,recipient FROM messages WHERE status='uncertain' ORDER BY seq"
                )
            ]

    def resolve(self, message_id: str, outcome: str) -> None:
        """Administrative review only: resolve an uncertain result, never redispatch it."""
        if outcome not in {"completed", "failed"}:
            raise CourierError("Resolution must be completed or failed")
        with self.connect(write=True) as db:
            row = db.execute("SELECT status FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or row["status"] != "uncertain":
                raise CourierError("Only an uncertain message may be resolved")
            db.execute(
                "UPDATE messages SET status=?,error='Administrator reviewed outcome', "
                "updated_at=? WHERE id=?",
                (outcome, time.time(), message_id),
            )

    def peer_state(
        self, peer: str, state: str, session_id: str | None = None, error: str | None = None
    ) -> None:
        with self.connect(write=True) as db:
            db.execute(
                "UPDATE peers SET state=?,session_id=COALESCE(?,session_id),error=? WHERE id=?",
                (state, session_id, error, peer),
            )
