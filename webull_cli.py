"""Official US Webull OpenAPI starter; offline order plans by default."""

import argparse
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import re
import sys
import uuid

from dotenv import dotenv_values, load_dotenv
from webull.core.client import ApiClient
from webull.data.common.category import Category
from webull.data.common.timespan import Timespan
from webull.data.data_client import DataClient
from webull.trade.trade_client import TradeClient

ROOT = Path(__file__).resolve().parent
ENDPOINTS = {
    "sandbox": "api.sandbox.webull.com",
    "production": "api.webull.com",
}


@dataclass(frozen=True)
class Settings:
    environment: str = "sandbox"
    app_key: str = field(default="", repr=False)
    app_secret: str = field(default="", repr=False)
    account_id: str = field(default="", repr=False)
    enable_live_trading: bool = False

    def __post_init__(self):
        if self.environment not in ENDPOINTS:
            raise ValueError("WEBULL_ENV must be sandbox or production.")

    @classmethod
    def from_env(cls):
        # Existing process environment variables take precedence over .env.
        load_dotenv(ROOT / ".env", override=False)
        return cls.from_values(cls.selected_profile_values(os.environ))

    @classmethod
    def from_file(cls, path=None):
        """Read a local .env without inheriting stale process environment values."""
        return cls.from_values(cls.selected_profile_values(dotenv_values(path or ROOT / ".env", interpolate=False)))

    @staticmethod
    def selected_profile_values(values):
        mode = (values.get("WEBULL_ENV") or "sandbox").strip().lower()
        prefix = "WEBULL_LIVE_" if mode == "production" else "WEBULL_SANDBOX_"
        if mode == "production" or any(values.get(prefix + name) for name in ("APP_KEY", "APP_SECRET", "ACCOUNT_ID")):
            return {**values, **{"WEBULL_" + name: values.get(prefix + name) or "" for name in ("APP_KEY", "APP_SECRET", "ACCOUNT_ID")}}
        return values

    @classmethod
    def from_values(cls, values):
        live = (values.get("WEBULL_ENABLE_LIVE_TRADING") or "false").strip().lower()
        if live not in {"true", "false"}:
            raise ValueError("WEBULL_ENABLE_LIVE_TRADING must be true or false.")
        return cls(
            environment=(values.get("WEBULL_ENV") or "sandbox").strip().lower(),
            app_key=(values.get("WEBULL_APP_KEY") or "").strip(),
            app_secret=(values.get("WEBULL_APP_SECRET") or "").strip(),
            account_id=(values.get("WEBULL_ACCOUNT_ID") or "").strip(),
            enable_live_trading=live == "true",
        )

    @property
    def endpoint(self):
        return ENDPOINTS[self.environment]

    def require_credentials(self):
        for name, value in (
            ("WEBULL_APP_KEY", self.app_key),
            ("WEBULL_APP_SECRET", self.app_secret),
        ):
            if not value or value.startswith("<") or value.lower().startswith("your_"):
                raise ValueError(f"Set {name} in {ROOT / '.env'} first.")


def positive_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Must be a positive whole number.") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be a positive whole number.")
    return number


def bar_count(value):
    number = positive_int(value)
    if number > 1200:
        raise argparse.ArgumentTypeError("Bar count must be between 1 and 1200.")
    return number


def positive_price(value):
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("Must be a positive finite price.") from exc
    if not number.is_finite() or number <= 0:
        raise argparse.ArgumentTypeError("Must be a positive finite price.")
    return format(number, "f")


def symbol(value):
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", value):
        raise argparse.ArgumentTypeError("Enter a US stock/ETF ticker such as AAPL.")
    return value


def client_order_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
        raise argparse.ArgumentTypeError("Use 1-32 letters, numbers, hyphens, or underscores.")
    return value


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Check local configuration without API calls.")
    commands.add_parser("accounts", help="List accounts; first connection check.")
    for name in ("balance", "positions", "orders"):
        command = commands.add_parser(name, help=f"Read account {name}.")
        command.add_argument("--account-id", help="Override WEBULL_ACCOUNT_ID.")
        if name == "orders":
            command.add_argument("--pagination-key", help="Fetch the next page of open orders.")
    quotes = commands.add_parser("quotes", help="Fetch stock/ETF market snapshots.")
    quotes.add_argument("symbols", nargs="+", type=symbol)
    bars = commands.add_parser("bars", help="Fetch historical stock/ETF candlesticks.")
    bars.add_argument("symbol", type=symbol)
    bars.add_argument("--timespan", choices=[item.name for item in Timespan], default="M1")
    bars.add_argument("--count", type=bar_count, default=100)
    order = commands.add_parser("order", help="Plan a stock/ETF limit order; default is OFFLINE.")
    order.add_argument("symbol", type=symbol)
    order.add_argument("--account-id", help="Override WEBULL_ACCOUNT_ID.")
    order.add_argument("--side", choices=["BUY", "SELL"], required=True)
    order.add_argument("--quantity", type=positive_int, required=True)
    order.add_argument("--limit-price", type=positive_price, required=True)
    order.add_argument("--client-order-id", type=client_order_id)
    order.add_argument("--submit", action="store_true", help="Actually send the order to Webull.")
    order.add_argument("--confirm-live-order", action="store_true", help="Required for production submission.")
    return result


