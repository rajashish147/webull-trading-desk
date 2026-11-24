from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from trading.app import create_app
from trading.broker import WebullGateway
from trading.config import AppConfig, RiskLimits
from trading.database import Database
from trading.safety import ExecutionPermit, digest
from webull_cli import Settings


class LiveTradingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            broker=Settings(environment="production", app_key="fixture-live-key", app_secret="fixture-live-secret",
                            account_id="live-cash", enable_live_trading=True),
            api_key="test-backend-key-" + "x" * 48, database=Path(self.temp.name) / "live.sqlite3",
            limits=RiskLimits.live_defaults(), reconcile_interval=0,
        )
        self.broker = FakeBroker("live-cash")
        self.app = create_app(self.config, self.broker)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.client.headers.update({"X-API-Key": self.config.api_key, "X-Trading-Environment": "production"})
        self.spec = {"symbol": "AAPL", "side": "BUY", "quantity": 1, "limit_price": "100"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def connect(self):
        self.assertEqual(self.client.post("/v1/broker/connect").status_code, 200)

    def arm(self):
        self.connect()
        response = self.client.post("/v1/live/arm", json={"confirmation": "ENABLE LIVE TRADING"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def draft(self, key="live-test", spec=None):
        response = self.client.post("/v1/orders", json=spec or self.spec, headers={"Idempotency-Key": key})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def approval(self, order, operation="SUBMIT", **replacement):
        response = self.client.post(f"/v1/orders/{order['id']}/live-approval", json={"operation": operation, **replacement})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def headers(approval):
        return {"X-Live-Confirmation": approval["token"], "X-Live-Acknowledgement": approval["acknowledgement"]}

    def submit(self, order, approval=None):
        approval = approval or self.approval(order)
        response = self.client.post(f"/v1/orders/{order['id']}/submit", headers=self.headers(approval))
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def open_order(self):
        self.arm()
        return self.submit(self.draft())

    def test_live_starts_disarmed(self):
        data = self.client.get("/v1/system").json()
        self.assertTrue(data["kill_switch"]["enabled"])
        self.assertFalse(data["live_armed"])
        self.assertFalse(data["production_mutations_enabled"])

    def test_live_draft_is_offline_and_scoped(self):
        order = self.draft()
        self.assertEqual(order["environment"], "production")
        self.assertEqual(order["state"], "DRAFT")
        self.assertEqual(self.broker.place_calls, 0)

    def test_production_write_requires_environment_header(self):
        self.client.headers.pop("X-Trading-Environment")
        response = self.client.post("/v1/orders", json=self.spec, headers={"Idempotency-Key": "missing-env"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "environment_required")

    def test_disarmed_submission_never_reaches_broker(self):
        self.connect()
        response = self.client.post(f"/v1/orders/{self.draft()['id']}/submit")
        self.assertEqual(response.json()["error"]["code"], "live_not_armed")
        self.assertEqual(self.broker.place_calls, 0)

    def test_arm_requires_typed_phrase(self):
        self.connect()
        response = self.client.post("/v1/live/arm", json={"confirmation": "yes"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.app.state.service.live_armed())

    def test_generic_kill_switch_enable_cannot_arm_live(self):
        self.connect()
        response = self.client.put("/v1/risk/kill-switch", json={"enabled": False, "reason": "Attempt bypass"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.app.state.service.live_armed())

    def test_file_opt_in_required_even_with_correct_arm_phrase(self):
        self.connect()
        self.app.state.service.config = replace(self.config, broker=replace(self.config.broker, enable_live_trading=False))
        response = self.client.post("/v1/live/arm", json={"confirmation": "ENABLE LIVE TRADING"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.app.state.service.live_armed())

    def test_invalid_cash_readiness_cannot_arm(self):
        self.connect()
        self.broker.cash = "NaN"
        response = self.client.post("/v1/live/arm", json={"confirmation": "ENABLE LIVE TRADING"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.app.state.service.live_armed())

    def test_arming_does_not_trade(self):
        self.arm()
        self.assertTrue(self.client.get("/v1/system").json()["live_armed"])
        self.assertEqual(self.broker.place_calls, 0)

    def test_arm_expiry_blocks_new_submissions(self):
        self.arm()
        order = self.draft()
        self.app.state.service._arm_deadline = 0
        response = self.client.post(f"/v1/orders/{order['id']}/submit")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.broker.place_calls, 0)

    def test_live_submit_needs_exact_one_time_confirmation(self):
        self.arm()
        order = self.draft()
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/submit").status_code, 409)
        result = self.submit(order)
        self.assertEqual(result["state"], "OPEN")
        self.assertIsInstance(self.broker.last_permit, ExecutionPermit)
        self.assertEqual(self.broker.last_permit.operation, "SUBMIT")
        self.assertEqual(self.broker.place_calls, 1)

    def test_missing_typed_order_acknowledgement_is_rejected(self):
        self.arm()
        order = self.draft()
        approval = self.approval(order)
        response = self.client.post(f"/v1/orders/{order['id']}/submit", headers={"X-Live-Confirmation": approval["token"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.broker.place_calls, 0)

    def test_confirmation_cannot_be_used_for_another_order(self):
        self.arm()
        first, second = self.draft("first"), self.draft("second")
        approval = self.approval(first)
        response = self.client.post(f"/v1/orders/{second['id']}/submit", headers=self.headers(approval))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.broker.place_calls, 0)

    def test_expired_confirmation_is_rejected(self):
        self.arm()
        order = self.draft()
        approval = self.approval(order)
        token_hash = hashlib.sha256(approval["token"].encode()).hexdigest()
        self.app.state.service._approval_deadlines[token_hash] = 0
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/submit", headers=self.headers(approval)).status_code, 409)
        self.assertEqual(self.broker.place_calls, 0)

    def test_database_expired_confirmation_is_rejected(self):
        self.arm()
        order = self.draft()
        approval = self.approval(order)
        with self.app.state.database.transaction() as conn:
            conn.execute("UPDATE live_approvals SET expires_at=0")
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/submit", headers=self.headers(approval)).status_code, 409)
        self.assertEqual(self.broker.place_calls, 0)

    def test_duplicate_submit_never_resubmits(self):
        self.arm()
        order = self.draft()
        approval = self.approval(order)
        self.submit(order, approval)
        self.submit(order, approval)
        self.assertEqual(self.broker.place_calls, 1)

    def test_rearming_invalidates_previous_confirmation(self):
        self.arm()
        order = self.draft()
        approval = self.approval(order)
        self.arm()
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/submit", headers=self.headers(approval)).status_code, 409)
        self.assertEqual(self.broker.place_calls, 0)

    def test_halt_invalidates_confirmation(self):
        self.arm()
        order = self.draft()
        approval = self.approval(order)
        self.client.put("/v1/risk/kill-switch", json={"enabled": True, "reason": "Operator halt"})
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/submit", headers=self.headers(approval)).status_code, 409)
        self.assertEqual(self.broker.place_calls, 0)

    def test_live_replacement_requires_matching_quantity_and_limit(self):
        order = self.open_order()
        approval = self.approval(order, "REPLACE", quantity=2, limit_price="99")
        wrong = self.client.patch(f"/v1/orders/{order['id']}", json={"quantity": 3, "limit_price": "99"}, headers=self.headers(approval))
        self.assertEqual(wrong.status_code, 409)
        self.assertEqual(self.broker.replace_calls, 0)
        correct = self.client.patch(f"/v1/orders/{order['id']}", json={"quantity": 2, "limit_price": "99"}, headers=self.headers(approval))
        self.assertEqual(correct.status_code, 202, correct.text)
        self.assertEqual(correct.json()["spec"]["quantity"], 2)

    def test_fill_change_invalidates_replacement_confirmation(self):
        order = self.open_order()
        approval = self.approval(order, "REPLACE", quantity=2, limit_price="100")
        self.broker.order_data[order["client_order_id"]].update(filled_quantity="1", status="FILLED")
        response = self.client.patch(f"/v1/orders/{order['id']}", json={"quantity": 2, "limit_price": "100"}, headers=self.headers(approval))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.broker.replace_calls, 0)

    def test_live_cancel_works_disarmed_but_requires_approval(self):
        order = self.open_order()
        self.client.put("/v1/risk/kill-switch", json={"enabled": True, "reason": "Emergency disarm"})
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/cancel").status_code, 409)
        approval = self.approval(order, "CANCEL")
        response = self.client.post(f"/v1/orders/{order['id']}/cancel", headers=self.headers(approval))
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "CANCELLED")
        self.assertEqual(self.broker.last_permit.operation, "CANCEL")

    def test_cancel_remains_available_if_file_opt_in_is_disabled(self):
        order = self.open_order()
        self.app.state.service.config = replace(self.config, broker=replace(self.config.broker, enable_live_trading=False))
        approval = self.approval(order, "CANCEL")
        response = self.client.post(f"/v1/orders/{order['id']}/cancel", headers=self.headers(approval))
        self.assertEqual(response.json()["state"], "CANCELLED")

    def test_local_live_draft_can_be_cancelled_without_execution_approval(self):
        self.connect()
        order = self.draft()
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/cancel").json()["state"], "CANCELLED")
        self.assertEqual(self.broker.cancel_calls, 0)

    def test_timeout_is_unknown_and_consumed_intent_is_not_retried(self):
        self.arm()
        order = self.draft()
        approval = self.approval(order)
        from trading.models import BrokerError
        self.broker.place_error = BrokerError("Simulated live timeout after acceptance")
        result = self.submit(order, approval)
        self.assertEqual(result["state"], "UNKNOWN")
        self.submit(order, approval)
        self.assertEqual(self.broker.place_calls, 1)
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/reconcile").json()["state"], "OPEN")

    def test_restart_invalidates_arm_and_pending_confirmations(self):
        self.arm()
        order = self.draft()
        approval = self.approval(order)
        self.app.state.database.initialize()
        self.assertFalse(self.app.state.service.live_armed())
        with self.app.state.database.transaction() as conn:
            self.assertEqual(conn.execute("SELECT consumed FROM live_approvals").fetchone()[0], 1)
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/submit", headers=self.headers(approval)).status_code, 409)

    def test_live_risk_limits_are_conservative(self):
        self.arm()
        order = self.draft(spec={**self.spec, "quantity": 6})
        response = self.client.post(f"/v1/orders/{order['id']}/live-approval", json={"operation": "SUBMIT"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.broker.place_calls, 0)

    def test_live_cent_price_policy(self):
        self.connect()
        response = self.client.post("/v1/risk/check", json={**self.spec, "limit_price": "100.001"})
        self.assertFalse(response.json()["allowed"])

    def test_live_preview_requires_fees_and_valid_currency(self):
        self.arm()
        order = self.draft()
        self.broker.preview = lambda payload: {"estimated_cost": "100", "currency": "USD"}
        response = self.client.post(f"/v1/orders/{order['id']}/live-approval", json={"operation": "SUBMIT"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.broker.place_calls, 0)

    def test_optional_preview_currency_is_derived_from_validated_usd_account(self):
        self.arm()
        order = self.draft()
        self.broker.preview = lambda payload: {"estimated_cost": "100", "estimated_transaction_fee": "0"}
        self.approval(order)

    def test_non_usd_settlement_cannot_arm(self):
        self.connect()
        original = self.broker.balance
        self.broker.balance = lambda: {**original(), "total_asset_currency": "HKD"}
        response = self.client.post("/v1/live/arm", json={"confirmation": "ENABLE LIVE TRADING"})
        self.assertEqual(response.status_code, 409)

    def test_live_long_only_sell_can_reduce_an_owned_position(self):
        self.broker.position_data = [{"symbol": "AAPL", "quantity": "100", "last_price": "100", "instrument_type": "EQUITY", "currency": "USD"}]
        self.broker.daily_pnl = "-100"
        self.arm()
        order = self.draft(spec={**self.spec, "side": "SELL"})
        result = self.submit(order)
        self.assertEqual(result["state"], "OPEN")
        self.assertEqual(result["spec"]["side"], "SELL")

    def test_live_sell_cannot_open_a_short_position(self):
        self.arm()
        order = self.draft(spec={**self.spec, "side": "SELL"})
        response = self.client.post(f"/v1/orders/{order['id']}/live-approval", json={"operation": "SUBMIT"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.broker.place_calls, 0)

    def test_malformed_detail_blocks_new_risk_until_reconciled(self):
        order = self.open_order()
        original = self.broker.detail
        self.broker.detail = lambda key: {"orders": [None]}
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/reconcile").status_code, 502)
        self.assertEqual(self.app.state.database.get(order["id"])["state"], "UNKNOWN")
        self.broker.detail = original
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/reconcile").json()["state"], "OPEN")

    def test_unknown_instrument_details_are_not_treated_as_equity(self):
        order = self.open_order()
        self.broker.order_data[order["client_order_id"]]["instrument_type"] = "OPTION"
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/reconcile").status_code, 502)
        self.assertEqual(self.app.state.database.get(order["id"])["state"], "UNKNOWN")

    def test_mismatched_replacement_acknowledgement_remains_uncertain(self):
        order = self.open_order()
        approval = self.approval(order, "REPLACE", quantity=2, limit_price="99")
        self.broker.replace = lambda *args, **kwargs: {"client_order_id": "wrong-id", "order_id": "wrong-broker"}
        response = self.client.patch(f"/v1/orders/{order['id']}", json={"quantity": 2, "limit_price": "99"}, headers=self.headers(approval))
        self.assertEqual(response.json()["state"], "UNKNOWN")
        self.assertEqual(response.json()["pending_action"], "REPLACE")


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        base = AppConfig(broker=Settings(account_id="sandbox-cash"), api_key="x" * 48,
                         database=root / "sandbox.sqlite3", reconcile_interval=0)
        live = replace(base, broker=Settings(environment="production", account_id="live-cash", enable_live_trading=True),
                       database=root / "live.sqlite3", limits=RiskLimits.live_defaults())
        self.config = replace(base, profiles={"sandbox": base, "production": live})
        self.sandbox, self.live = FakeBroker("sandbox-cash"), FakeBroker("live-cash")
        self.app = create_app(self.config, {"sandbox": self.sandbox, "production": self.live})
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.client.headers.update({"X-API-Key": base.api_key, "X-Trading-Environment": "sandbox"})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def switch(self, environment):
        phrase = "SWITCH TO LIVE" if environment == "production" else "SWITCH TO SANDBOX"
        response = self.client.put("/v1/environment", json={"environment": environment, "confirmation": phrase})
        if response.status_code == 200:
            self.client.headers["X-Trading-Environment"] = environment
        return response

    def draft(self):
        return self.client.post("/v1/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 1, "limit_price": "100"}, headers={"Idempotency-Key": "same-key"}).json()

    def test_switch_halts_both_and_keeps_ledgers_separate(self):
        sandbox_order = self.draft()
        self.assertEqual(self.switch("production").status_code, 200)
        self.assertEqual(self.client.get("/v1/orders").json()["data"], [])
        live_order = self.draft()
        self.assertNotEqual(live_order["id"], sandbox_order["id"])
        self.assertEqual(live_order["environment"], "production")
        self.assertTrue(self.client.get("/v1/system").json()["kill_switch"]["enabled"])
        self.assertEqual(self.switch("sandbox").status_code, 200)
        self.assertEqual(self.client.get("/v1/orders").json()["data"][0]["id"], sandbox_order["id"])

    def test_stale_tab_cannot_create_or_submit_in_new_environment(self):
        order = self.draft()
        self.switch("production")
        self.client.headers["X-Trading-Environment"] = "sandbox"
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/submit").status_code, 409)
        self.assertEqual(self.client.post("/v1/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 1, "limit_price": "100"}, headers={"Idempotency-Key": "stale"}).status_code, 409)
        self.assertEqual(self.live.place_calls, 0)

    def test_scope_rejects_order_id_from_other_profile(self):
        order = self.draft()
        self.switch("production")
        self.assertEqual(self.client.post(f"/v1/orders/{order['id']}/submit").status_code, 404)

    def test_active_local_orders_block_switch_and_force_halt(self):
        self.client.post("/v1/broker/connect")
        self.client.put("/v1/risk/kill-switch", json={"enabled": False, "reason": "Sandbox fixture enabled"})
        order = self.draft()
        self.client.post(f"/v1/orders/{order['id']}/submit")
        self.assertEqual(self.switch("production").status_code, 409)
        self.assertTrue(self.client.get("/v1/system").json()["kill_switch"]["enabled"])

    def test_unknown_broker_orders_block_switch(self):
        self.client.post("/v1/broker/connect")
        self.sandbox.external_orders = [{"orders": [{"status": "SUBMITTED"}]}]
        self.assertEqual(self.switch("production").status_code, 409)

    def test_switch_requires_environment_binding_and_phrase(self):
        self.assertEqual(self.client.put("/v1/environment", json={"environment": "production", "confirmation": "yes"}).status_code, 409)
        self.client.headers.pop("X-Trading-Environment")
        self.assertEqual(self.client.put("/v1/environment", json={"environment": "production", "confirmation": "SWITCH TO LIVE"}).status_code, 409)

    def test_global_halt_works_even_from_stale_tab(self):
        self.switch("production")
        self.client.post("/v1/broker/connect")
        self.client.post("/v1/live/arm", json={"confirmation": "ENABLE LIVE TRADING"})
        self.client.headers["X-Trading-Environment"] = "sandbox"
        self.assertEqual(self.client.put("/v1/risk/kill-switch", json={"enabled": True, "reason": "Stale tab emergency halt"}).status_code, 200)
        self.assertFalse(self.client.get("/v1/system").json()["live_armed"])

    def test_selected_environment_does_not_change_startup_default(self):
        self.switch("production")
        self.assertEqual(self.config.broker.environment, "sandbox")


class LiveInfrastructureTests(unittest.TestCase):
    def test_live_limits_do_not_inherit_large_sandbox_limits(self):
        values = {"WEBULL_ENV": "production", "TRADING_API_KEY": "x" * 48,
                  "WEBULL_LIVE_APP_KEY": "live-key", "WEBULL_LIVE_APP_SECRET": "live-secret", "WEBULL_LIVE_ACCOUNT_ID": "live-account",
                  "TRADING_MAX_ORDER_NOTIONAL": "999999"}
        with patch("trading.config.dotenv_values", return_value=values):
            config = AppConfig.from_env()
        self.assertEqual(config.limits.max_order_notional, Decimal(500))
        self.assertFalse(config.broker.enable_live_trading)

    def test_production_never_uses_legacy_sandbox_credentials(self):
        values = {"WEBULL_ENV": "production", "WEBULL_APP_KEY": "sandbox-key", "WEBULL_APP_SECRET": "sandbox-secret", "WEBULL_ACCOUNT_ID": "sandbox-account", "TRADING_API_KEY": "x" * 48}
        with patch("trading.config.dotenv_values", return_value=values), self.assertRaises(ValueError):
            AppConfig.from_env()

    def test_live_startup_preserves_legacy_sandbox_profile(self):
        values = {"WEBULL_ENV": "production", "WEBULL_APP_KEY": "sandbox-key", "WEBULL_APP_SECRET": "sandbox-secret", "WEBULL_ACCOUNT_ID": "sandbox-account",
                  "WEBULL_LIVE_APP_KEY": "live-key", "WEBULL_LIVE_APP_SECRET": "live-secret", "WEBULL_LIVE_ACCOUNT_ID": "live-account", "TRADING_API_KEY": "x" * 48}
        with patch("trading.config.dotenv_values", return_value=values):
            config = AppConfig.from_env()
        self.assertEqual(set(config.profiles), {"sandbox", "production"})
        self.assertEqual(config.profiles["sandbox"].broker.app_key, "sandbox-key")

    def test_portfolio_cache_is_separate_from_fresh_risk_queries(self):
        gateway = WebullGateway(Settings(environment="production", account_id="fixture"))
        gateway.connected, gateway._trade = True, Mock()
        balance_response = Mock(status_code=200)
        balance_response.json.return_value = {"cash": "100"}
        position_response = Mock(status_code=200)
        position_response.json.return_value = []
        gateway._trade.account_v2.get_account_balance.return_value = balance_response
        gateway._trade.account_v2.get_account_position.return_value = position_response
        gateway.balance()
        gateway.positions()
        self.assertTrue(gateway.portfolio()["cached"])
        self.assertEqual(gateway._trade.account_v2.get_account_balance.call_count, 1)
        gateway.balance()  # Risk reads do not use the dashboard cache.
        self.assertEqual(gateway._trade.account_v2.get_account_balance.call_count, 2)

    def test_production_read_quota_is_enforced_before_transport(self):
        gateway = WebullGateway(Settings(environment="production", account_id="fixture"))
        gateway._rate_limit("balance")
        gateway._rate_limit("balance")
        from trading.models import DomainError
        with self.assertRaises(DomainError):
            gateway._rate_limit("balance")

    def test_same_credentials_across_profiles_are_rejected(self):
        values = {"WEBULL_ENV": "sandbox", "WEBULL_APP_KEY": "same-key", "WEBULL_APP_SECRET": "sandbox-secret", "WEBULL_ACCOUNT_ID": "sandbox-account",
                  "WEBULL_LIVE_APP_KEY": "same-key", "WEBULL_LIVE_APP_SECRET": "live-secret", "WEBULL_LIVE_ACCOUNT_ID": "live-account", "TRADING_API_KEY": "x" * 48}
        with patch("trading.config.dotenv_values", return_value=values), self.assertRaises(ValueError):
            AppConfig.from_env()

    def test_gateway_requires_exact_permit_even_with_file_opt_in(self):
        settings = Settings(environment="production", account_id="live-cash", enable_live_trading=True)
        gateway = WebullGateway(settings)
        gateway.connected, gateway._trade = True, Mock()
        payload = {"client_order_id": "test-id", "quantity": "1"}
        from trading.models import DomainError
        with self.assertRaises(DomainError):
            gateway.place(payload)
        gateway._trade.order_v3.place_order.assert_not_called()
        response = Mock(status_code=200)
        response.json.return_value = {"client_order_id": "test-id", "order_id": "broker-test"}
        gateway._trade.order_v3.place_order.return_value = response
        permit = ExecutionPermit("production", "live-cash", "SUBMIT", "test-id", digest(payload), time.time() + 10)
        self.assertEqual(gateway.place(payload, permit)["order_id"], "broker-test")
        with self.assertRaises(DomainError):
            gateway.place(payload, permit)
        self.assertEqual(gateway._trade.order_v3.place_order.call_count, 1)
        with self.assertRaises(DomainError):
            gateway.replace("test-id", 2, "100", permit)

    def test_schema_one_migrates_with_backup_without_losing_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            identity = "sandbox:test-account"
            record = {"id": "legacy-order", "client_order_id": "legacy-order", "state": "SUBMITTING", "created_at": "2026-01-01", "updated_at": "2026-01-01", "pending_action": "SUBMIT"}
            conn = sqlite3.connect(path)
            conn.executescript("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE orders(id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,client_order_id TEXT UNIQUE NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,record TEXT NOT NULL);")
            conn.executemany("INSERT INTO metadata VALUES (?,?)", [("identity", identity), ("schema_version", "1")])
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", ("legacy-order", "legacy-key", "legacy-order", "SUBMITTING", "2026-01-01", "2026-01-01", json.dumps(record)))
            conn.commit()
            conn.close()
            db = Database(path, identity)
            db.initialize()
            migrated = db.get("legacy-order")
            self.assertEqual(migrated["state"], "UNKNOWN")
            self.assertEqual(migrated["environment"], "sandbox")
            backups = list((path.parent / "backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            backup = sqlite3.connect(backups[0])
            self.assertEqual(backup.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], "1")
            backup.close()


if __name__ == "__main__":
    unittest.main()
