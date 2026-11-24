import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import os
import unittest
from unittest.mock import Mock, patch

import webull_cli as cli


class StarterTests(unittest.TestCase):
    def setUp(self):
        self.settings = cli.Settings(
            app_key="test-app-key", app_secret="test-app-secret", account_id="sandbox-account",
        )
        self.order_args = [
            "order", "aapl", "--side", "BUY", "--quantity", "1", "--limit-price", "10.25",
        ]

    def test_sandbox_default(self):
        self.assertEqual(cli.Settings().endpoint, "api.sandbox.webull.com")
        self.assertFalse(cli.Settings().enable_live_trading)

    def test_invalid_environment(self):
        with self.assertRaises(ValueError):
            cli.Settings(environment="unknown")

    def test_env_settings_and_strict_live_switch(self):
        with patch.dict(os.environ, {"WEBULL_ENV": "sandbox", "WEBULL_ENABLE_LIVE_TRADING": "false"}, clear=True):
            with patch.object(cli, "load_dotenv"):
                self.assertEqual(cli.Settings.from_env(), cli.Settings())
        with patch.dict(os.environ, {"WEBULL_ENABLE_LIVE_TRADING": "yes"}, clear=True):
            with patch.object(cli, "load_dotenv"), self.assertRaises(ValueError):
                cli.Settings.from_env()

    def test_credentials_not_in_repr(self):
        self.assertNotIn("test-app-key", repr(self.settings))
        self.assertNotIn("test-app-secret", repr(self.settings))

    def test_missing_credentials(self):
        with self.assertRaises(ValueError):
            cli.Settings().require_credentials()

    def test_status_is_offline(self):
        with patch.object(cli, "api_client") as client:
            result = cli.execute_command(cli.parser().parse_args(["status"]), self.settings)
            client.assert_not_called()
        self.assertFalse(result["api_connection_tested"])
        self.assertNotIn(self.settings.app_secret, str(result))

    def test_order_defaults_to_offline_plan(self):
        args = cli.parser().parse_args(self.order_args)
        with patch.object(cli, "api_client") as client:
            result = cli.execute_command(args, self.settings)
            client.assert_not_called()
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["new_orders"][0]["symbol"], "AAPL")
        self.assertEqual(result["new_orders"][0]["limit_price"], "10.25")

    def test_production_is_also_offline_by_default(self):
        settings = cli.Settings(environment="production", account_id="live-account")
        with patch.object(cli, "api_client") as client:
            result = cli.execute_command(cli.parser().parse_args(self.order_args), settings)
            client.assert_not_called()
        self.assertTrue(result["dry_run"])

    def test_live_submission_requires_both_guards_before_client_creation(self):
        for enabled, confirmed in ((False, False), (True, False), (False, True)):
            with self.subTest(enabled=enabled, confirmed=confirmed):
                settings = cli.Settings(environment="production", account_id="live-account", enable_live_trading=enabled)
                argv = self.order_args + ["--submit"] + (["--confirm-live-order"] if confirmed else [])
                with patch.object(cli, "api_client") as client:
                    with self.assertRaisesRegex(ValueError, "Live order blocked"):
                        cli.execute_command(cli.parser().parse_args(argv), settings)
                    client.assert_not_called()

    def test_live_cli_cannot_bypass_backend_confirmations(self):
        settings = cli.Settings(environment="production", enable_live_trading=True)
        args = cli.parser().parse_args(self.order_args + ["--submit", "--confirm-live-order"])
        with self.assertRaisesRegex(ValueError, "legacy CLI"):
            cli.check_submission(settings, args)

    def test_sandbox_rejects_live_confirmation(self):
        args = cli.parser().parse_args(self.order_args + ["--submit", "--confirm-live-order"])
        with self.assertRaises(ValueError):
            cli.check_submission(self.settings, args)

    def test_invalid_prices(self):
        for value in ("0", "-1", "NaN", "Infinity", "-Infinity", "abc"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                cli.positive_price(value)

    def test_invalid_quantities(self):
        for value in ("0", "-1", "1.5", "abc"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                cli.positive_int(value)

    def test_bar_count_range(self):
        self.assertEqual(cli.bar_count("1200"), 1200)
        with self.assertRaises(argparse.ArgumentTypeError):
            cli.bar_count("1201")

    def test_explicit_account_required(self):
        args = cli.parser().parse_args(["balance"])
        with patch.object(cli, "api_client") as client:
            with self.assertRaises(ValueError):
                cli.execute_command(args, cli.Settings())
            client.assert_not_called()

    def test_http_error_redacts_credentials(self):
        response = Mock(status_code=403, text="test-app-key test-app-secret forbidden")
        with self.assertRaises(RuntimeError) as error:
            cli.response_json(response, self.settings)
        self.assertIn("entitlements", str(error.exception))
        self.assertNotIn("test-app-secret", str(error.exception))
        self.assertNotIn("test-app-key", str(error.exception))

    def test_main_returns_nonzero_on_failure(self):
        with patch.object(cli.Settings, "from_env", return_value=cli.Settings()):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["accounts"]), 1)

    def test_main_status_does_not_print_secrets(self):
        output = io.StringIO()
        with patch.object(cli.Settings, "from_env", return_value=self.settings):
            with redirect_stdout(output):
                self.assertEqual(cli.main(["status"]), 0)
        self.assertNotIn("test-app-secret", output.getvalue())

    def test_sdk_endpoint_override_and_transport_safety(self):
        from webull.trade.request.v2.get_account_list_request import GetAccountListRequest

        for environment, endpoint in cli.ENDPOINTS.items():
            with self.subTest(environment=environment):
                settings = cli.Settings(environment=environment, app_key="test-key", app_secret="test-secret")
                with redirect_stderr(io.StringIO()):
                    client = cli.api_client(settings)
                self.assertEqual(client._resolve_endpoint(GetAccountListRequest()), endpoint)
                self.assertTrue(client.get_verify())
                self.assertFalse(client._auto_retry)
                self.assertIn(environment, client.get_token_dir())
                self.assertNotIn("test-key", client.get_token_dir())

    def test_official_sdk_requests_without_network(self):
        # Real SDK request construction, with authentication and transport mocked.
        cases = [
            (["accounts"], "/trading/accounts/list", "GET"),
            (["balance"], "/trading/assets/balances/get", "GET"),
            (["positions"], "/trading/assets/positions/list", "GET"),
            (["orders", "--pagination-key", "next-page"], "/trading/orders/open-orders/list", "GET"),
            (["quotes", "AAPL", "MSFT"], "/market-data/stocks/snapshots/list", "GET"),
            (["bars", "AAPL", "--count", "5"], "/market-data/stocks/bars/list", "POST"),
            (self.order_args + ["--submit", "--client-order-id", "test-order-1"], "/trading/orders/place", "POST"),
        ]
        for argv, expected_path, expected_method in cases:
            with self.subTest(argv=argv):
                response = Mock(status_code=200)
                response.json.return_value = {"ok": True}
                with patch("webull.core.http.initializer.client_initializer.ClientInitializer.initializer"):
                    with patch.object(cli.ApiClient, "get_response", return_value=response) as transport:
                        with redirect_stderr(io.StringIO()):
                            cli.execute_command(cli.parser().parse_args(argv), self.settings)
                request = transport.call_args.args[0]
                self.assertEqual(request.get_action_name(), expected_path)
                self.assertEqual(request.get_method(), expected_method)
                self.assertEqual(request.get_protocol_type(), "https")
                if argv[0] == "order":
                    self.assertEqual(request.get_body_params()["account_id"], "sandbox-account")
                    self.assertEqual(request.get_body_params()["new_orders"][0]["client_order_id"], "test-order-1")
                if argv[0] == "orders":
                    self.assertEqual(request.get_query_params()["pagination_key"], "next-page")


if __name__ == "__main__":
    unittest.main()