def api_client(settings):
    settings.require_credentials()
    client = ApiClient(
        settings.app_key, settings.app_secret, "us",
        connect_timeout=5, timeout=15, verify=True, auto_retry=False,
    )
    # Override the SDK's production default BEFORE any SDK initialization.
    client.add_endpoint("us", settings.endpoint)
    key_hash = hashlib.sha256(settings.app_key.encode()).hexdigest()[:16]
    client.set_token_dir(str(ROOT / ".webull_tokens" / settings.environment / key_hash))
    # Avoid automatic SDK file logs; keep JSON output on stdout.
    client.set_stream_logger(log_level=logging.WARNING, stream=sys.stderr)
    print(
        f"Connecting to {settings.environment} ({settings.endpoint}). "
        "If 2FA is enabled, approve the request in the Webull App (up to 5 minutes).",
        file=sys.stderr,
    )
    return client


def account_for(args, settings):
    account_id = (getattr(args, "account_id", None) or settings.account_id).strip()
    if not account_id:
        raise ValueError("Run accounts, then set WEBULL_ACCOUNT_ID or pass --account-id.")
    return account_id


def order_payload(args):
    return {
        "combo_type": "NORMAL",
        "client_order_id": args.client_order_id or uuid.uuid4().hex,
        "symbol": args.symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "LIMIT",
        "limit_price": args.limit_price,
        "quantity": str(args.quantity),
        "support_trading_session": "CORE",
        "side": args.side,
        "time_in_force": "DAY",
        "entrust_type": "QTY",
    }


def check_submission(settings, args):
    if settings.environment == "production":
        raise ValueError(
            "Live order blocked in the legacy CLI. Use the dashboard/API's live arm "
            "and exact per-order confirmation workflow."
        )
    if settings.environment == "sandbox" and args.confirm_live_order:
        raise ValueError("--confirm-live-order is for production, not sandbox.")


def redact(text, settings):
    for secret in sorted((settings.app_key, settings.app_secret), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def response_json(response, settings):
    if not 200 <= response.status_code < 300:
        hint = {
            401: "Check credentials, environment, and Webull App verification.",
            403: "Check API permissions and OpenAPI market-data entitlements/subscription.",
            429: "Rate limited; wait before trying again.",
        }.get(response.status_code, "Check the Webull API response and service status.")
        detail = redact(response.text, settings)[:2000]
        raise RuntimeError(f"Webull HTTP {response.status_code}. {hint}\n{detail}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("Webull returned a non-JSON response.") from exc


def execute_command(args, settings):
    if args.command == "status":
        return {
            "sdk_version": version("webull-openapi-python-sdk"),
            "environment": settings.environment,
            "endpoint": settings.endpoint,
            "app_key_configured": bool(settings.app_key),
            "app_secret_configured": bool(settings.app_secret),
            "account_id_configured": bool(settings.account_id),
            "live_trading_enabled": settings.enable_live_trading,
            "api_connection_tested": False,
        }

    account_id = None
    if args.command in {"balance", "positions", "orders", "order"}:
        account_id = account_for(args, settings)
    if args.command == "quotes" and len(args.symbols) > 100:
        raise ValueError("Request at most 100 stock/ETF symbols at once.")

    payload = None
    if args.command == "order":
        payload = order_payload(args)
        if not args.submit:
            return {
                "dry_run": True,
                "environment": settings.environment,
                "endpoint": settings.endpoint,
                "account_id": account_id,
                "new_orders": [payload],
                "note": "OFFLINE plan only; not submitted or validated by Webull.",
            }
        check_submission(settings, args)

    client = api_client(settings)
    if args.command in {"quotes", "bars"}:
        data = DataClient(client).market_data
        if args.command == "quotes":
            response = data.get_snapshot(args.symbols, Category.US_STOCK.name)
        else:
            response = data.get_batch_history_bar(
                [args.symbol], Category.US_STOCK.name, args.timespan, count=str(args.count),
            )
    else:
        trade = TradeClient(client)
        if args.command == "accounts":
            response = trade.account_v2.get_account_list()
        elif args.command == "balance":
            response = trade.account_v2.get_account_balance(account_id)
        elif args.command == "positions":
            response = trade.account_v2.get_account_position(account_id)
        elif args.command == "orders":
            response = trade.order_v3.list_order_open(account_id, pagination_key=args.pagination_key)
        else:
            print(f"SUBMITTING {settings.environment} order {payload['client_order_id']}", file=sys.stderr)
            print("If the request times out, check orders before resubmitting; it may have succeeded.", file=sys.stderr)
            response = trade.order_v3.place_order(account_id, [payload])
    return response_json(response, settings)


def main(argv=None):
    args = parser().parse_args(argv)
    settings = None
    try:
        settings = Settings.from_env()
        output = execute_command(args, settings)
        print(redact(json.dumps(output, indent=2), settings))
        return 0
    except KeyboardInterrupt:
        print("Interrupted. If submitting an order, check orders before retrying.", file=sys.stderr)
        return 130
    except Exception as exc:
        message = str(exc)
        if settings is not None:
            message = redact(message, settings)
        print(f"Error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
