from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from trading.app import create_app
from trading.config import AppConfig
from trading.database import Database
from trading.models import BrokerError, DomainError
from webull_cli import Settings


class TradingAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            broker=Settings(account_id="fake-cash-account"), api_key="test-key-" + "x" * 48,
            database=Path(self.temp.name) / "trading.sqlite3", reconcile_interval=0,
        )
        self.broker = FakeBroker()
        self.app = create_app(self.config, self.broker)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.client.headers.update({"X-API-Key": self.config.api_key})
        self.spec = {"symbol": "AAPL", "side": "BUY", "quantity": 1, "limit_price": "100"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def connect(self, enable=False):
        self.assertEqual(self.client.post("/v1/broker/connect").status_code, 200)
        if enable:
            self.assertEqual(self.client.put("/v1/risk/kill-switch", json={"enabled": False, "reason": "Fixture trading enabled"}).status_code, 200)

    def draft(self, key="test-order", spec=None):
        response = self.client.post("/v1/orders", json=spec or self.spec, headers={"Idempotency-Key": key})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def submit(self, order):
        response = self.client.post(f"/v1/orders/{order['id']}/submit")
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def open_order(self):
        self.connect(enable=True)
        return self.submit(self.draft())

    def test_liveness_is_public_and_does_not_connect(self):
        self.client.headers.pop("X-API-Key")
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertFalse(self.broker.connected)

    def test_all_business_routes_require_authentication(self):
        self.client.headers.pop("X-API-Key")
        for path in ("/v1/system", "/v1/orders", "/v1/portfolio", "/v1/risk", "/v1/audit"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_wrong_key_is_rejected(self):
        self.client.headers["X-API-Key"] = "wrong-key"
        self.assertEqual(self.client.get("/v1/system").status_code, 401)

    def test_startup_is_halted_and_disconnected(self):
        result = self.client.get("/v1/system").json()
        self.assertTrue(result["kill_switch"]["enabled"])
        self.assertFalse(result["connected"])
        self.assertFalse(result["production_mutations_enabled"])

    def test_connect_never_submits_orders(self):
        self.connect()
        self.assertEqual(self.broker.place_calls, 0)

    def test_draft_is_offline_and_persistent(self):
        order = self.draft()
        self.assertEqual(order["state"], "DRAFT")
        self.assertEqual(self.client.get(f"/v1/orders/{order['id']}").json()["id"], order["id"])
        self.assertEqual(self.broker.place_calls, 0)

    def test_idempotency_key_required(self):
        self.assertEqual(self.client.post("/v1/orders", json=self.spec).status_code, 422)

    def test_idempotent_draft_reuses_order(self):
        self.assertEqual(self.draft()["id"], self.draft()["id"])

    def test_equivalent_prices_are_idempotent(self):
        first = self.draft()
        second = self.draft(spec={**self.spec, "limit_price": "100.00"})
        self.assertEqual(first["id"], second["id"])

    def test_idempotency_conflict(self):
        self.draft()
        response = self.client.post("/v1/orders", json={**self.spec, "quantity": 2}, headers={"Idempotency-Key": "test-order"})
        self.assertEqual(response.status_code, 409)

    def test_order_schema_rejects_unsupported_or_invalid_values(self):
        for change in ({"quantity": 1.5}, {"quantity": "1"}, {"quantity": 0}, {"limit_price": "NaN"},
                       {"limit_price": "-1"}, {"order_type": "MARKET"}, {"side": "SHORT"}, {"surprise": True}):
            with self.subTest(change=change):
                self.assertEqual(self.client.post("/v1/orders", json={**self.spec, **change}, headers={"Idempotency-Key": "invalid"}).status_code, 422)

    def test_preview_is_safe_while_halted(self):
        self.connect()
        response = self.client.post(f"/v1/orders/{self.draft()['id']}/preview")
        self.assertTrue(response.json()["risk"]["allowed"])
        self.assertFalse(response.json()["submitted"])
        self.assertEqual(self.broker.place_calls, 0)

    def test_submit_is_blocked_while_halted(self):
        self.connect()
        response = self.client.post(f"/v1/orders/{self.draft()['id']}/submit")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.broker.place_calls, 0)

    def test_submit_requires_connection(self):
        response = self.client.post(f"/v1/orders/{self.draft()['id']}/submit")
        self.assertEqual(response.status_code, 503)

    def test_complete_order_lifecycle(self):
        order = self.open_order()
        self.assertEqual(order["state"], "OPEN")
        response = self.client.patch(f"/v1/orders/{order['id']}", json={"quantity": 2, "limit_price": "99"})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(Decimal(response.json()["spec"]["limit_price"]), Decimal(99))
        response = self.client.post(f"/v1/orders/{order['id']}/cancel")
        self.assertEqual(response.json()["state"], "CANCELLED")
        self.assertEqual((self.broker.place_calls, self.broker.replace_calls, self.broker.cancel_calls), (1, 1, 1))

    def test_duplicate_submit_never_reaches_broker_twice(self):
        order = self.open_order()
        self.submit(order)
        self.assertEqual(self.broker.place_calls, 1)

    def test_concurrent_submits_are_serialized(self):
        self.connect(enable=True)
        order = self.draft()
        service = self.app.state.service
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: service.submit(order["id"]), range(4)))
        self.assertEqual(self.broker.place_calls, 1)
        self.assertTrue(all(result["state"] == "OPEN" for result in results))

    def test_timeout_is_unknown_and_never_automatically_resubmitted(self):
        self.connect(enable=True)
        self.broker.place_error = BrokerError("Simulated timeout after acceptance")
        order = self.submit(self.draft())
        self.assertEqual(order["state"], "UNKNOWN")
        self.submit(order)
        self.assertEqual(self.broker.place_calls, 1)
        response = self.client.post(f"/v1/orders/{order['id']}/reconcile")
        self.assertEqual(response.json()["state"], "OPEN")
        self.assertIsNone(response.json()["pending_action"])

    def test_unresolved_submission_blocks_new_risk(self):
        self.connect(enable=True)
        self.broker.place_error = BrokerError("Simulated ambiguous timeout")
        self.submit(self.draft())
        response = self.client.post(f"/v1/orders/{self.draft('another')['id']}/submit")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.broker.place_calls, 1)

    def test_definitive_broker_rejection_is_not_unknown(self):
        self.connect(enable=True)
        self.broker.place_error = BrokerError("Simulated rejection", definitive=True)
        order = self.submit(self.draft())
        self.assertEqual(order["state"], "REJECTED")
        self.assertIsNone(order["pending_action"])

    def test_cancel_remains_available_under_kill_switch(self):
        order = self.open_order()
        self.client.put("/v1/risk/kill-switch", json={"enabled": True, "reason": "Emergency fixture halt"})
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/cancel").json()["state"], "CANCELLED")

    def test_repeated_cancel_does_not_duplicate_mutation(self):
        order = self.open_order()
        self.client.post(f"/v1/orders/{order['id']}/cancel")
        self.client.post(f"/v1/orders/{order['id']}/cancel")
        self.assertEqual(self.broker.cancel_calls, 1)

    def test_delayed_cancel_stays_pending(self):
        order = self.open_order()
        self.broker.hold_cancel = True
        response = self.client.post(f"/v1/orders/{order['id']}/cancel")
        self.assertEqual(response.json()["state"], "CANCELLING")
        self.assertEqual(response.json()["pending_action"], "CANCEL")
        self.client.post(f"/v1/orders/{order['id']}/cancel")
        self.assertEqual(self.broker.cancel_calls, 1)

    def test_delayed_replace_does_not_claim_new_price(self):
        order = self.open_order()
        self.broker.hold_replace = True
        response = self.client.patch(f"/v1/orders/{order['id']}", json={"quantity": 1, "limit_price": "99"})
        self.assertEqual(response.json()["state"], "MODIFYING")
        self.assertEqual(Decimal(response.json()["spec"]["limit_price"]), Decimal(100))
        self.assertEqual(Decimal(response.json()["pending_spec"]["limit_price"]), Decimal(99))

    def test_replace_timeout_can_be_reconciled(self):
        order = self.open_order()
        self.broker.replace_error = BrokerError("Simulated replacement timeout")
        response = self.client.patch(f"/v1/orders/{order['id']}", json={"quantity": 1, "limit_price": "99"})
        self.assertEqual(response.json()["state"], "UNKNOWN")
        response = self.client.post(f"/v1/orders/{order['id']}/reconcile")
        self.assertEqual(response.json()["state"], "OPEN")
        self.assertIsNone(response.json()["pending_action"])

    def test_fill_reconciliation(self):
        order = self.open_order()
        self.broker.order_data[order["client_order_id"]].update(status="FILLED", filled_quantity="1", avg_filled_price="100")
        response = self.client.post(f"/v1/orders/{order['id']}/reconcile")
        self.assertEqual(response.json()["state"], "FILLED")
        self.assertEqual(response.json()["average_fill_price"], "100")

    def test_invalid_fill_quantities_do_not_corrupt_ledger(self):
        order = self.open_order()
        self.broker.order_data[order["client_order_id"]].update(status="FILLED", filled_quantity="2")
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/reconcile").status_code, 502)
        self.assertEqual(self.app.state.database.get(order["id"])["filled_quantity"], "0")

    def risk_check(self, spec=None):
        return self.client.post("/v1/risk/check", json=spec or self.spec).json()

    def test_long_only_blocks_sell_without_shares(self):
        self.connect()
        result = self.risk_check({**self.spec, "side": "SELL"})
        self.assertFalse(result["allowed"])
        self.assertIn("short selling", " ".join(result["violations"]))

    def test_owned_stock_can_be_sold(self):
        self.connect()
        self.broker.position_data = [{"symbol": "AAPL", "quantity": "5", "last_price": "100", "instrument_type": "EQUITY", "currency": "USD"}]
        self.assertTrue(self.risk_check({**self.spec, "side": "SELL"})["allowed"])

    def test_pending_sells_reserve_shares(self):
        self.connect()
        self.broker.position_data = [{"symbol": "AAPL", "quantity": "1", "last_price": "100", "instrument_type": "EQUITY", "currency": "USD"}]
        self.broker.external_orders = [{"combo_type": "NORMAL", "orders": [{"symbol": "AAPL", "instrument_type": "EQUITY", "side": "SELL", "total_quantity": "1", "filled_quantity": "0"}]}]
        self.assertFalse(self.risk_check({**self.spec, "side": "SELL"})["allowed"])

    def test_insufficient_settled_cash(self):
        self.connect()
        self.broker.cash = "100"
        self.assertFalse(self.risk_check()["allowed"])  # 1% fee buffer.

    def test_daily_loss_blocks_buys(self):
        self.connect()
        self.broker.daily_pnl = "-1000"
        self.assertFalse(self.risk_check()["allowed"])

    def test_stale_quote_blocks_risk(self):
        self.connect()
        self.broker.quote_age = 121
        self.assertFalse(self.risk_check()["allowed"])

    def test_closed_session_blocks_risk(self):
        self.connect()
        self.broker.trade_status = "CLOSED"
        self.assertFalse(self.risk_check()["allowed"])

    def test_large_price_deviation_blocks_risk(self):
        self.connect()
        self.assertFalse(self.risk_check({**self.spec, "limit_price": "10"})["allowed"])

    def test_quantity_and_notional_limits(self):
        self.connect()
        self.assertFalse(self.risk_check({**self.spec, "quantity": 101})["allowed"])

    def test_symbol_exposure_counts_positions(self):
        self.connect()
        self.broker.position_data = [{"symbol": "AAPL", "quantity": "200", "last_price": "100", "instrument_type": "EQUITY", "currency": "USD"}]
        self.assertFalse(self.risk_check()["allowed"])

    def test_malformed_broker_risk_data_fails_closed(self):
        self.connect()
        self.broker.cash = "not-a-number"
        self.assertFalse(self.risk_check()["allowed"])

    def test_option_positions_fail_closed(self):
        self.connect()
        self.broker.position_data = [{"instrument_type": "OPTION", "currency": "USD"}]
        self.assertFalse(self.risk_check()["allowed"])

    def test_watchlist_persistence_and_deduplication(self):
        self.assertEqual(self.client.put("/v1/watchlist", json={"symbols": ["AAPL", "AAPL", "MSFT"]}).json()["symbols"], ["AAPL", "MSFT"])
        self.assertEqual(self.client.get("/v1/watchlist").json()["symbols"], ["AAPL", "MSFT"])

    def test_market_data_and_portfolio(self):
        self.connect()
        self.assertEqual(self.client.get("/v1/market/quotes?symbols=aapl,MSFT").status_code, 200)
        self.assertEqual(self.client.get("/v1/market/bars/AAPL?count=5").status_code, 200)
        self.assertEqual(self.client.get("/v1/portfolio").status_code, 200)

    def test_market_query_validation(self):
        self.connect()
        for url in ("/v1/market/bars/AAPL?count=1201", "/v1/market/bars/AAPL?timespan=bad", "/v1/market/quotes?symbols=bad!", "/v1/market/quotes?symbols=AAPL,"):
            self.assertEqual(self.client.get(url).status_code, 422)

    def test_audit_and_reports_are_persisted(self):
        order = self.open_order()
        self.client.post(f"/v1/orders/{order['id']}/cancel")
        actions = [item["action"] for item in self.client.get("/v1/audit").json()["data"]]
        self.assertTrue({"draft_created", "submit_sent", "submit_acknowledged"}.issubset(actions))
        self.assertIn("cancel_sent", actions)
        self.assertEqual(self.client.get("/v1/reports/orders").json()["order_counts"]["CANCELLED"], 1)

    def test_response_headers(self):
        response = self.client.get("/health")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(len(response.headers["X-Request-ID"]), 32)

    def test_configuration_mutation_cannot_retarget_a_sandbox_order(self):
        self.connect(enable=True)
        order = self.draft()
        service = self.app.state.service
        service.config = replace(self.config, broker=Settings(environment="production", account_id="fake", enable_live_trading=True))
        with self.assertRaises(DomainError) as error:
            service.submit(order["id"])
        self.assertEqual(error.exception.code, "order_environment_mismatch")
        self.assertEqual(self.broker.place_calls, 0)

    def test_restart_recovers_interrupted_intent_and_halts(self):
        order = self.draft()
        db = self.app.state.database
        db.update(order["id"], {"state": "SUBMITTING", "pending_action": "SUBMIT"}, "submit_sent")
        db.initialize()
        self.assertEqual(db.get(order["id"])["state"], "UNKNOWN")
        self.assertTrue(db.state("halt")["enabled"])

    def test_database_cannot_be_reused_for_another_account(self):
        with self.assertRaises(ValueError):
            Database(self.config.database, "different-account").initialize()

    def test_another_server_cannot_own_the_same_database(self):
        second = create_app(self.config, FakeBroker())
        with self.assertRaises(RuntimeError):
            with TestClient(second):
                pass

    def test_api_rate_limit_does_not_prevent_emergency_halt(self):
        # Exhaust just the authenticated request limiter without broker calls.
        for _ in range(self.config.request_limit):
            self.client.get("/v1/system")
        self.assertEqual(self.client.get("/v1/system").status_code, 429)
        response = self.client.put("/v1/risk/kill-switch", json={"enabled": True, "reason": "Rate limit emergency"})
        self.assertEqual(response.status_code, 200)

    def test_daily_submission_cap(self):
        self.connect()
        for _ in range(self.config.limits.max_daily_orders):
            self.app.state.database.audit_event("submit_sent")
        self.assertFalse(self.risk_check()["allowed"])

    def test_portfolio_exposure_cap(self):
        self.connect()
        self.broker.position_data = [{"symbol": "MSFT", "quantity": "1000", "last_price": "100", "instrument_type": "EQUITY", "currency": "USD"}]
        self.assertFalse(self.risk_check()["allowed"])

    def test_external_open_order_cap(self):
        self.connect()
        self.broker.external_orders = [{"combo_type": "NORMAL", "orders": [{
            "symbol": "MSFT", "instrument_type": "EQUITY", "side": "BUY", "total_quantity": "1",
            "filled_quantity": "0", "limit_price": "100", "client_order_id": f"external-{index}",
        }]} for index in range(self.config.limits.max_open_orders)]
        self.assertFalse(self.risk_check()["allowed"])

    def test_unchanged_reconciliation_does_not_flood_audit(self):
        order = self.open_order()
        before = self.app.state.database.report()["audit_events"]
        self.client.post(f"/v1/orders/{order['id']}/reconcile")
        self.assertEqual(self.app.state.database.report()["audit_events"], before)

    def test_unreflected_local_buy_reserves_cash(self):
        self.connect(enable=True)
        self.broker.cash = "150"
        self.submit(self.draft())
        self.broker.open_orders = lambda: []  # Simulate delayed open-order visibility.
        result = self.risk_check()
        self.assertFalse(result["allowed"])
        self.assertIn("Insufficient", " ".join(result["violations"]))

    def test_missing_fill_fields_returns_broker_error_not_500(self):
        order = self.open_order()
        del self.broker.order_data[order["client_order_id"]]["filled_quantity"]
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/reconcile").status_code, 502)


if __name__ == "__main__":
    unittest.main()
