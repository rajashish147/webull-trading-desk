from decimal import Decimal
import threading

from pydantic import ValidationError

from trading.database import now
from trading.models import BrokerError, DomainError, OrderSpec, TERMINAL
from trading.risk import RiskEngine, number


def broker_number(value, name):
    try:
        return number(value, name)
    except ValueError as exc:
        raise BrokerError(f"Invalid broker order field: {name}; local state retained.") from exc


class TradingService:
    def __init__(self, config, database, broker):
        self.config, self.database, self.broker = config, database, broker
        self.risk = RiskEngine(config.limits, database, broker)
        # Hold across fresh risk snapshots, durable intent, broker call, and acknowledgement.
        self.lock = threading.RLock()

    def _writable(self):
        if self.config.broker.environment != "sandbox":
            raise DomainError("production_read_only", "Production mutations are disabled in this backend.", 403)
        if not self.broker.connected:
            raise DomainError("broker_not_connected", "Connect the broker first.", 503)

    def create(self, spec, idempotency_key):
        with self.lock:
            return self.database.create(spec.model_dump(mode="json"), idempotency_key)

    def preview(self, order_id):
        with self.lock:
            record = self.database.get(order_id)
            if record["state"] != "DRAFT":
                raise DomainError("invalid_order_state", "Only drafts can be previewed.")
            spec = OrderSpec.model_validate(record["spec"])
            risk = self.risk.check(spec, ignore_halt=True)
            result = {"risk": risk, "broker_preview": None, "submitted": False}
            if risk["allowed"]:
                result["broker_preview"] = self.broker.preview(spec.payload(record["client_order_id"]))
            self.database.audit_event("order_previewed", order_id, {"allowed": risk["allowed"]})
            return result

    def _risk_gate(self, spec, order_id=None, filled_quantity=Decimal(0)):
        result = self.risk.check(spec, ignore_id=order_id, filled_quantity=filled_quantity)
        if not result["allowed"]:
            self.database.audit_event("risk_rejected", order_id, result)
            raise DomainError("risk_rejected", "Order failed pre-trade risk checks.", details=result)

    def submit(self, order_id):
        with self.lock:
            self._writable()
            record = self.database.get(order_id)
            # Retrying a submit HTTP request never submits this order a second time.
            if record["state"] != "DRAFT":
                return record
            spec = OrderSpec.model_validate(record["spec"])
            self._risk_gate(spec, order_id)
            self.database.update(order_id, {
                "state": "SUBMITTING", "pending_action": "SUBMIT", "last_error": None,
            }, "submit_sent", {"spec": record["spec"]})
            try:
                ack = self.broker.place(spec.payload(record["client_order_id"]))
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
            record = self.database.get(order_id)
            if record["state"] == "DRAFT" or (record["state"] in TERMINAL and not record["broker_order_id"]):
                return record
            detail = self.broker.detail(record["client_order_id"])
            items = detail.get("orders", []) if isinstance(detail, dict) else []
            if len(items) != 1:
                raise BrokerError("Unexpected broker order-detail schema; local state retained.")
            order = items[0]
            if order.get("client_order_id") != record["client_order_id"] or order.get("symbol") != record["spec"]["symbol"] or order.get("side") != record["spec"]["side"]:
                raise BrokerError("Broker order identity mismatch; local state retained.")
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

    def replace(self, order_id, request):
        with self.lock:
            self._writable()
            record = self.database.get(order_id)
            if record["pending_action"]:
                raise DomainError("pending_mutation", "Reconcile the pending operation before replacing.")
            record = self.reconcile(order_id)
            if record["state"] not in {"OPEN", "PARTIALLY_FILLED"}:
                raise DomainError("invalid_order_state", "Only open/partially filled orders can be replaced.")
            spec = OrderSpec.model_validate({**record["spec"], **request.model_dump(mode="json")})
            self._risk_gate(spec, order_id, Decimal(record["filled_quantity"]))
            self.database.update(order_id, {
                "state": "MODIFYING", "pending_action": "REPLACE", "pending_spec": spec.model_dump(mode="json"),
                "last_error": None,
            }, "replace_sent", {"spec": spec.model_dump(mode="json")})
            try:
                self.broker.replace(record["client_order_id"], spec.quantity, format(spec.limit_price, "f"))
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

    def cancel(self, order_id):
        with self.lock:
            self._writable()
            record = self.database.get(order_id)
            if record["state"] in TERMINAL or record["pending_action"] == "CANCEL":
                return record
            if record["state"] == "DRAFT":
                return self.database.update(order_id, {"state": "CANCELLED"}, "draft_cancelled")
            record = self.reconcile(order_id)
            if record["state"] in TERMINAL:
                return record
            if record["pending_action"]:
                raise DomainError("pending_mutation", "Reconcile the previous broker mutation before cancelling.")
            # Emergency cancellation remains available while the kill switch is enabled.
            self.database.update(order_id, {
                "state": "CANCELLING", "pending_action": "CANCEL", "last_error": None,
            }, "cancel_sent")
            try:
                self.broker.cancel(record["client_order_id"])
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
                self._writable()
                if any(item["pending_action"] or item["state"] in {"UNKNOWN", "ACKNOWLEDGED"} for item in self.database.orders(active=True)):
                    raise DomainError("pending_mutation", "Resolve uncertain orders before enabling trading.")
            return self.database.set_state("halt", {"enabled": enabled, "reason": reason, "updated_at": now()}, "kill_switch_changed")
