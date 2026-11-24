"""Local provisioning and single-worker server launcher."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import secrets
import sqlite3

from dotenv import dotenv_values, set_key

from trading.config import AppConfig, profile_settings
from webull_cli import ROOT


def initialize(environment="sandbox"):
    path = ROOT / ".env"
    values = dotenv_values(path, interpolate=False)
    settings = profile_settings(values, environment)
    settings.require_credentials()
    # User-authorized account selection. No order is placed by provisioning.
    from webull.trade.trade_client import TradeClient
    from webull_cli import api_client, response_json

    trade = TradeClient(api_client(settings))
    accounts = response_json(trade.account_v2.get_account_list(), settings)
    cash = [item for item in accounts if item.get("account_class") == "INDIVIDUAL_CASH"]
    selected = next((item for item in cash if item.get("account_id") == settings.account_id), None) if settings.account_id else None
    if selected is None and len(cash) == 1 and not settings.account_id:
        selected = cash[0]
    if selected is None:
        raise ValueError(f"Configured {environment} Individual Cash account is missing or ambiguous; review its ID locally.")
    backend_key = values.get("TRADING_API_KEY")
    if not backend_key:
        set_key(path, "TRADING_API_KEY", secrets.token_urlsafe(48))
    elif len(backend_key) < 32:
        raise ValueError("Existing TRADING_API_KEY is too short; replace it locally.")
    account_field = "WEBULL_LIVE_ACCOUNT_ID" if environment == "production" else (
        "WEBULL_SANDBOX_ACCOUNT_ID" if values.get("WEBULL_SANDBOX_APP_KEY") or values.get("WEBULL_SANDBOX_APP_SECRET") else "WEBULL_ACCOUNT_ID"
    )
    set_key(path, account_field, selected["account_id"])
    print(f"Individual Cash {environment} account configured. Webull app key/secret unchanged.")
    print("Backend API key stored locally in .env (not printed). No orders submitted.")
    print("Start: .\\.venv\\Scripts\\python.exe manage.py serve")


def upgrade_environment_file():
    path = ROOT / ".env"
    if not path.exists():
        path.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    existing = dotenv_values(path, interpolate=False)
    template = dotenv_values(ROOT / ".env.example", interpolate=False)
    added = 0
    for name, value in template.items():
        if name not in existing:
            set_key(path, name, value or "")
            added += 1
    print(f"Added {added} missing profile/risk fields. Existing secrets and flags preserved. No broker requests.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--environment", choices=["sandbox", "production"], default="sandbox")
    commands.add_parser("upgrade-env")
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", type=Path)
    backup.add_argument("--environment", choices=["sandbox", "production"])
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.environment)
    elif args.command == "upgrade-env":
        upgrade_environment_file()
    elif args.command == "backup":
        config = AppConfig.from_env()
        if args.environment and args.environment != config.broker.environment:
            if args.environment not in config.profiles:
                raise ValueError("The backup profile is not configured.")
            config = config.profiles[args.environment]
        if not config.database.exists():
            raise ValueError("Database does not exist yet; start the server first.")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = args.destination or ROOT / "data" / "backups" / f"trading_{timestamp}.sqlite3"
        if destination.resolve() == config.database.resolve() or destination.exists():
            raise ValueError("Backup destination must be a new file, not the active database.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(config.database.resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        print(f"Consistent SQLite backup saved to {destination}")
    else:
        import uvicorn
        AppConfig.from_env()  # Fail early on unsafe/incomplete configuration.
        uvicorn.run("trading.app:create_app", factory=True, host="127.0.0.1", port=args.port,
                    workers=1, limit_concurrency=20, timeout_graceful_shutdown=90)


if __name__ == "__main__":
    main()
