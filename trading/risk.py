from decimal import Decimal, InvalidOperation
import time

from trading.models import TERMINAL


class RiskDataError(ValueError):
    pass


def number(value, name):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RiskDataError(f"Missing/invalid broker risk field: {name}.") from exc


def flatten_orders(groups):
    result = []
    for group in groups:
        if group.get("combo_type", "NORMAL") != "NORMAL" or not isinstance(group.get("orders"), list):
            raise RiskDataError("Unsupported broker open-order structure.")
        for order in group["orders"]:
            if order.get("status") not in TERMINAL | {"CANCELED"}:
                result.append(order)
    return result


class RiskEngine:
    def __init__(self, limits, database, broker):
        self.limits, self.database, self.broker = limits, database, broker

    def readiness(self):
        """Read-only validation before arming; never submits an order."""
        try:
            balance, positions = self.broker.balance(), self.broker.positions()
            orders = flatten_orders(self.broker.open_orders())
            assets = [item for item in balance.get("account_currency_assets", []) if item.get("currency") == "USD"]
            if len(assets) != 1 or not isinstance(positions, list):
                raise RiskDataError("Complete USD cash-account data is required.")
            if balance.get("total_asset_currency", "USD") != "USD":
                raise RiskDataError("Only USD account settlement is supported.")
            cash = min(number(assets[0].get("buying_power"), "buying_power"), number(assets[0].get("settled_cash"), "settled_cash"))
            number(balance.get("total_day_profit_loss"), "total_day_profit_loss")
            if cash < 0:
                raise RiskDataError("Negative settled cash/buying power is unsupported.")
            for position in positions:
                if position.get("instrument_type") != "EQUITY" or position.get("currency") != "USD" or position.get("market", "US") != "US" or number(position.get("quantity"), "quantity") < 0 or number(position.get("last_price"), "last_price") <= 0:
                    raise RiskDataError("Only valid, long USD equity positions are supported.")
            for order in orders:
                remaining = number(order.get("total_quantity"), "total_quantity") - number(order.get("filled_quantity"), "filled_quantity")
                if order.get("instrument_type") != "EQUITY" or order.get("side") not in {"BUY", "SELL"} or order.get("market", "US") != "US" or order.get("currency", "USD") != "USD" or remaining < 0:
                    raise RiskDataError("Unsupported/invalid broker open orders.")
                if order["side"] == "BUY" and number(order.get("limit_price"), "limit_price") <= 0:
                    raise RiskDataError("Unbounded open buy order prevents complete risk accounting.")
            return {"ready": True, "violations": [], "settled_buying_power": str(cash), "open_orders": len(orders), "positions": len(positions)}
        except (RiskDataError, TypeError, AttributeError, KeyError) as exc:
            return {"ready": False, "violations": [str(exc) if isinstance(exc, RiskDataError) else "Invalid broker readiness schema."]}

    def check(self, spec, ignore_id=None, ignore_halt=False, filled_quantity=Decimal(0), strategy_id=None):
        violations = []
        limits = self.limits
        remaining = Decimal(spec.quantity) - filled_quantity
        if remaining <= 0:
            violations.append("Quantity must exceed the already filled quantity.")
        if not ignore_halt and self.database.state("halt")["enabled"]:
            violations.append("Kill switch is enabled.")
        if spec.quantity > limits.max_order_quantity:
            violations.append("Maximum order quantity exceeded.")
        if Decimal(spec.quantity) * spec.limit_price > limits.max_order_notional:
            violations.append("Maximum order notional exceeded.")
        if getattr(self, "live_mode", False):
            tick = Decimal("0.01") if spec.limit_price >= 1 else Decimal("0.0001")
            if spec.limit_price % tick:
                violations.append("Live pilot accepts cent-priced limits at/above $1 and 0.0001 increments below $1; review the limit price.")
        if self.database.daily_submissions() >= limits.max_daily_orders:
            violations.append("Daily submission-attempt limit reached (UTC).")
        local = [item for item in self.database.orders(active=True) if item["client_order_id"] != ignore_id]
        if any(item["pending_action"] or item["state"] in {"UNKNOWN", "SUBMITTING", "ACKNOWLEDGED"} for item in local):
            violations.append("An unresolved broker mutation exists; reconcile it before adding risk.")
        metrics = {}
        try:
            balance = self.broker.balance()
            positions = self.broker.positions()
            remote = flatten_orders(self.broker.open_orders())
            if not isinstance(positions, list):
                raise RiskDataError("Unexpected position schema.")
            assets = [item for item in balance.get("account_currency_assets", []) if item.get("currency") == "USD"]
            if len(assets) != 1:
                raise RiskDataError("USD cash-account risk data is required.")
            if balance.get("total_asset_currency", "USD") != "USD":
                raise RiskDataError("Only USD account settlement is supported.")
            cash = min(number(assets[0].get("buying_power"), "buying_power"), number(assets[0].get("settled_cash"), "settled_cash"))
            daily_pnl = number(balance.get("total_day_profit_loss"), "total_day_profit_loss")
            if spec.side == "BUY" and daily_pnl <= -limits.max_daily_loss:
                violations.append("Daily loss limit reached; new buys are blocked.")
            quotes = self.broker.quotes([spec.symbol])
            quote = next((item for item in quotes if item.get("symbol") == spec.symbol), None) if isinstance(quotes, list) else None
            if not quote:
                raise RiskDataError("Requested quote is missing.")
            reference = number(quote.get("ask" if spec.side == "BUY" else "bid", quote.get("price")), "quote_price")
            quote_time = number(quote.get("quote_time", quote.get("last_trade_time")), "quote_time") / 1000
            age = Decimal(str(time.time())) - quote_time
            if reference <= 0 or age > limits.max_quote_age_seconds or age < -60:
                violations.append("Quote is stale, invalid, or clock synchronization is incorrect.")
            if quote.get("trade_status") != "RTH":
                violations.append("CORE-only trading requires a regular-hours market quote.")
            if reference > 0 and abs(spec.limit_price - reference) / reference > limits.max_price_deviation:
                violations.append("Limit price deviates too far from the current quote.")

            portfolio_value, symbol_value, held = Decimal(0), Decimal(0), Decimal(0)
            for position in positions:
                if position.get("instrument_type") != "EQUITY" or position.get("currency") != "USD" or position.get("market", "US") != "US":
                    raise RiskDataError("Non-USD/equity positions require unsupported risk accounting.")
                quantity = number(position.get("quantity"), "position.quantity")
                price = number(position.get("last_price"), "position.last_price")
                if quantity < 0 or price <= 0:
                    raise RiskDataError("Short/invalid positions are not supported.")
                portfolio_value += quantity * price
                if position.get("symbol") == spec.symbol:
                    symbol_value += quantity * price
                    available = quantity
                    legs = position.get("legs", [])
                    if len(legs) == 1 and legs[0].get("quantity") is not None:
                        available = min(available, number(legs[0]["quantity"], "available_quantity"))
                    held += available

            remote = [item for item in remote if not ignore_id or item.get("client_order_id") != ignore_id]
            visible = {item.get("client_order_id") for item in remote}
            unreflected_buy = Decimal(0)
            # Include unreflected local acknowledged orders conservatively, deduplicated by ID.
            for item in local:
                if item["client_order_id"] not in visible:
                    if item["spec"]["side"] == "BUY":
                        unreflected_buy += (
                            Decimal(item["spec"]["quantity"]) - Decimal(item["filled_quantity"])
                        ) * Decimal(item["spec"]["limit_price"])
                    remote.append({
                        **item["spec"], "instrument_type": "EQUITY",
                        "total_quantity": item["spec"]["quantity"], "filled_quantity": item["filled_quantity"],
                        "client_order_id": item["client_order_id"],
                    })
            if len(remote) >= limits.max_open_orders:
                violations.append("Maximum open-order count reached.")
            pending_buy, pending_symbol_buy, pending_sell = Decimal(0), Decimal(0), Decimal(0)
            for order in remote:
                if order.get("instrument_type") != "EQUITY" or order.get("side") not in {"BUY", "SELL"} or order.get("market", "US") != "US" or order.get("currency", "USD") != "USD":
                    raise RiskDataError("Unsupported open orders prevent complete risk accounting.")
                qty = number(order.get("total_quantity"), "order.total_quantity") - number(order.get("filled_quantity"), "order.filled_quantity")
                if qty < 0:
                    raise RiskDataError("Invalid broker remaining quantity.")
                if order["side"] == "BUY":
                    price = number(order.get("limit_price"), "order.limit_price")
                    if price <= 0:
                        raise RiskDataError("Open buy order has no bounded limit price.")
                    pending_buy += qty * price
                    if order.get("symbol") == spec.symbol:
                        pending_symbol_buy += qty * price
                elif order.get("symbol") == spec.symbol:
                    pending_sell += qty
            cost = max(remaining, Decimal(0)) * spec.limit_price * (1 + limits.fee_buffer)
            reserved_strategy_shares = self.database.reserved_shares(exclude_strategy=strategy_id).get(spec.symbol, Decimal(0))
            if spec.side == "BUY":
                if cost > cash - unreflected_buy:
                    violations.append("Insufficient settled cash/buying power including fee buffer.")
                added_exposure = max(remaining, Decimal(0)) * max(spec.limit_price, reference)
                if symbol_value + pending_symbol_buy + added_exposure > limits.max_symbol_exposure:
                    violations.append("Maximum per-symbol gross exposure exceeded.")
                if portfolio_value + pending_buy + added_exposure > limits.max_portfolio_exposure:
                    violations.append("Maximum portfolio gross exposure exceeded.")
            elif remaining > held - pending_sell - reserved_strategy_shares:
                violations.append("Insufficient available shares; short selling is disabled.")
            metrics = {
                "settled_buying_power": str(cash), "estimated_cost_with_buffer": str(cost),
                "settlement_currency": "USD",
                "cash_available_after_local_reservations": str(max(cash - unreflected_buy, Decimal(0))),
                "current_portfolio_value": str(portfolio_value), "pending_buy_notional": str(pending_buy),
                "available_symbol_shares": str(max(held - pending_sell - reserved_strategy_shares, Decimal(0))),
                "strategy_reserved_shares": str(reserved_strategy_shares),
                "quote_price": str(reference), "quote_age_seconds": str(max(age, Decimal(0))),
                "day_profit_loss": str(daily_pnl), "open_order_count": len(remote),
            }
        except (RiskDataError, AttributeError, TypeError, KeyError) as exc:
            violations.append(str(exc) if isinstance(exc, RiskDataError) else "Unexpected broker risk-data schema.")
        return {"allowed": not violations, "violations": violations, "metrics": metrics}
