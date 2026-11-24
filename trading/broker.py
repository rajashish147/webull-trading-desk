from collections import defaultdict, deque
import json
import threading
import time

from webull.data.common.category import Category
from webull.data.data_client import DataClient
from webull.trade.trade_client import TradeClient

from trading.models import BrokerError, DomainError
from webull_cli import api_client, redact


class WebullGateway:
    """Lazy official SDK adapter; serial calls, TLS, no mutation retries."""

    def __init__(self, settings):
        self.settings = settings
        self.connected = False
        self.account_summary = None
        self._client = self._trade = self._data = None
        self.lock = threading.RLock()
        self.calls = defaultdict(deque)

    def _rate_limit(self, name):
        # Conservative sandbox quotas; production's 2/2s query limits also apply.
        window, maximum = (60, 30)
        if self.settings.environment == "production":
            if name in {"balance", "positions", "open_orders", "history", "detail"}:
                window, maximum = (2, 2)
            elif name == "accounts":
                window, maximum = (30, 10)
        timestamp = time.monotonic()
        bucket = self.calls[name]
        while bucket and bucket[0] <= timestamp - window:
            bucket.popleft()
        if len(bucket) >= maximum:
            raise DomainError("broker_rate_limit", f"Local Webull {name} quota reached; retry later.", 429)
        bucket.append(timestamp)

    def _request(self, name, function):
        with self.lock:
            self._rate_limit(name)
            try:
                response = function()
            except Exception as exc:
                # Do not expose SDK exception text or credentials. A timeout is ambiguous.
                raise BrokerError(f"Webull {name} transport failed ({type(exc).__name__}); reconcile mutations before retrying.") from exc
            if not 200 <= response.status_code < 300:
                definitive = 400 <= response.status_code < 500 and response.status_code != 408
                status = 429 if response.status_code == 429 else 502
                raise BrokerError(f"Webull {name} returned HTTP {response.status_code}.", definitive, status)
            try:
                data = response.json()
                if isinstance(data, dict) and (
                    data.get("error_code") or data.get("code", 0) not in (0, "0", 200, "200", "SUCCESS")
                ):
                    raise BrokerError(f"Webull {name} returned a business error.", definitive=True)
                return json.loads(redact(json.dumps(data), self.settings))
            except BrokerError:
                raise
            except (ValueError, TypeError) as exc:
                raise BrokerError(f"Webull {name} returned invalid JSON.") from exc

    def connect(self):
        with self.lock:
            self.connected = False
            if self._trade is None:
                self._client = api_client(self.settings)
                try:
                    self._trade = TradeClient(self._client)
                except Exception as exc:
                    raise BrokerError("Webull authentication initialization failed; check credentials and App 2FA.") from exc
            accounts = self._request("accounts", self._trade.account_v2.get_account_list)
            if not isinstance(accounts, list):
                raise BrokerError("Unexpected Webull account-list schema.")
            selected = next((item for item in accounts if item.get("account_id") == self.settings.account_id), None)
            if not selected or selected.get("account_class") != "INDIVIDUAL_CASH":
                self.connected = False
                raise DomainError("account_mismatch", "This backend requires the configured Individual Cash account.")
            self.account_summary = {key: selected.get(key) for key in ("account_id", "account_class", "account_label")}
            self.connected = True
            return self.account_summary

    def _ready(self):
        if not self.connected:
            raise DomainError("broker_not_connected", "Call POST /v1/broker/connect first.", 503)

    def _writable(self):
        self._ready()
        if self.settings.environment != "sandbox":
            raise DomainError("production_read_only", "Production mutations are disabled in this version.", 403)

    def accounts(self):
        self._ready()
        return self._request("accounts", self._trade.account_v2.get_account_list)

    def balance(self):
        self._ready()
        return self._request("balance", lambda: self._trade.account_v2.get_account_balance(self.settings.account_id))

    def positions(self):
        self._ready()
        return self._request("positions", lambda: self._trade.account_v2.get_account_position(self.settings.account_id))

    def open_orders(self):
        self._ready()
        result, seen, key = [], set(), None
        for _ in range(20):
            page = self._request("open_orders", lambda key=key: self._trade.order_v3.list_order_open(self.settings.account_id, pagination_key=key))
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise BrokerError("Unexpected Webull open-order schema; refusing partial risk data.")
            result.extend(page["data"])
            key = page.get("pagination_key")
            if not key:
                return result
            if key in seen:
                break
            seen.add(key)
        raise BrokerError("Open-order pagination did not complete; risk snapshot unavailable.")

    def history(self, pagination_key=None):
        self._ready()
        return self._request("history", lambda: self._trade.order_v3.list_order_history(self.settings.account_id, pagination_key=pagination_key))

    def detail(self, client_order_id):
        self._ready()
        return self._request("detail", lambda: self._trade.order_v3.get_order_detail(self.settings.account_id, client_order_id))

    def quotes(self, symbols):
        self._ready()
        with self.lock:
            if self._data is None:
                try:
                    self._data = DataClient(self._client)
                except Exception as exc:
                    raise BrokerError("Webull market-data initialization failed.") from exc
            return self._request("quotes", lambda: self._data.market_data.get_snapshot(symbols, Category.US_STOCK.name))

    def bars(self, symbol, timespan, count):
        self.quotes_ready()
        return self._request("bars", lambda: self._data.market_data.get_batch_history_bar(
            [symbol], Category.US_STOCK.name, timespan, count=str(count),
        ))

    def quotes_ready(self):
        self._ready()
        with self.lock:
            if self._data is None:
                try:
                    self._data = DataClient(self._client)
                except Exception as exc:
                    raise BrokerError("Webull market-data initialization failed.") from exc

    def preview(self, payload):
        self._ready()
        return self._request("preview", lambda: self._trade.order_v3.preview_order(self.settings.account_id, [payload]))

    def place(self, payload):
        self._writable()
        return self._request("place", lambda: self._trade.order_v3.place_order(self.settings.account_id, [payload]))

    def replace(self, client_order_id, quantity, limit_price):
        self._writable()
        return self._request("replace", lambda: self._trade.order_v3.replace_order(self.settings.account_id, [{
            "client_order_id": client_order_id, "quantity": str(quantity), "limit_price": str(limit_price),
        }]))

    def cancel(self, client_order_id):
        self._writable()
        return self._request("cancel", lambda: self._trade.order_v3.cancel_order(self.settings.account_id, client_order_id))
