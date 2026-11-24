from copy import deepcopy
import time

from trading.models import BrokerError, DomainError


class FakeBroker:
    """No-network exchange fixture. Never imports/initializes the live SDK."""

    def __init__(self, account_id="fake-cash-account"):
        self.account_id = account_id
        self.last_permit = None
        self.connected = False
        self.account_summary = None
        self.order_data = {}
        self.place_calls = self.replace_calls = self.cancel_calls = 0
        self.place_error = self.replace_error = self.cancel_error = None
        self.hold_replace = self.hold_cancel = False
        self.cash = "100000"
        self.daily_pnl = "0"
        self.position_data = []
        self.external_orders = []
        self.quote_price = "100"
        self.quote_age = 0
        self.trade_status = "RTH"

    def connect(self):
        self.connected = True
        self.account_summary = {"account_id": self.account_id, "account_class": "INDIVIDUAL_CASH", "account_label": "Individual Cash fixture"}
        return self.account_summary

    def _ready(self):
        if not self.connected:
            raise DomainError("broker_not_connected", "Connect the fixture first.", 503)

    def accounts(self):
        self._ready()
        return [self.account_summary]

    def balance(self):
        self._ready()
        return {"total_day_profit_loss": self.daily_pnl, "total_cash_balance": self.cash,
                "account_currency_assets": [{"currency": "USD", "buying_power": self.cash, "settled_cash": self.cash}]}

    def positions(self):
        self._ready()
        return deepcopy(self.position_data)

    def open_orders(self):
        self._ready()
        active = [order for order in self.order_data.values() if order["status"] not in {"FILLED", "CANCELLED", "REJECTED"}]
        return deepcopy([{"combo_type": "NORMAL", "orders": [order]} for order in active] + self.external_orders)

    def history(self, pagination_key=None):
        return {"data": list(self.order_data.values())}

    def quotes(self, symbols):
        self._ready()
        return [{"symbol": symbol, "price": self.quote_price, "ask": self.quote_price, "bid": self.quote_price,
                 "quote_time": int((time.time() - self.quote_age) * 1000), "trade_status": self.trade_status} for symbol in symbols]

    def bars(self, symbol, timespan, count):
        self._ready()
        return {"result": [{"symbol": symbol, "result": [{"close": "100"}] * count}]}

    def preview(self, payload):
        self._ready()
        return {"estimated_cost": str(float(payload["limit_price"]) * int(payload["quantity"])), "estimated_transaction_fee": "0", "currency": "USD"}

    def place(self, payload, permit=None):
        self.last_permit = permit
        self._ready()
        self.place_calls += 1
        if self.place_error and self.place_error.definitive:
            raise self.place_error
        key = payload["client_order_id"]
        self.order_data[key] = {**payload, "total_quantity": payload["quantity"], "filled_quantity": "0",
                                "status": "SUBMITTED", "order_id": "broker-" + key}
        if self.place_error:
            raise self.place_error
        return {"client_order_id": key, "order_id": "broker-" + key}

    def detail(self, key):
        self._ready()
        if key not in self.order_data:
            raise BrokerError("Order not found.", definitive=True)
        return {"client_order_id": key, "orders": [deepcopy(self.order_data[key])]}

    def replace(self, key, quantity, limit_price, permit=None):
        self.last_permit = permit
        self.replace_calls += 1
        if not self.hold_replace:
            self.order_data[key].update(total_quantity=str(quantity), limit_price=str(limit_price))
        if self.replace_error:
            raise self.replace_error
        return {"client_order_id": key, "order_id": "broker-" + key}

    def cancel(self, key, permit=None):
        self.last_permit = permit
        self.cancel_calls += 1
        if not self.hold_cancel:
            self.order_data[key]["status"] = "CANCELLED"
        if self.cancel_error:
            raise self.cancel_error
        return {"client_order_id": key, "order_id": "broker-" + key}
