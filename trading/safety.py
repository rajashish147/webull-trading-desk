from dataclasses import dataclass, field
import hashlib
import json

LIVE_PHRASES = {"SUBMIT": "SUBMIT LIVE ORDER", "REPLACE": "MODIFY LIVE ORDER", "CANCEL": "CANCEL LIVE ORDER"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def revision(record):
    return digest({key: record.get(key) for key in (
        "id", "environment", "account_fingerprint", "state", "spec", "filled_quantity",
        "broker_order_id", "pending_action", "pending_spec",
    )})


@dataclass(frozen=True)
class LiveConfirmation:
    token: str = field(repr=False)
    acknowledgement: str


@dataclass(frozen=True)
class ExecutionPermit:
    environment: str
    account_id: str = field(repr=False)
    operation: str
    client_order_id: str
    payload_hash: str
    expires_at: float
