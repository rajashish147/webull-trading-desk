"""Bounded, non-atomic two-stock sum strategy. Never auto-arms or retries mutations."""
from decimal import Decimal
import time
import uuid

from trading.database import now
from trading.models import DomainError, OrderSpec, SumStrategySpec, TERMINAL
from trading.risk import number
from trading.safety import StrategyGrant, digest

RUNNING = {"WAITING_ENTRY", "ENTERING", "HOLDING", "EXITING", "CLEANUP"}


class SumStrategyEngine:
    def __init__(self, service):
        self.service, self.db, self.broker, self.config = service, service.database, service.broker, service.config

    def create(self, spec):
        with self.service.lock:
            if len(self.db.strategies()) >= 20:
                raise DomainError("strategy_limit", "At most 20 strategies per profile.")
            record = {"id": uuid.uuid4().hex, "created_at": now(), "updated_at": now(),
                      "environment": self.config.broker.environment, "account_fingerprint": self.config.identity,
                      "spec": spec.model_dump(mode="json"), "status": "DRAFT", "cycle": 0,
                      "cycles_started": 0, "cycles_completed": 0, "seen_above": False,
                      "last_ask_sum": None, "last_bid_sum": None, "intent": None, "order_ids": [],
                      "expires_at": 0, "next_entry_at": 0, "last_error": None, "cleanup_deadline": 0}
            return self.db.save_strategy(record, "strategy_created")

    def get(self, strategy_id):
        record = self.db.strategy(strategy_id)
        return {**record, "owned": {key: str(value) for key, value in self.db.strategy_owned(strategy_id).items()},
                "orders": self.db.strategy_orders(strategy_id)}

    def active(self):
        return [item for item in self.db.strategies() if item["status"] in RUNNING]

    def _spec(self, record):
        if record["account_fingerprint"] != self.config.identity or record["environment"] != self.config.broker.environment:
            raise DomainError("strategy_scope_mismatch", "Strategy belongs to another account/environment.")
        return SumStrategySpec.model_validate(record["spec"])

    def arm(self, strategy_id, confirmation):
        with self.service.lock:
            record = self.db.strategy(strategy_id)
            spec = self._spec(record)
            live = self.config.broker.environment == "production"
            phrase = "ARM LIVE STRATEGY" if live else "ARM SANDBOX STRATEGY"
            if confirmation != phrase:
                raise DomainError("strategy_confirmation", f"Type {phrase} to authorize this exact strategy policy.")
            self.service._writable()
            if self.db.state("halt")["enabled"]:
                raise DomainError("strategy_halted", "Enable the selected environment before arming a strategy.")
            if live and not self.config.enable_live_strategies:
                raise DomainError("live_strategies_disabled", "TRADING_ENABLE_LIVE_STRATEGIES is false in .env.", 403)
            if live and self.config.reconcile_interval == 0:
                raise DomainError("strategy_scheduler_disabled", "Live automation requires the backend reconciliation/evaluation worker enabled.")
            if any(item["id"] != strategy_id for item in self.active()) or record["status"] in RUNNING:
                raise DomainError("strategy_already_running", "Only one strategy can be armed per profile; pause it before re-arming.")
            orders = self.db.strategy_orders(strategy_id)
            if any(order["state"] not in TERMINAL for order in orders):
                raise DomainError("strategy_recovery_required", "Reconcile/cancel all working or uncertain strategy orders before arming again.")
            if record["status"] == "RELEASED":
                raise DomainError("strategy_released", "Released allocations cannot be re-armed; create a new strategy.")
            owned = self.db.strategy_owned(strategy_id)
            holding = any(value > 0 for value in owned.values())
            if holding and any(owned.get(symbol, Decimal(0)) != spec.quantity for symbol in (spec.symbol_a, spec.symbol_b)):
                raise DomainError("strategy_orphan_exposure", "Incomplete/unbalanced fills require manual recovery, not automatic re-entry.")
            if record["cycles_started"] >= spec.max_cycles and not holding:
                raise DomainError("strategy_cycles_exhausted", "The configured entry-attempt limit is exhausted.")
            if spec.quantity > self.config.limits.max_order_quantity or spec.max_pair_notional > self.config.strategy_max_pair_notional:
                raise DomainError("strategy_budget", "Strategy quantity/pair budget exceeds the configured policy.")
            if spec.entry_sum * spec.quantity * (1 + self.config.limits.fee_buffer) > spec.max_pair_notional:
                raise DomainError("strategy_budget", "Pair entry threshold plus fee buffer exceeds its own budget.")
            planned = 4 * (spec.max_cycles - record["cycles_started"]) + (2 if holding else 0)
            if self.db.daily_submissions() + planned > self.config.limits.max_daily_orders:
                raise DomainError("strategy_daily_budget", "Remaining paired cycles need more submission capacity than the global UTC daily limit. Review limits; none are automatically increased.")
            readiness = self.service.risk.readiness()
            if not readiness["ready"]:
                raise DomainError("strategy_readiness", "Broker readiness failed.", details=readiness)
            expiry = time.time() + spec.duration_seconds
            if live:
                expiry = min(expiry, self.db.state("live_arm")["expires_at"])
            grant = StrategyGrant(strategy_id, digest(record["spec"]), expiry, time.monotonic() + max(0, expiry - time.time()))
            self.service._strategy_grants[strategy_id] = grant
            record.update(status="HOLDING" if holding else "WAITING_ENTRY", expires_at=expiry,
                          seen_above=False, last_error=None, intent=None if not holding else record["intent"])
            self.db.save_strategy(record, "strategy_armed")
            return self.get(strategy_id)

    def pause(self, strategy_id, reason="Operator paused strategy"):
        with self.service.lock:
            record = self.db.strategy(strategy_id)
            self.service._strategy_grants.pop(strategy_id, None)
            invalid_allocation = False
            try:
                owned = self.db.strategy_owned(strategy_id)
            except DomainError:
                owned = {}
                invalid_allocation = True
            active = any(order["state"] not in TERMINAL | {"DRAFT"} for order in self.db.strategy_orders(strategy_id))
            record.update(status="RECOVERY_REQUIRED" if active or invalid_allocation or any(value > 0 for value in owned.values()) else "PAUSED",
                          expires_at=0, last_error=reason)
            for order in self.db.strategy_orders(strategy_id):
                if order["state"] == "DRAFT":
                    self.db.update(order["id"], {"state": "CANCELLED"}, "strategy_unsent_draft_cancelled")
            self.db.save_strategy(record, "strategy_paused")
            return {**record, "owned": None, "orders": self.db.strategy_orders(strategy_id)} if invalid_allocation else self.get(strategy_id)

    def release(self, strategy_id):
        with self.service.lock:
            record = self.db.strategy(strategy_id)
            if record["status"] in RUNNING or any(order["state"] not in TERMINAL for order in self.db.strategy_orders(strategy_id)):
                raise DomainError("strategy_recovery_required", "Pause and reconcile/cancel all strategy orders before releasing holdings.")
            owned = self.db.strategy_owned(strategy_id)
            held = {}
            for position in self.broker.positions():
                if position.get("instrument_type") == "EQUITY" and position.get("currency") == "USD":
                    held[position["symbol"]] = held.get(position["symbol"], Decimal(0)) + number(position.get("quantity"), "quantity")
            reserved = self.db.reserved_shares()
            if any(held.get(symbol, Decimal(0)) < quantity for symbol, quantity in reserved.items()):
                raise DomainError("strategy_ownership_mismatch", "Broker holdings do not cover recorded allocations; review externally before release.")
            record.update(status="RELEASED", expires_at=0, released_holdings={key: str(value) for key, value in owned.items()},
                          last_error="Holdings explicitly released to manual management. No trade was placed.")
            self.service._strategy_grants.pop(strategy_id, None)
            self.db.save_strategy(record, "strategy_holdings_released")
            return self.get(strategy_id)

    def _quotes(self, spec):
        data = self.broker.quotes([spec.symbol_a, spec.symbol_b])
        if not isinstance(data, list) or len(data) != 2 or {row.get("symbol") for row in data} != {spec.symbol_a, spec.symbol_b}:
            raise DomainError("strategy_quotes", "A complete unique two-stock snapshot is required.")
        quotes, stamps = {}, []
        for row in data:
            stamp = number(row.get("quote_time"), "quote_time") / 1000
            age = Decimal(str(time.time())) - stamp
            ask, bid = number(row.get("ask"), "ask"), number(row.get("bid"), "bid")
            if ask <= 0 or bid <= 0 or ask < bid or row.get("trade_status") != "RTH" or age < -5 or age > min(15, self.config.limits.max_quote_age_seconds):
                raise DomainError("strategy_quotes", "Stale, invalid, crossed or non-regular-hours quotes; no signal executed.")
            quotes[row["symbol"]] = {"ask": ask, "bid": bid}
            stamps.append(stamp)
        if abs(stamps[0] - stamps[1]) > 2:
            raise DomainError("strategy_quotes", "Two-stock quote timestamps differ by more than two seconds.")
        return quotes

    def _start_pair(self, record, spec, quotes, side):
        plans = {symbol: OrderSpec(symbol=symbol, side=side, quantity=spec.quantity,
                                   limit_price=quotes[symbol]["ask" if side == "BUY" else "bid"]).model_dump(mode="json")
                 for symbol in (spec.symbol_a, spec.symbol_b)}
        if side == "BUY" and sum(Decimal(plan["limit_price"]) * plan["quantity"] for plan in plans.values()) * (1 + self.config.limits.fee_buffer) > spec.max_pair_notional:
            raise DomainError("strategy_budget", "Actual paired limit prices exceed the authorized pair budget.")
        if side == "BUY":
            record["cycle"] += 1
            record["cycles_started"] += 1
        record.update(status="ENTERING" if side == "BUY" else "EXITING", order_ids=[],
                      intent={"side": side, "specs": plans, "deadline": time.time() + spec.order_timeout_seconds})
        self.db.save_strategy(record, "strategy_pair_intent")
        # Persist both intents before either dispatch. Stable keys prevent duplicate cycle legs.
        for symbol, plan in plans.items():
            order = self.db.create(plan, f"strategy:{record['id']}:{record['cycle']}:{side}:{symbol}",
                                   {"strategy_id": record["id"], "strategy_cycle": record["cycle"]})
            record["order_ids"].append(order["id"])
        self.db.save_strategy(record, "strategy_pair_orders_created")
        grant = self.service._strategy_grants[record["id"]]
        for order_id in record["order_ids"]:
            try:
                order = self.service.submit(order_id, _strategy_grant=grant)
                if order["state"] in {"UNKNOWN", "REJECTED", "EXPIRED"} or order["last_error"]:
                    raise DomainError("strategy_leg_failed", "A paired leg failed or is uncertain.")
            except Exception as error:
                record.update(status="CLEANUP", cleanup_deadline=time.time() + spec.order_timeout_seconds,
                              last_error=f"Pair dispatch stopped: {getattr(error, 'code', type(error).__name__)}. Remaining working legs will be cancelled, not retried.")
                self.db.save_strategy(record, "strategy_pair_failed")
                return

    def _progress(self, record, spec):
        for order_id in record["order_ids"]:
            order = self.db.get(order_id)
            if order["state"] not in TERMINAL | {"DRAFT"}:
                try:
                    self.service.reconcile(order_id)
                except Exception:
                    record["last_error"] = "Broker reconciliation unavailable; no new paired leg sent."
        orders = [self.db.get(order_id) for order_id in record["order_ids"]]
        if len(orders) != 2 or {order["spec"]["symbol"] for order in orders} != {spec.symbol_a, spec.symbol_b}:
            self.pause(record["id"], "Paired intent is incomplete; inspect linked orders manually.")
            return
        if record["status"] == "CLEANUP":
            grant = self.service._strategy_grants[record["id"]]
            for order in orders:
                if order["state"] == "DRAFT":
                    self.db.update(order["id"], {"state": "CANCELLED"}, "strategy_unsent_draft_cancelled")
                elif order["state"] in {"OPEN", "PARTIALLY_FILLED"} and not order["pending_action"]:
                    try:
                        self.service.cancel(order["id"], _strategy_grant=grant)
                    except Exception:
                        record["last_error"] = "Cleanup cancellation/reconciliation failed; inspect broker activity. No blind retry."
            if all(self.db.get(item)["state"] in TERMINAL for item in record["order_ids"]) or time.time() >= record["cleanup_deadline"]:
                self.pause(record["id"], record["last_error"] or "Paired lifecycle failed; manual recovery required.")
                return
        elif all(order["state"] == "FILLED" for order in orders):
            owned = self.db.strategy_owned(record["id"])
            if record["status"] == "ENTERING" and all(owned.get(symbol, Decimal(0)) == spec.quantity for symbol in (spec.symbol_a, spec.symbol_b)):
                record.update(status="HOLDING", last_error=None)
                self.db.save_strategy(record, "strategy_entry_filled")
                return
            if record["status"] == "EXITING" and not any(value > 0 for value in owned.values()):
                record["cycles_completed"] += 1
                record.update(status="COMPLETED" if record["cycles_started"] >= spec.max_cycles else "WAITING_ENTRY",
                              seen_above=False, next_entry_at=time.time() + spec.cooldown_seconds, intent=None, order_ids=[], last_error=None)
                if record["status"] == "COMPLETED":
                    self.service._strategy_grants.pop(record["id"], None)
                self.db.save_strategy(record, "strategy_cycle_completed")
                return
            self.pause(record["id"], "Filled order quantities do not match the strategy's allocation.")
            return
        elif any(order["state"] in {"REJECTED", "CANCELLED", "EXPIRED", "UNKNOWN"} for order in orders) or time.time() >= record["intent"]["deadline"]:
            record.update(status="CLEANUP", cleanup_deadline=time.time() + spec.order_timeout_seconds,
                          last_error="A leg failed, became uncertain or timed out. Cancel remaining working legs; review any fills.")
        self.db.save_strategy(record, "strategy_progress")

    def tick(self, strategy_id):
        with self.service.lock:
            record = self.db.strategy(strategy_id)
            spec = self._spec(record)
            if record["status"] not in RUNNING:
                return self.get(strategy_id)
            grant = self.service._strategy_grants.get(strategy_id)
            if not self.service.strategy_grant_valid(grant):
                return self.pause(strategy_id, "Strategy/global authorization expired or was halted. Working orders and holdings remain; inspect manually.")
            if record["status"] in {"ENTERING", "EXITING", "CLEANUP"}:
                self._progress(record, spec)
                return self.get(strategy_id)
            try:
                quotes = self._quotes(spec)
                asks = sum(row["ask"] for row in quotes.values())
                bids = sum(row["bid"] for row in quotes.values())
                record.update(last_ask_sum=str(asks), last_bid_sum=str(bids), last_error=None)
                if record["status"] == "WAITING_ENTRY":
                    if time.time() < record["next_entry_at"]:
                        record["seen_above"] = False
                    elif asks > spec.entry_sum:
                        record["seen_above"] = True
                    elif record["seen_above"] and time.time() >= record["next_entry_at"]:
                        self._start_pair(record, spec, quotes, "BUY")
                        return self.get(strategy_id)
                elif record["status"] == "HOLDING":
                    owned = self.db.strategy_owned(strategy_id)
                    if any(owned.get(symbol, Decimal(0)) != spec.quantity for symbol in (spec.symbol_a, spec.symbol_b)):
                        return self.pause(strategy_id, "Unbalanced strategy holdings; no automatic sale of unrelated shares.")
                    if bids >= spec.entry_sum + spec.profit_increment or bids <= spec.entry_sum - spec.downside_delta:
                        self._start_pair(record, spec, quotes, "SELL")
                        return self.get(strategy_id)
                self.db.save_strategy(record, None)
            except Exception as error:
                if record["status"] == "WAITING_ENTRY":
                    record["seen_above"] = False
                record["last_error"] = f"Evaluation skipped: {getattr(error, 'code', type(error).__name__)}. No signal trade sent."
                self.db.save_strategy(record, "strategy_signal_skipped")
            return self.get(strategy_id)
