"""Manual browser-test server. Both profiles are fake; never reads Webull credentials."""
import argparse
from dataclasses import replace
from pathlib import Path
import tempfile
import sys

import uvicorn

def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fake_broker import FakeBroker
    from trading.app import create_app
    from trading.config import AppConfig, RiskLimits
    from webull_cli import Settings

    class FixtureBroker(FakeBroker):
        def connect(self):
            result = super().connect()
            result["account_label"] = "SIMULATED TEST BROKER — no real money"
            return result

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8042)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sandbox = AppConfig(broker=Settings(app_key="fixture-sandbox-key", app_secret="fixture-sandbox-secret", account_id="fixture-sandbox"), api_key="fixture-ui-key-" + "x" * 48,
                            database=root / "sandbox.sqlite3", reconcile_interval=0)
        live = replace(sandbox, broker=Settings(environment="production", app_key="fixture-live-key", app_secret="fixture-live-secret", account_id="fixture-live", enable_live_trading=True),
                       database=root / "live.sqlite3", limits=RiskLimits.live_defaults())
        config = replace(sandbox, profiles={"sandbox": sandbox, "production": live})
        brokers = {"sandbox": FixtureBroker("fixture-sandbox"), "production": FixtureBroker("fixture-live")}
        app = create_app(config, brokers)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))

        @app.get("/fixture/stats", include_in_schema=False)
        def stats():
            return {"simulated": True, "real_broker_mutations": 0, "profiles": {
                name: {"place": broker.place_calls, "replace": broker.replace_calls, "cancel": broker.cancel_calls, "quotes": broker.quote_calls}
                for name, broker in brokers.items()
            }}

        @app.post("/fixture/shutdown", include_in_schema=False)
        def shutdown():
            server.should_exit = True
            return {"stopping": True}

        print(f"SIMULATED UI TEST SERVER: http://127.0.0.1:{args.port} — no actual broker connection.", flush=True)
        server.run()


if __name__ == "__main__":
    main()
