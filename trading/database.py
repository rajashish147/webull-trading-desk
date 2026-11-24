from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from trading.models import ACTIVE, DomainError


def now():
    return datetime.now(timezone.utc).isoformat()


class Database:
    """SQLite WAL repository. Order state changes and audit events commit together."""

    def __init__(self, path, identity):
        self.path = Path(path)
        self.identity = identity
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.lock:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                with connection:
                    yield connection
            finally:
                connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                    client_order_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, record TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS orders_state ON orders(state, updated_at);
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                    action TEXT NOT NULL, order_id TEXT, details TEXT NOT NULL
                );
            """)
            row = conn.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            if row and row[0] != self.identity:
                raise ValueError("Database belongs to another broker environment/account.")
            conn.execute("INSERT OR IGNORE INTO metadata VALUES ('identity', ?)", (self.identity,))
            row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if row and row[0] != "1":
                raise ValueError("Unsupported database schema version.")
            conn.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')")
            conn.execute("INSERT OR IGNORE INTO metadata VALUES ('watchlist', '[]')")
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('halt', ?)", (
                json.dumps({"enabled": True, "reason": "Server startup: explicit trading enable required."}),
            ))
            self._audit(conn, "server_start_halted", None, {})
            for row in conn.execute("SELECT record FROM orders WHERE state IN ('SUBMITTING','MODIFYING','CANCELLING')").fetchall():
                record = json.loads(row[0])
                record.update(state="UNKNOWN", last_error="Interrupted broker operation; reconciliation required.")
                self._write(conn, record, "restart_recovery", {})

    @staticmethod
    def _audit(conn, action, order_id, details):
        conn.execute("INSERT INTO audit(timestamp,action,order_id,details) VALUES (?,?,?,?)", (
            now(), action, order_id, json.dumps(details),
        ))

    def _write(self, conn, record, action, details):
        record["updated_at"] = now()
        conn.execute("UPDATE orders SET state=?,updated_at=?,record=? WHERE id=?", (
            record["state"], record["updated_at"], json.dumps(record), record["id"],
        ))
        if action:
            self._audit(conn, action, record["id"], details)
        return record

    def create(self, spec, key):
        with self.transaction() as conn:
            row = conn.execute("SELECT record FROM orders WHERE idempotency_key=?", (key,)).fetchone()
            if row:
                record = json.loads(row[0])
                if record["original_spec"] != spec:
                    raise DomainError("idempotency_conflict", "Idempotency key was used for a different order.")
                return record
            order_id = uuid.uuid4().hex
            record = {
                "id": order_id, "client_order_id": order_id, "state": "DRAFT",
                "spec": spec, "original_spec": spec, "broker_order_id": None,
                "broker_status": None, "filled_quantity": "0", "average_fill_price": None,
                "pending_action": None, "pending_spec": None, "last_error": None,
                "created_at": now(), "updated_at": now(), "last_reconciled_at": None,
            }
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", (
                order_id, key, order_id, "DRAFT", record["created_at"], record["updated_at"], json.dumps(record),
            ))
            self._audit(conn, "draft_created", order_id, {"spec": spec})
            return record

    def get(self, order_id):
        with self.transaction() as conn:
            row = conn.execute("SELECT record FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                raise DomainError("order_not_found", "Local order not found.", 404)
            return json.loads(row[0])

    def update(self, order_id, changes, action, details=None):
        with self.transaction() as conn:
            row = conn.execute("SELECT record FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                raise DomainError("order_not_found", "Local order not found.", 404)
            record = json.loads(row[0])
            record.update(changes)
            return self._write(conn, record, action, details or {})

    def orders(self, limit=100, offset=0, active=False):
        with self.transaction() as conn:
            if active:
                placeholders = ",".join("?" for _ in ACTIVE)
                rows = conn.execute(f"SELECT record FROM orders WHERE state IN ({placeholders}) ORDER BY updated_at", tuple(ACTIVE)).fetchall()
            else:
                rows = conn.execute("SELECT record FROM orders ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def state(self, key):
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def set_state(self, key, value, action):
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))
            self._audit(conn, action, None, value)
        return value

    def audit_event(self, action, order_id=None, details=None):
        with self.transaction() as conn:
            self._audit(conn, action, order_id, details or {})

    def audit(self, after=0, limit=100):
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM audit WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit)).fetchall()
            return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def daily_submissions(self):
        with self.transaction() as conn:
            return conn.execute("SELECT COUNT(*) FROM audit WHERE action='submit_sent' AND timestamp>=?", (now()[:10],)).fetchone()[0]

    def report(self):
        with self.transaction() as conn:
            return {
                "order_counts": dict(conn.execute("SELECT state,COUNT(*) FROM orders GROUP BY state").fetchall()),
                "submission_attempts_today_utc": self.daily_submissions(),
                "audit_events": conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
                "note": "Local order ledger only; use broker portfolio data for financial P&L.",
            }
