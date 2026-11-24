"""Local provisioning and single-worker server launcher."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import secrets
import sqlite3

from dotenv import dotenv_values, set_key

from trading.config import AppConfig
from webull_cli import ROOT, Settings


def initialize():
    settings = Settings.from_file()
    if settings.environment != "sandbox" or settings.enable_live_trading:
        raise ValueError("Initialization requires sandbox and WEBULL_ENABLE_LIVE_TRADING=false.")
    # User-authorized account selection. No order is placed by provisioning.
    from webull.trade.trade_client import TradeClient
    from webull_cli import api_client, response_json

    trade = TradeClient(api_client(settings))
    accounts = response_json(trade.account_v2.get_account_list(), settings)
    cash = [item for item in accounts if item.get("account_class") == "INDIVIDUAL_CASH"]
    if len(cash) != 1:
        raise ValueError("Expected one Individual Cash sandbox account; configure manually if ambiguous.")
    path = ROOT / ".env"
    values = dotenv_values(path, interpolate=False)
    backend_key = values.get("TRADING_API_KEY")
    if not backend_key:
        set_key(path, "TRADING_API_KEY", secrets.token_urlsafe(48))
    elif len(backend_key) < 32:
        raise ValueError("Existing TRADING_API_KEY is too short; replace it locally.")
    set_key(path, "WEBULL_ACCOUNT_ID", cash[0]["account_id"])
    print("Individual Cash sandbox account configured. Webull app key/secret unchanged.")
    print("Backend API key stored locally in .env (not printed). No orders submitted.")
    print("Start: .\\.venv\\Scripts\\python.exe manage.py serve")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", type=Path)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "init":
        initialize()
    elif args.command == "backup":
        config = AppConfig.from_env()
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
