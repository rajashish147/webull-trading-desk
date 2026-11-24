from decimal import Decimal
from datetime import datetime, timezone
import hashlib
import secrets
import threading
import time

from pydantic import ValidationError

from trading.database import now
from trading.models import BrokerError, DomainError, OrderSpec, ReplaceOrder, TERMINAL
from trading.risk import RiskEngine, number
from trading.safety import ExecutionPermit, LIVE_PHRASES, LiveConfirmation, digest, revision


def broker_number(value, name):
    try:
        return number(value, name)
    except ValueError as exc:
        raise BrokerError(f"Invalid broker order field: {name}; local state retained.") from exc


class TradingService:
    def __init__(self, config, database, broker):
        self.config, self.database, self.broker = config, database, broker
        self.risk = RiskEngine(config.limits, database, broker)
        self.risk.live_mode = config.broker.environment == "production"
        # Hold across fresh risk snapshots, durable intent, broker call, and acknowledgement.
        self.lock = threading.RLock()
        self._arm_deadline = 0
        self._arm_nonce = ""
        self._approval_deadlines = {}

    def _writable(self, operation="SUBMIT"):
        if not self.broker.connected:
            raise DomainError("broker_not_connected", "Connect the broker first.", 503)
        if self.config.broker.environment == "production" and operation != "CANCEL":
            if not self.config.broker.enable_live_trading:
                raise DomainError("live_config_disabled", "Set WEBULL_ENABLE_LIVE_TRADING=true locally and restart only after reviewing live limits.", 403)
            if not self.live_armed():
                raise DomainError("live_not_armed", "Live trading is disarmed or its arming window expired.")

    def _record(self, order_id):
        record = self.database.get(order_id)
        if record.get("account_fingerprint") != self.config.identity or record.get("environment") != self.config.broker.environment:
            raise DomainError("order_environment_mismatch", "This order belongs to another environment/account.")
        return record

    def live_armed(self):
        arm = self.database.state("live_arm") or {}
        return bool(self.config.broker.environment == "production" and self.config.broker.enable_live_trading
                    and arm.get("armed") and not self.database.state("halt")["enabled"]
                    and arm.get("expires_at", 0) > time.time() and self._arm_deadline > time.monotonic())

    def arm_live(self, confirmation):
        with self.lock:
            self.set_halt(True, "Revalidating live arming")
            if self.config.broker.environment != "production" or confirmation != "ENABLE LIVE TRADING":
                raise DomainError("live_arm_confirmation", "Select live and type ENABLE LIVE TRADING.")
            if not self.config.broker.enable_live_trading:
                raise DomainError("live_config_disabled", "Live execution is disabled in .env.", 403)
            if not self.broker.connected:
                raise DomainError("broker_not_connected", "Connect and validate the production account first.", 503)
            if any(item["pending_action"] or item["state"] in {"UNKNOWN", "SUBMITTING", "ACKNOWLEDGED"} for item in self.database.orders(active=True)):
                raise DomainError("pending_mutation", "Resolve uncertain broker outcomes before arming live trading.")
            readiness = self.risk.readiness()
            if not readiness["ready"]:
                raise DomainError("live_readiness_failed", "Production account readiness checks failed.", details=readiness)
            expires = time.time() + self.config.live_arm_seconds
            self._arm_deadline = time.monotonic() + self.config.live_arm_seconds
            self._arm_nonce = secrets.token_urlsafe(24)
            self.database.set_state("live_arm", {"armed": True, "expires_at": expires}, "live_armed")
            self.database.set_state("halt", {"enabled": False, "reason": "Explicit, time-limited live arm", "updated_at": now()}, "kill_switch_changed")
            return {"armed": True, "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(), "readiness": readiness}

    def _broker_preview(self, spec, record, risk):
        preview = self.broker.preview(spec.payload(record["client_order_id"]))
        if self.config.broker.environment == "production":
            try:
                cost = number(preview.get("estimated_cost"), "estimated_cost")
                fees = number(preview.get("estimated_transaction_fee"), "estimated_transaction_fee")
                if preview.get("currency", risk["metrics"].get("settlement_currency")) != "USD" or cost < 0 or fees < 0 or spec.side == "BUY" and cost == 0:
                    raise ValueError()
                if cost + fees > self.config.limits.max_order_notional or (spec.side == "BUY" and cost + fees > Decimal(risk["metrics"]["cash_available_after_local_reservations"])):
                    raise DomainError("broker_preview_rejected", "Broker estimated cost/fees exceed the live budget or available settled cash.")
            except (ValueError, TypeError, AttributeError, KeyError) as exc:
                raise BrokerError("Invalid broker live cost preview; nothing submitted.") from exc
        return preview

    def _intent(self, record, operation, spec):
        return digest({"account": self.config.identity, "operation": operation, "revision": revision(record),
                       "spec": spec.model_dump(mode="json") if spec else None})

    def live_approval(self, order_id, request):
        with self.lock:
            if self.config.broker.environment != "production":
                raise DomainError("live_profile_required", "Live approvals are only issued in the production profile.")
            self._writable(request.operation)
            record = self._record(order_id)
            if request.operation == "SUBMIT":
                if record["state"] != "DRAFT":
                    raise DomainError("invalid_order_state", "Only a draft can receive submission approval.")
                if request.quantity is not None or request.limit_price is not None:
                    raise DomainError("invalid_approval", "Submit approval must use the immutable draft parameters.", 422)
                spec = OrderSpec.model_validate(record["spec"])
            else:
                record = self.reconcile(order_id)
                if record["state"] not in {"OPEN", "PARTIALLY_FILLED"} or record["pending_action"]:
                    raise DomainError("invalid_order_state", "Resolve pending operations; only open orders can be approved for mutation.")
                if request.operation == "REPLACE":
                    replacement = ReplaceOrder.model_validate({"quantity": request.quantity, "limit_price": request.limit_price})
                    spec = OrderSpec.model_validate({**record["spec"], **replacement.model_dump(mode="json")})
                else:
                    if request.quantity is not None or request.limit_price is not None:
                        raise DomainError("invalid_approval", "Cancellation approval cannot include replacement parameters.", 422)
                    spec = None
            risk = preview = None
            if spec:
                risk = self._risk_gate(spec, order_id, Decimal(record["filled_quantity"]))
                preview = self._broker_preview(spec, record, risk)
                self._writable(request.operation)
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            expires = time.time() + 60
            nonce = self._arm_nonce if request.operation != "CANCEL" else ""
            self.database.issue_approval(token_hash, order_id, request.operation, self._intent(record, request.operation, spec), nonce, expires)
            self._approval_deadlines = {key: deadline for key, deadline in self._approval_deadlines.items() if deadline > time.monotonic()}
            self._approval_deadlines[token_hash] = time.monotonic() + 60
            return {"token": token, "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                    "acknowledgement": LIVE_PHRASES[request.operation], "risk": risk, "broker_preview": preview,
                    "intent": {"environment": "production", "account_hint": self.config.broker.account_id[-4:],
                               "order_id": order_id, "operation": request.operation,
                               "spec": spec.model_dump(mode="json") if spec else record["spec"]}}

    def _permit(self, record, operation, spec, confirmation):
        if self.config.broker.environment == "sandbox":
            if confirmation:
                raise DomainError("unexpected_live_confirmation", "Do not use live confirmation in sandbox.")
            return None
        if not isinstance(confirmation, LiveConfirmation) or confirmation.acknowledgement != LIVE_PHRASES[operation]:
            raise DomainError("live_confirmation_required", f"Obtain approval and acknowledge {LIVE_PHRASES[operation]}.")
        token_hash = hashlib.sha256(confirmation.token.encode()).hexdigest()
        if self._approval_deadlines.get(token_hash, 0) <= time.monotonic():
            raise DomainError("live_confirmation_invalid", "Live confirmation expired or was invalidated.")
        nonce = self._arm_nonce if operation != "CANCEL" else ""
        expiry = self.database.consume_approval(token_hash, record["id"], operation, self._intent(record, operation, spec), nonce)
        self._approval_deadlines.pop(token_hash, None)
        if operation == "SUBMIT":
            payload = spec.payload(record["client_order_id"])
        elif operation == "REPLACE":
            payload = {"client_order_id": record["client_order_id"], "quantity": str(spec.quantity), "limit_price": format(spec.limit_price, "f")}
        else:
            payload = {"client_order_id": record["client_order_id"]}
        return ExecutionPermit("production", self.config.broker.account_id, operation, record["client_order_id"], digest(payload), min(expiry, time.time() + 10))

    def create(self, spec, idempotency_key):
        with self.lock:
            if self.config.identity != self.database.identity:
                raise DomainError("account_mismatch", "Database account does not match the selected profile.")
            return self.database.create(spec.model_dump(mode="json"), idempotency_key)

    def preview(self, order_id):
        with self.lock:
            record = self._record(order_id)
            if record["state"] != "DRAFT":
                raise DomainError("invalid_order_state", "Only drafts can be previewed.")
            spec = OrderSpec.model_validate(record["spec"])
            risk = self.risk.check(spec, ignore_halt=True)
            result = {"risk": risk, "broker_preview": None, "submitted": False}
            if risk["allowed"]:
                result["broker_preview"] = self._broker_preview(spec, record, risk)
            self.database.audit_event("order_previewed", order_id, {"allowed": risk["allowed"]})
            return result

    def _risk_gate(self, spec, order_id=None, filled_quantity=Decimal(0)):
        result = self.risk.check(spec, ignore_id=order_id, filled_quantity=filled_quantity)
        if not result["allowed"]:
            self.database.audit_event("risk_rejected", order_id, result)
            raise DomainError("risk_rejected", "Order failed pre-trade risk checks.", details=result)
        return result

    def submit(self, order_id, confirmation=None):
        with self.lock:
            record = self._record(order_id)
            # Retrying a submit HTTP request never submits this order a second time.
            if record["state"] != "DRAFT":
                return record
            self._writable()
            spec = OrderSpec.model_validate(record["spec"])
            risk = self._risk_gate(spec, order_id)
            if self.config.broker.environment == "production":
                self._broker_preview(spec, record, risk)
                self._writable()
            permit = self._permit(record, "SUBMIT", spec, confirmation)
            self.database.update(order_id, {
                "state": "SUBMITTING", "pending_action": "SUBMIT", "last_error": None,
            }, "submit_sent", {"spec": record["spec"]})
            try:
                ack = self.broker.place(spec.payload(record["client_order_id"]), permit=permit)
                if not isinstance(ack, dict) or ack.get("client_order_id") != record["client_order_id"] or not ack.get("order_id"):
                    raise BrokerError("Unrecognized order acknowledgement; reconcile before retrying.")
                self.database.update(order_id, {
                    "state": "ACKNOWLEDGED", "broker_order_id": ack["order_id"], "pending_action": None,
                }, "submit_acknowledged")
            except DomainError as exc:
                definitive = getattr(exc, "definitive", False) or exc.code == "broker_rate_limit"
                return self.database.update(order_id, {
                    "state": "REJECTED" if definitive else "UNKNOWN",
                    "pending_action": None if definitive else "SUBMIT", "last_error": str(exc),
                }, "submit_failed", {"definitive": definitive})
            except Exception:
                return self.database.update(order_id, {
                    "state": "UNKNOWN", "pending_action": "SUBMIT", "last_error": "Unexpected submission failure; reconciliation required.",
                }, "submit_uncertain")
            return self._reconcile_or_record_error(order_id)

    def reconcile(self, order_id):
        with self.lock:
            try:
                return self._reconcile(order_id)
            except BrokerError as error:
                record = self._record(order_id)
                if record["state"] not in TERMINAL and error.status != 429:
                    self.database.update(order_id, {"state": "UNKNOWN", "last_error": str(error)}, "reconciliation_uncertain")
                raise

    def _reconcile(self, order_id):
        with self.lock:
            record = self._record(order_id)
            if record["state"] == "DRAFT" or (record["state"] in TERMINAL and not record["broker_order_id"]):
                return record
            detail = self.broker.detail(record["client_order_id"])
            items = detail.get("orders", []) if isinstance(detail, dict) else []
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                raise BrokerError("Unexpected broker order-detail schema; local state retained.")
            order = items[0]
            if order.get("client_order_id") != record["client_order_id"] or order.get("symbol") != record["spec"]["symbol"] or order.get("side") != record["spec"]["side"]:
                raise BrokerError("Broker order identity mismatch; local state retained.")
            if order.get("instrument_type") != "EQUITY" or order.get("order_type") != "LIMIT" or detail.get("combo_type", "NORMAL") != "NORMAL":
                raise BrokerError("Unexpected instrument/order type in broker detail; review required.")
            total = broker_number(order.get("total_quantity"), "total_quantity")
            filled = broker_number(order.get("filled_quantity"), "filled_quantity")
            if total != total.to_integral_value() or total <= 0 or filled < 0 or filled > total or filled < Decimal(record["filled_quantity"]):
                raise BrokerError("Invalid/nonmonotonic broker fill quantities; local state retained.")
            status = str(order.get("status", "UNKNOWN")).upper()
            state = {
                "SUBMITTED": "OPEN", "NEW": "OPEN", "WORKING": "OPEN", "ACCEPTED": "OPEN",
                "PARTIALLY_FILLED": "PARTIALLY_FILLED", "PARTIAL_FILLED": "PARTIALLY_FILLED",
                "FILLED": "FILLED", "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED",
                "REJECTED": "REJECTED", "EXPIRED": "EXPIRED",
            }.get(status, "UNKNOWN")
            if state == "FILLED" and filled != total:
                raise BrokerError("FILLED order has inconsistent quantities; local state retained.")
            if filled > 0 and state == "OPEN":
                state = "PARTIALLY_FILLED"
            try:
                spec = OrderSpec.model_validate({
                    **record["spec"], "quantity": int(total),
                    "limit_price": order.get("limit_price", record["spec"]["limit_price"]),
                }).model_dump(mode="json")
            except ValidationError as exc:
                raise BrokerError("Invalid broker order values; local state retained.") from exc
            pending = record["pending_action"]
            if state in TERMINAL or pending == "SUBMIT" and state != "UNKNOWN":
                pending = None
            elif pending == "REPLACE":
                desired = OrderSpec.model_validate(record["pending_spec"])
                if desired.quantity == int(total) and desired.limit_price == Decimal(spec["limit_price"]):
                    pending = None
                elif state not in TERMINAL:
                    state = "MODIFYING"
            elif pending == "CANCEL" and state not in TERMINAL:
                state = "CANCELLING"
            average = order.get("avg_filled_price", order.get("average_filled_price"))
            if average is not None:
                average_number = broker_number(average, "average_fill_price")
                if filled > 0 and average_number <= 0:
                    raise BrokerError("Invalid broker average fill price; local state retained.")
                average = str(average_number)
            changes = {
                "state": state, "broker_status": status, "spec": spec,
                "broker_order_id": order.get("order_id", record["broker_order_id"]),
                "filled_quantity": str(filled), "average_fill_price": average,
                "pending_action": pending, "pending_spec": record["pending_spec"] if pending else None,
                "last_error": "Broker status is not recognized; manual review required." if state == "UNKNOWN" else None,
                "last_reconciled_at": now(),
            }
            changed = any(record.get(key) != value for key, value in changes.items() if key != "last_reconciled_at")
            return self.database.update(order_id, changes, "order_reconciled" if changed else None,
                                        {"broker_status": status, "filled_quantity": str(filled)})

    def _reconcile_or_record_error(self, order_id):
        try:
            return self.reconcile(order_id)
        except Exception:
            return self.database.update(order_id, {
                "last_error": "Order acknowledgement received, but detail reconciliation failed. Do not resubmit.",
            }, "reconciliation_failed")

    def replace(self, order_id, request, confirmation=None):
        with self.lock:
            self._writable()
            record = self._record(order_id)
            if record["pending_action"]:
                raise DomainError("pending_mutation", "Reconcile the pending operation before replacing.")
            record = self.reconcile(order_id)
            if record["state"] not in {"OPEN", "PARTIALLY_FILLED"}:
                raise DomainError("invalid_order_state", "Only open/partially filled orders can be replaced.")
            spec = OrderSpec.model_validate({**record["spec"], **request.model_dump(mode="json")})
            risk = self._risk_gate(spec, order_id, Decimal(record["filled_quantity"]))
            if self.config.broker.environment == "production":
                self._broker_preview(spec, record, risk)
                self._writable("REPLACE")
            permit = self._permit(record, "REPLACE", spec, confirmation)
            self.database.update(order_id, {
                "state": "MODIFYING", "pending_action": "REPLACE", "pending_spec": spec.model_dump(mode="json"),
                "last_error": None,
            }, "replace_sent", {"spec": spec.model_dump(mode="json")})
            try:
                ack = self.broker.replace(record["client_order_id"], spec.quantity, format(spec.limit_price, "f"), permit=permit)
                if not isinstance(ack, dict) or ack.get("client_order_id") != record["client_order_id"] or not ack.get("order_id"):
                    raise BrokerError("Unrecognized replacement acknowledgement; reconcile before another action.")
            except DomainError as exc:
                return self._mutation_error(record, exc, "REPLACE")
            except Exception:
                return self._mutation_error(record, BrokerError("Replacement outcome is uncertain."), "REPLACE")
            return self._reconcile_or_record_error(order_id)

    def _mutation_error(self, original, error, action):
        definitive = getattr(error, "definitive", False) or error.code == "broker_rate_limit"
        return self.database.update(original["id"], {
            "state": original["state"] if definitive else "UNKNOWN",
            "pending_action": None if definitive else action,
            "pending_spec": None if definitive else self.database.get(original["id"])["pending_spec"],
            "last_error": str(error),
        }, action.lower() + "_failed", {"definitive": definitive})

    def cancel(self, order_id, confirmation=None):
        with self.lock:
            self._writable("CANCEL")
            record = self._record(order_id)
            if record["state"] in TERMINAL or record["pending_action"] == "CANCEL":
                return record
            if record["state"] == "DRAFT":
                return self.database.update(order_id, {"state": "CANCELLED"}, "draft_cancelled")
            record = self.reconcile(order_id)
            if record["state"] in TERMINAL:
                return record
            if record["pending_action"]:
                raise DomainError("pending_mutation", "Reconcile the previous broker mutation before cancelling.")
            permit = self._permit(record, "CANCEL", None, confirmation)
            # Emergency cancellation remains available while the kill switch is enabled.
            self.database.update(order_id, {
                "state": "CANCELLING", "pending_action": "CANCEL", "last_error": None,
            }, "cancel_sent")
            try:
                ack = self.broker.cancel(record["client_order_id"], permit=permit)
                if not isinstance(ack, dict) or ack.get("client_order_id") != record["client_order_id"] or not ack.get("order_id"):
                    raise BrokerError("Unrecognized cancellation acknowledgement; reconcile before another action.")
            except DomainError as exc:
                return self._mutation_error(record, exc, "CANCEL")
            except Exception:
                return self._mutation_error(record, BrokerError("Cancellation outcome is uncertain."), "CANCEL")
            return self._reconcile_or_record_error(order_id)

    def reconcile_active(self, limit=5):
        with self.lock:
            results = []
            for record in self.database.orders(active=True)[:limit]:
                try:
                    results.append(self.reconcile(record["id"]))
                except Exception:
                    results.append(self.database.update(record["id"], {
                        "last_error": "Reconciliation unavailable; broker state remains uncertain.",
                    }, "reconciliation_failed"))
            return results

    def set_halt(self, enabled, reason):
        with self.lock:
            if not enabled:
                if self.config.broker.environment == "production":
                    raise DomainError("live_arm_required", "Use explicit time-limited live arming instead of disabling the kill switch.")
                self._writable()
                if any(item["pending_action"] or item["state"] in {"UNKNOWN", "ACKNOWLEDGED"} for item in self.database.orders(active=True)):
                    raise DomainError("pending_mutation", "Resolve uncertain orders before enabling trading.")
            if enabled:
                self._arm_deadline = 0
                self._arm_nonce = ""
                self._approval_deadlines.clear()
                self.database.invalidate_approvals()
                self.database.set_state("live_arm", {"armed": False, "expires_at": 0}, "live_disarmed")
            return self.database.set_state("halt", {"enabled": enabled, "reason": reason, "updated_at": now()}, "kill_switch_changed")
