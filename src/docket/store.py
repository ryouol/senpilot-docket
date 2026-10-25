import sqlite3
from datetime import timedelta
from pathlib import Path

from .models import InboundEmail, utc_now


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, inbound TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                matter TEXT, error TEXT, result_path TEXT
            );
            CREATE TABLE IF NOT EXISTS replies (
                job_id TEXT NOT NULL REFERENCES jobs(id), part INTEGER NOT NULL,
                message_id TEXT UNIQUE NOT NULL, mime_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ready', error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (job_id, part)
            );
            CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(status, available_at);
        """)

    def close(self):
        self.db.close()

    def get_setting(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str):
        with self.db:
            self.db.execute(
                "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def enqueue(self, identity: str, inbound: InboundEmail) -> bool:
        now = utc_now().isoformat()
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO jobs(id,inbound,available_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (identity, inbound.model_dump_json(), now, now, now),
            )
        return cur.rowcount == 1

    def recover(self):
        # A network send interrupted by process death must be reconciled, never blindly replayed.
        with self.db:
            self.db.execute("UPDATE replies SET status='uncertain' WHERE status='sending'")
            self.db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            self.db.execute(
                "UPDATE jobs SET status='delivery_uncertain' WHERE id IN "
                "(SELECT job_id FROM replies WHERE status='uncertain')"
            )

    def next_job(self) -> dict | None:
        with self.db:
            row = self.db.execute(
                "SELECT * FROM jobs WHERE status='queued' AND available_at<=? "
                "ORDER BY created_at LIMIT 1",
                (utc_now().isoformat(),),
            ).fetchone()
            if row:
                self.db.execute(
                    "UPDATE jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",
                    (utc_now().isoformat(), row["id"]),
                )
        return dict(row) | {"attempts": row["attempts"] + 1} if row else None

    def set_job(
        self,
        identity: str,
        status: str,
        *,
        matter: str | None = None,
        error: str | None = None,
        result_path: str | None = None,
    ):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status=?,updated_at=?,matter=COALESCE(?,matter),error=?,"
                "result_path=COALESCE(?,result_path) WHERE id=?",
                (status, utc_now().isoformat(), matter, error, result_path, identity),
            )

    def thread_matter(self, inbound: InboundEmail) -> str | None:
        for reference in reversed(inbound.references):
            row = self.db.execute(
                "SELECT jobs.matter FROM replies JOIN jobs ON jobs.id=replies.job_id "
                "WHERE replies.message_id=?",
                (reference,),
            ).fetchone()
            if row and row[0]:
                return row[0]
        return None

    def prepare_replies(self, identity: str, replies: list[tuple[int, str, Path]]):
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO replies(job_id,part,message_id,mime_path) VALUES (?,?,?,?)",
                [(identity, part, message_id, str(path)) for part, message_id, path in replies],
            )
            self.db.execute(
                "UPDATE jobs SET status='ready',updated_at=? WHERE id=?",
                (utc_now().isoformat(), identity),
            )

    def has_replies(self, identity: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM replies WHERE job_id=? LIMIT 1", (identity,)).fetchone()
            is not None
        )

    def pending_replies(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM replies WHERE status IN ('ready','uncertain') AND available_at<=? "
                "ORDER BY available_at,job_id,part LIMIT 25",
                (utc_now().isoformat(),),
            )
        ]

    def defer_reconciliation(self, identity: str, part: int):
        with self.db:
            self.db.execute(
                "UPDATE replies SET available_at=? WHERE job_id=? AND part=?",
                ((utc_now() + timedelta(seconds=300)).isoformat(), identity, part),
            )

    def sending(self, identity: str, part: int):
        with self.db:
            self.db.execute(
                "UPDATE replies SET status='sending',attempts=attempts+1 WHERE job_id=? AND part=?",
                (identity, part),
            )

    def defer_reply(
        self, identity: str, part: int, error: str, delay: int = 60, *, count_attempt: bool = True
    ):
        self.set_reply(identity, part, "ready", error)
        with self.db:
            self.db.execute(
                "UPDATE replies SET available_at=?,attempts=MAX(0,attempts-?) "
                "WHERE job_id=? AND part=?",
                (
                    (utc_now() + timedelta(seconds=delay)).isoformat(),
                    int(not count_attempt),
                    identity,
                    part,
                ),
            )

    def set_reply(self, identity: str, part: int, status: str, error: str | None = None):
        with self.db:
            self.db.execute(
                "UPDATE replies SET status=?,error=? WHERE job_id=? AND part=?",
                (status, error, identity, part),
            )
            statuses = {
                row[0]
                for row in self.db.execute(
                    "SELECT status FROM replies WHERE job_id=?",
                    (identity,),
                )
            }
            if statuses == {"sent"}:
                job_status = "sent"
            elif "failed" in statuses:
                job_status = "delivery_failed"
            elif "uncertain" in statuses:
                job_status = "delivery_uncertain"
            else:
                job_status = "ready"
            self.db.execute(
                "UPDATE jobs SET status=?,error=COALESCE(?,error),updated_at=? WHERE id=?",
                (job_status, error, utc_now().isoformat(), identity),
            )

    def summary(self) -> dict:
        return {
            "jobs": dict(self.db.execute("SELECT status,count(*) FROM jobs GROUP BY status")),
            "replies": dict(self.db.execute("SELECT status,count(*) FROM replies GROUP BY status")),
            "started_at": self.get_setting("started_at"),
            "last_poll": self.get_setting("last_poll"),
        }
