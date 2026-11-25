from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from trading.app import create_app
from trading.config import AppConfig, RiskLimits
from trading.models import BrokerError
from webull_cli import Settings


class SumStrategyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = AppConfig(broker=Settings(account_id="fake-cash-account"), api_key="x" * 48,
                                database=Path(self.temp.name) / "strategy.sqlite3", reconcile_interval=0)
        self.broker = FakeBroker()
        self.app = create_app(self.config, self.broker)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.client.headers.update({"X-API-Key": self.config.api_key, "X-Trading-Environment": "sandbox"})
        self.client.post("/v1/broker/connect")
        self.client.put("/v1/risk/kill-switch", json={"enabled": False, "reason": "Strategy fixture session"})
        self.spec = {"name": "A+B test", "symbol_a": "AAPL", "symbol_b": "MSFT", "quantity": 1,
                     "entry_sum": "200", "profit_increment": "4", "downside_delta": "10",
                     "max_pair_notional": "500", "max_cycles": 2, "cooldown_seconds": 30,
                     "order_timeout_seconds": 10, "duration_seconds": 300}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    @property
    def engine(self):
        return self.app.state.profile_manager.active.strategies

    def create(self, **changes):
        response = self.client.post("/v1/strategies", json={**self.spec, **changes})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def arm(self, strategy):
        response = self.client.post(f"/v1/strategies/{strategy['id']}/arm", json={"confirmation": "ARM SANDBOX STRATEGY"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def tick(self, strategy):
        response = self.client.post(f"/v1/strategies/{strategy['id']}/tick")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def enter(self, strategy=None):
        strategy = strategy or self.create()
        self.arm(strategy)
        self.broker.quote_price = "101"
        self.tick(strategy)
        self.broker.quote_price = "100"
        return self.tick(strategy)

    def fill(self, order, quantity=None):
        data = self.broker.order_data[order["client_order_id"]]
        old = Decimal(data["filled_quantity"])
        filled = Decimal(str(quantity if quantity is not None else data["total_quantity"]))
        data.update(filled_quantity=str(filled), status="FILLED" if filled == Decimal(data["total_quantity"]) else "PARTIALLY_FILLED", avg_filled_price=data["limit_price"])
        positions = {item["symbol"]: item for item in self.broker.position_data}
        position = positions.setdefault(data["symbol"], {"symbol": data["symbol"], "quantity": "0", "last_price": data["limit_price"], "currency": "USD", "instrument_type": "EQUITY"})
        position["quantity"] = str(Decimal(position["quantity"]) + (filled - old) * (1 if data["side"] == "BUY" else -1))
        self.broker.position_data = [item for item in positions.values() if Decimal(item["quantity"]) > 0]

    def hold(self, strategy=None):
        strategy = self.enter(strategy)
        for order in strategy["orders"]:
            self.fill(order)
        return self.tick(strategy)

    def expire_order_deadline(self, strategy):
        record = self.app.state.database.strategy(strategy["id"])
        record["intent"]["deadline"] = 0
        self.app.state.database.save_strategy(record, None)

    def test_creation_never_arms_or_orders(self):
        strategy = self.create()
        self.assertEqual(strategy["status"], "DRAFT")
        self.assertEqual(self.broker.place_calls, 0)

    def test_requires_distinct_symbols_and_positive_targets(self):
        for changes in ({"symbol_b": "AAPL"}, {"profit_increment": "0"}, {"downside_delta": "200"}, {"quantity": 1.5}):
            self.assertEqual(self.client.post("/v1/strategies", json={**self.spec, **changes}).status_code, 422)

    def test_arm_requires_typed_scope_and_enabled_environment(self):
        strategy = self.create()
        self.assertEqual(self.client.post(f"/v1/strategies/{strategy['id']}/arm", json={"confirmation": "ARM LIVE STRATEGY"}).status_code, 409)
        self.client.put("/v1/risk/kill-switch", json={"enabled": True, "reason": "Halted fixture"})
        self.assertEqual(self.client.post(f"/v1/strategies/{strategy['id']}/arm", json={"confirmation": "ARM SANDBOX STRATEGY"}).status_code, 409)

    def test_initial_below_threshold_does_not_buy_without_crossing(self):
        strategy = self.create()
        self.arm(strategy)
        self.broker.quote_price = "99"
        self.tick(strategy)
        self.assertEqual(self.broker.place_calls, 0)

    def test_crossing_places_two_stable_owned_buy_legs(self):
        strategy = self.enter()
        self.assertEqual(strategy["status"], "ENTERING")
        self.assertEqual(len(strategy["orders"]), 2)
        self.assertEqual(self.broker.place_calls, 2)
        self.assertTrue(all(order["strategy_id"] == strategy["id"] for order in strategy["orders"]))
        self.tick(strategy)
        self.assertEqual(self.broker.place_calls, 2)

    def test_fill_then_profit_exit_then_cooldown(self):
        strategy = self.hold()
        self.assertEqual(strategy["status"], "HOLDING")
        self.broker.quote_price = "102"
        strategy = self.tick(strategy)
        self.assertEqual(strategy["status"], "EXITING")
        for order in strategy["orders"]:
            if order["spec"]["side"] == "SELL":
                self.fill(order)
        strategy = self.tick(strategy)
        self.assertEqual(strategy["status"], "WAITING_ENTRY")
        self.assertEqual(strategy["cycles_completed"], 1)
        self.assertTrue(all(Decimal(value) == 0 for value in strategy["owned"].values()))
        self.broker.quote_price = "101"
        self.tick(strategy)
        self.broker.quote_price = "100"
        self.tick(strategy)
        self.assertEqual(self.broker.place_calls, 4)  # Cooldown blocks immediate re-entry.

    def test_downside_bid_threshold_attempts_exit(self):
        strategy = self.hold()
        self.broker.quote_price = "95"
        strategy = self.tick(strategy)
        self.assertEqual(strategy["status"], "EXITING")
        self.assertEqual(len([order for order in strategy["orders"] if order["spec"]["side"] == "SELL"]), 2)

    def test_repeat_requires_fresh_above_then_below_after_cooldown(self):
        strategy = self.hold()
        self.broker.quote_price = "102"
        strategy = self.tick(strategy)
        for order in strategy["orders"]:
            if order["spec"]["side"] == "SELL":
                self.fill(order)
        strategy = self.tick(strategy)
        record = self.app.state.database.strategy(strategy["id"])
        record["next_entry_at"] = 0
        self.app.state.database.save_strategy(record, None)
        self.broker.quote_price = "100"
        self.tick(strategy)
        self.assertEqual(self.broker.place_calls, 4)
        self.broker.quote_price = "101"
        self.tick(strategy)
        self.broker.quote_price = "100"
        strategy = self.tick(strategy)
        self.assertEqual(self.broker.place_calls, 6)
        self.assertEqual(strategy["cycles_started"], 2)

    def test_max_cycles_stops_after_flat_exit(self):
        strategy = self.hold(self.create(max_cycles=1))
        self.broker.quote_price = "102"
        strategy = self.tick(strategy)
        for order in strategy["orders"]:
            if order["spec"]["side"] == "SELL":
                self.fill(order)
        strategy = self.tick(strategy)
        self.assertEqual(strategy["status"], "COMPLETED")
        self.assertEqual(self.client.post(f"/v1/strategies/{strategy['id']}/arm", json={"confirmation": "ARM SANDBOX STRATEGY"}).status_code, 409)

    def test_only_one_strategy_can_be_armed(self):
        self.arm(self.create())
        second = self.create(name="Second")
        self.assertEqual(self.client.post(f"/v1/strategies/{second['id']}/arm", json={"confirmation": "ARM SANDBOX STRATEGY"}).status_code, 409)

    def test_stale_or_closed_quotes_reset_crossing_without_orders(self):
        strategy = self.create()
        self.arm(strategy)
        self.broker.quote_price = "101"
        self.tick(strategy)
        self.broker.quote_age = 20
        self.broker.quote_price = "100"
        self.tick(strategy)
        self.broker.quote_age = 0
        self.tick(strategy)
        self.assertEqual(self.broker.place_calls, 0)

    def test_incoherent_quote_timestamps_skip_signal(self):
        strategy = self.create()
        self.arm(strategy)
        original = self.broker.quotes
        def quotes(symbols):
            rows = original(symbols)
            rows[0]["quote_time"] -= 3000
            return rows
        self.broker.quotes = quotes
        self.assertIn("skipped", self.tick(strategy)["last_error"])
        self.assertEqual(self.broker.place_calls, 0)

    def test_expired_strategy_window_does_not_autocancel_or_exit(self):
        strategy = self.enter()
        self.app.state.service._strategy_grants[strategy["id"]].deadline = 0
        strategy = self.tick(strategy)
        self.assertEqual(strategy["status"], "RECOVERY_REQUIRED")
        self.assertEqual(self.broker.cancel_calls, 0)

    def test_halt_pauses_strategy_without_unrequested_broker_mutation(self):
        strategy = self.enter()
        self.client.put("/v1/risk/kill-switch", json={"enabled": True, "reason": "Emergency stop"})
        self.assertEqual(self.engine.get(strategy["id"])["status"], "RECOVERY_REQUIRED")
        self.assertEqual(self.broker.cancel_calls, 0)

    def test_timeout_cancel_rest_and_stop_no_fill(self):
        strategy = self.enter()
        self.expire_order_deadline(strategy)
        strategy = self.tick(strategy)
        self.assertEqual(strategy["status"], "CLEANUP")
        self.tick(strategy)
        strategy = self.tick(strategy)
        self.assertEqual(strategy["status"], "PAUSED")
        self.assertEqual(self.broker.cancel_calls, 2)
        self.assertEqual(self.broker.place_calls, 2)

    def test_one_filled_leg_is_not_automatically_reversed(self):
        strategy = self.enter()
        self.fill(strategy["orders"][0])
        self.expire_order_deadline(strategy)
        self.tick(strategy)
        self.tick(strategy)
        strategy = self.tick(strategy)
        self.assertEqual(strategy["status"], "RECOVERY_REQUIRED")
        self.assertEqual(self.broker.place_calls, 2)
        self.assertEqual(self.broker.cancel_calls, 1)
        self.assertTrue(any(Decimal(value) > 0 for value in strategy["owned"].values()))

    def test_unknown_first_leg_stops_second_dispatch(self):
        strategy = self.create()
        self.arm(strategy)
        self.broker.quote_price = "101"
        self.tick(strategy)
        self.broker.place_error = BrokerError("Simulated uncertain first leg")
        self.broker.quote_price = "100"
        strategy = self.tick(strategy)
        self.assertEqual(strategy["status"], "CLEANUP")
        self.assertEqual(self.broker.place_calls, 1)

    def test_strategy_holding_is_reserved_from_unrelated_manual_sells(self):
        strategy = self.hold()
        response = self.client.post("/v1/risk/check", json={"symbol": "AAPL", "side": "SELL", "quantity": 1, "limit_price": "100"})
        self.assertFalse(response.json()["allowed"])
        self.assertEqual(strategy["owned"]["AAPL"], "1")

    def test_explicit_release_after_pause_allows_manual_recovery(self):
        strategy = self.hold()
        self.client.post(f"/v1/strategies/{strategy['id']}/pause")
        release = self.client.post(f"/v1/strategies/{strategy['id']}/release", json={"confirmation": "RELEASE STRATEGY HOLDINGS"})
        self.assertEqual(release.status_code, 200, release.text)
        self.assertEqual(release.json()["status"], "RELEASED")
        self.assertTrue(self.client.post("/v1/risk/check", json={"symbol": "AAPL", "side": "SELL", "quantity": 1, "limit_price": "100"}).json()["allowed"])

    def test_restart_requires_explicit_recovery_not_auto_arming(self):
        strategy = self.hold()
        self.app.state.database.initialize()
        self.assertEqual(self.engine.get(strategy["id"])["status"], "RECOVERY_REQUIRED")
        self.tick(strategy)
        self.assertEqual(self.broker.place_calls, 2)

    def test_strategy_owned_orders_cannot_be_manually_changed_while_active(self):
        strategy = self.enter()
        order = strategy["orders"][0]
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/cancel").status_code, 409)
        self.assertEqual(self.broker.cancel_calls, 0)

    def test_live_strategy_requires_separate_opt_in_and_global_live_arm(self):
        strategy = self.create()
        service = self.app.state.service
        # A retargeted config cannot authorize a sandbox strategy.
        service.config = replace(self.config, broker=Settings(environment="production", account_id="other", enable_live_trading=True), enable_live_strategies=True)
        self.assertEqual(self.client.post(f"/v1/strategies/{strategy['id']}/arm", json={"confirmation": "ARM LIVE STRATEGY"}).status_code, 409)


class LiveStrategyTests(unittest.TestCase):
    def test_live_grant_requires_file_opt_in_and_global_arm_and_is_narrow(self):
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(broker=Settings(environment="production", app_key="fixture-live-key", app_secret="fixture-live-secret", account_id="fixture-live", enable_live_trading=True),
                               api_key="x" * 48, database=Path(directory) / "live.sqlite3", reconcile_interval=0,
                               limits=RiskLimits.live_defaults())
            broker = FakeBroker("fixture-live")
            app = create_app(config, broker)
            with TestClient(app) as client:
                client.headers.update({"X-API-Key": config.api_key, "X-Trading-Environment": "production"})
                client.post("/v1/broker/connect")
                strategy = client.post("/v1/strategies", json={"name": "Live pair", "symbol_a": "AAPL", "symbol_b": "MSFT", "entry_sum": "200", "profit_increment": "4", "downside_delta": "10", "max_cycles": 1}).json()
                path = f"/v1/strategies/{strategy['id']}/arm"
                self.assertEqual(client.post(path, json={"confirmation": "ARM LIVE STRATEGY"}).status_code, 409)
                client.post("/v1/live/arm", json={"confirmation": "ENABLE LIVE TRADING"})
                self.assertEqual(client.post(path, json={"confirmation": "ARM LIVE STRATEGY"}).status_code, 403)
                profile = app.state.profile_manager.active
                profile.service.config = replace(config, enable_live_strategies=True, reconcile_interval=15)
                profile.strategies.config = profile.service.config
                self.assertEqual(client.post(path, json={"confirmation": "ARM LIVE STRATEGY"}).status_code, 200)
                broker.quote_price = "101"
                client.post(f"/v1/strategies/{strategy['id']}/tick")
                broker.quote_price = "100"
                response = client.post(f"/v1/strategies/{strategy['id']}/tick")
                self.assertEqual(response.json()["status"], "ENTERING")
                self.assertEqual(broker.place_calls, 2)
                self.assertEqual(broker.last_permit.environment, "production")
                self.assertEqual(broker.last_permit.operation, "SUBMIT")
                client.put("/v1/risk/kill-switch", json={"enabled": True, "reason": "Live strategy stop"})
                client.post(f"/v1/strategies/{strategy['id']}/tick")
                self.assertEqual(broker.place_calls, 2)

    def test_strategy_endpoint_classification_requires_live_review(self):
        # Browser helper tests are in console_core.test.mjs; this ensures headers
        # still bind the newly introduced API control routes.
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(broker=Settings(environment="production", account_id="live"), api_key="x" * 48,
                               database=Path(directory) / "live.sqlite3", reconcile_interval=0)
            with TestClient(create_app(config, FakeBroker("live"))) as client:
                client.headers["X-API-Key"] = config.api_key
                response = client.post("/v1/strategies", json={"name": "Pair", "symbol_a": "AAPL", "symbol_b": "MSFT", "entry_sum": "200", "profit_increment": "4", "downside_delta": "10"})
                self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
