"""SQLite：群消息、每群读到哪了、每次 run 的统计、几个开关。

全部同步调用。小群的写入量下，一次 INSERT 远小于一次 Telegram 往返，不值得上线程池。
"""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    sender_id   INTEGER,
    sender      TEXT NOT NULL,
    is_self     INTEGER NOT NULL DEFAULT 0,
    ts          INTEGER NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    reply_to    INTEGER,
    file_id     TEXT,
    file_name   TEXT,
    directed    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    last_seen   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    started     REAL NOT NULL,
    n_new       INTEGER,
    turns       INTEGER,
    duration    REAL,
    cost        REAL,
    input       INTEGER,
    cache_read  INTEGER,
    cache_write INTEGER,
    output      INTEGER,
    sends       INTEGER,
    timed_out   INTEGER,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class Msg:
    seq: int
    chat_id: int
    msg_id: int
    sender_id: int | None
    sender: str
    is_self: bool
    ts: int
    text: str
    reply_to: int | None
    file_id: str | None
    file_name: str | None
    directed: bool


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ---- 消息 ----

    def add_message(self, chat_id: int, msg_id: int, sender: str, text: str, *,
                    sender_id: int | None = None, is_self: bool = False,
                    ts: int | None = None, reply_to: int | None = None,
                    file_id: str | None = None, file_name: str | None = None,
                    directed: bool = False) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO messages (chat_id, msg_id, sender_id, sender, is_self, ts,"
            " text, reply_to, file_id, file_name, directed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, msg_id, sender_id, sender, int(is_self), ts or int(time.time()),
             text, reply_to, file_id, file_name, int(directed)))

    def edit_message(self, chat_id: int, msg_id: int, text: str) -> None:
        self.db.execute("UPDATE messages SET text=? WHERE chat_id=? AND msg_id=?",
                        (text, chat_id, msg_id))

    def get_message(self, chat_id: int, msg_id: int) -> Msg | None:
        r = self.db.execute("SELECT * FROM messages WHERE chat_id=? AND msg_id=?",
                            (chat_id, msg_id)).fetchone()
        return _msg(r) if r else None

    def unseen(self, chat_id: int) -> list[Msg]:
        """上次 run 之后别人发的消息。bot 自己的不算，不然它说一句就会再触发一次自己。"""
        rows = self.db.execute(
            "SELECT * FROM messages WHERE chat_id=? AND is_self=0 AND seq>? ORDER BY seq",
            (chat_id, self.last_seen(chat_id))).fetchall()
        return [_msg(r) for r in rows]

    def history(self, chat_id: int, before_seq: int, limit: int) -> list[Msg]:
        rows = self.db.execute(
            "SELECT * FROM messages WHERE chat_id=? AND seq<? ORDER BY seq DESC LIMIT ?",
            (chat_id, before_seq, limit)).fetchall()
        return [_msg(r) for r in reversed(rows)]

    def names(self, chat_id: int) -> dict[int, str]:
        """群里每个人最近一次用的名字。"""
        rows = self.db.execute(
            "SELECT sender_id, sender FROM messages WHERE chat_id=? AND sender_id IS NOT NULL"
            " AND is_self=0 ORDER BY seq", (chat_id,)).fetchall()
        return {r[0]: r[1] for r in rows}

    def max_seq(self, chat_id: int) -> int:
        r = self.db.execute("SELECT MAX(seq) FROM messages WHERE chat_id=?", (chat_id,)).fetchone()
        return r[0] or 0

    # ---- 群 ----

    def touch_chat(self, chat_id: int, title: str | None) -> None:
        self.db.execute(
            "INSERT INTO chats (chat_id, title) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title", (chat_id, title))

    def chat_title(self, chat_id: int) -> str:
        r = self.db.execute("SELECT title FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        return (r and r[0]) or str(chat_id)

    def chat_ids(self) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT chat_id FROM chats")]

    def spoke_in(self, user_id: int, chats: frozenset[int]) -> bool:
        if not chats:
            return False
        q = ",".join("?" * len(chats))
        return self.db.execute(
            f"SELECT 1 FROM messages WHERE sender_id=? AND chat_id IN ({q}) LIMIT 1",
            (user_id, *chats)).fetchone() is not None

    def last_seen(self, chat_id: int) -> int:
        r = self.db.execute("SELECT last_seen FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        return r[0] if r else 0

    def set_last_seen(self, chat_id: int, seq: int) -> None:
        self.db.execute(
            "INSERT INTO chats (chat_id, last_seen) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET last_seen=excluded.last_seen", (chat_id, seq))

    # ---- run 统计 ----

    def record_run(self, row: dict) -> None:
        cols = ", ".join(row)
        self.db.execute(f"INSERT OR REPLACE INTO runs ({cols}) VALUES ({', '.join('?' * len(row))})",
                        tuple(row.values()))

    def runs_since(self, ts: float) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM runs WHERE started>=? ORDER BY started",
                               (ts,)).fetchall()

    def last_runs(self, n: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT ?", (n,)).fetchall()

    # ---- 开关 ----

    def get(self, key: str, default: str = "") -> str:
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def put(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))


def _msg(r: sqlite3.Row) -> Msg:
    return Msg(seq=r["seq"], chat_id=r["chat_id"], msg_id=r["msg_id"], sender_id=r["sender_id"],
               sender=r["sender"], is_self=bool(r["is_self"]), ts=r["ts"], text=r["text"],
               reply_to=r["reply_to"], file_id=r["file_id"], file_name=r["file_name"],
               directed=bool(r["directed"]))
