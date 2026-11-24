from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
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
            existing = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
            if existing:
                old_identity = conn.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
                old_version = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
                if old_identity and old_identity[0] != self.identity:
                    raise ValueError("Database belongs to another broker environment/account.")
                if old_version and old_version[0] not in {"1", "2"}:
                    raise ValueError("Unsupported database schema version.")
                if old_version and old_version[0] == "1":
                    backup_dir = self.path.parent / "backups"
                    backup_dir.mkdir(exist_ok=True)
                    backup_path = backup_dir / f"{self.path.stem}_schema1_{uuid.uuid4().hex}.sqlite3"
                    target = sqlite3.connect(backup_path)
                    try:
                        conn.backup(target)
                    finally:
                        target.close()
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
                CREATE TABLE IF NOT EXISTS live_approvals (
                    token_hash TEXT PRIMARY KEY, order_id TEXT NOT NULL, operation TEXT NOT NULL,
                    intent_hash TEXT NOT NULL, arm_nonce TEXT NOT NULL,
                    expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
                );
            """)
            row = conn.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            if row and row[0] != self.identity:
                raise ValueError("Database belongs to another broker environment/account.")
            conn.execute("INSERT OR IGNORE INTO metadata VALUES ('identity', ?)", (self.identity,))
            row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if row and row[0] not in {"1", "2"}:
                raise ValueError("Unsupported database schema version.")
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_version', '2')")
            conn.execute("INSERT OR IGNORE INTO metadata VALUES ('watchlist', '[]')")
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('halt', ?)", (
                json.dumps({"enabled": True, "reason": "Server startup: explicit trading enable required."}),
            ))
            self._audit(conn, "server_start_halted", None, {})
            conn.execute("INSERT OR REPLACE INTO metadata VALUES ('live_arm', ?)", (json.dumps({"expires_at": 0, "armed": False}),))
            conn.execute("UPDATE live_approvals SET consumed=1 WHERE consumed=0")
            for row in conn.execute("SELECT record FROM orders").fetchall():
                record = json.loads(row[0])
                if "environment" not in record:
                    record.update(environment=self.identity.split(":", 1)[0], account_fingerprint=self.identity)
                    self._write(conn, record, "record_scope_migrated", {})
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
                "environment": self.identity.split(":", 1)[0], "account_fingerprint": self.identity,
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

    def issue_approval(self, token_hash, order_id, operation, intent_hash, arm_nonce, expires_at):
        with self.transaction() as conn:
            conn.execute("INSERT INTO live_approvals VALUES (?,?,?,?,?,?,0)",
                         (token_hash, order_id, operation, intent_hash, arm_nonce, expires_at))
            self._audit(conn, "live_confirmation_issued", order_id, {"operation": operation, "expires_at": expires_at})

    def consume_approval(self, token_hash, order_id, operation, intent_hash, arm_nonce):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM live_approvals WHERE token_hash=?", (token_hash,)).fetchone()
            if not row or row["consumed"] or row["expires_at"] <= time.time() or any(
                row[key] != value for key, value in {"order_id": order_id, "operation": operation,
                                                    "intent_hash": intent_hash, "arm_nonce": arm_nonce}.items()
            ):
                raise DomainError("live_confirmation_invalid", "Live confirmation expired, was already used, or does not match this exact order.")
            conn.execute("UPDATE live_approvals SET consumed=1 WHERE token_hash=?", (token_hash,))
            self._audit(conn, "live_confirmation_consumed", order_id, {"operation": operation})
            return row["expires_at"]

    def invalidate_approvals(self):
        with self.transaction() as conn:
            conn.execute("UPDATE live_approvals SET consumed=1 WHERE consumed=0")

    def report(self):
        with self.transaction() as conn:
            return {
                "order_counts": dict(conn.execute("SELECT state,COUNT(*) FROM orders GROUP BY state").fetchall()),
                "submission_attempts_today_utc": self.daily_submissions(),
                "audit_events": conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
                "note": "Local order ledger only; use broker portfolio data for financial P&L.",
            }
