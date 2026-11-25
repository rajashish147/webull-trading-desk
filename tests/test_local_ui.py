from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from fake_broker import FakeBroker
from trading.app import create_app
from trading.config import AppConfig
from webull_cli import Settings


class LocalUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            broker=Settings(app_key="fixture-webull-key", app_secret="fixture-webull-secret", account_id="fake-cash-account"),
            api_key="fixture-backend-key-" + "x" * 40,
            database=Path(self.temp.name) / "ui.sqlite3", reconcile_interval=0,
        )
        self.broker = FakeBroker()
        self.client = TestClient(create_app(self.config, self.broker), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def session(self):
        response = self.client.get("/ui/session", headers={"X-Local-UI": "1"})
        self.assertEqual(response.status_code, 200)
        self.client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return response

    def test_dashboard_and_assets_are_served_locally(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Webull Desk", response.text)
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.client.get("/static/styles.css").status_code, 200)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)

    def test_api_console_is_separate_local_page_with_csp(self):
        response = self.client.get("/api-console")
        self.assertEqual(response.status_code, 200)
        self.assertIn("API Console", response.text)
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        for path in ("/static/console.mjs", "/static/console-core.mjs", "/static/console.css"):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIn("API testing console", self.client.get("/").text)
        self.assertEqual(self.broker.place_calls, 0)

    def test_console_rejects_remote_peers_and_rebinding_hosts(self):
        self.assertEqual(self.client.get("/api-console", headers={"Host": "untrusted.example"}).status_code, 403)
        remote = TestClient(self.client.app, base_url="http://127.0.0.1:8000", client=("192.0.2.1", 50000))
        self.assertEqual(remote.get("/api-console").status_code, 403)

    def test_console_page_and_scripts_do_not_include_env_credentials(self):
        for path in ("/api-console", "/static/console.mjs", "/static/console-core.mjs"):
            response = self.client.get(path)
            for secret in (self.config.api_key, self.config.broker.app_key, self.config.broker.app_secret):
                self.assertNotIn(secret, response.text)

    def test_console_session_can_execute_existing_endpoints_without_api_key(self):
        self.client.get("/api-console")
        self.session()
        self.assertEqual(self.client.get("/v1/system").status_code, 200)
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)
        self.assertEqual(self.client.post("/v1/broker/connect").status_code, 200)
        self.assertEqual(self.client.get("/v1/portfolio").status_code, 200)
        self.assertEqual(self.broker.place_calls, 0)

    def test_browser_session_needs_no_api_key(self):
        response = self.session()
        self.assertEqual(set(response.json()), {"csrf_token", "configuration_source"})
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertEqual(self.client.get("/v1/system").status_code, 200)
        self.assertEqual(self.client.post("/v1/broker/connect").status_code, 200)
        self.assertEqual(self.broker.place_calls, 0)

    def test_browser_session_cannot_bypass_kill_switch(self):
        self.session()
        self.client.post("/v1/broker/connect")
        draft = self.client.post("/v1/orders", headers={"Idempotency-Key": "ui-fixture"}, json={"symbol": "AAPL", "side": "BUY", "quantity": 1, "limit_price": "100"}).json()
        self.assertEqual(self.client.post(f"/v1/orders/{draft['id']}/submit").status_code, 409)
        self.assertEqual(self.broker.place_calls, 0)

    def test_browser_csrf_token_is_required(self):
        self.session()
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual(self.client.get("/v1/system").status_code, 401)
        self.assertEqual(self.client.put("/v1/risk/kill-switch", json={"enabled": False, "reason": "Should not work"}).status_code, 401)

    def test_bad_csrf_token_is_rejected(self):
        self.session()
        self.client.headers["X-CSRF-Token"] = "wrong"
        self.assertEqual(self.client.get("/v1/system").status_code, 401)

    def test_bootstrap_requires_custom_local_header(self):
        self.assertEqual(self.client.get("/ui/session").status_code, 403)

    def test_cross_origin_bootstrap_and_session_requests_are_rejected(self):
        self.assertEqual(self.client.get("/ui/session", headers={"X-Local-UI": "1", "Origin": "https://untrusted.example"}).status_code, 403)
        self.session()
        for headers in ({"Origin": "https://untrusted.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.client.get("/v1/system", headers=headers).status_code, 401)

    def test_rebinding_host_is_rejected(self):
        self.assertEqual(self.client.get("/", headers={"Host": "untrusted.example:8000"}).status_code, 403)
        self.assertEqual(self.client.get("/ui/session", headers={"X-Local-UI": "1", "Host": "untrusted.example:8000"}).status_code, 403)

    def test_remote_peer_cannot_bootstrap_local_session(self):
        remote = TestClient(self.client.app, base_url="http://127.0.0.1:8000", client=("192.0.2.1", 50000))
        self.assertEqual(remote.get("/ui/session", headers={"X-Local-UI": "1"}).status_code, 403)

    def test_env_and_secrets_are_never_served(self):
        self.session()
        for path in ("/", "/static/app.js", "/ui/session", "/v1/system"):
            response = self.client.get(path, headers={"X-Local-UI": "1"})
            for secret in (self.config.api_key, self.config.broker.app_key, self.config.broker.app_secret):
                self.assertNotIn(secret, response.text)
        for path in ("/.env", "/static/.env", "/static/%2e%2e/%2e%2e/.env"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_direct_api_key_authentication_still_works(self):
        self.assertEqual(self.client.get("/v1/system", headers={"X-API-Key": self.config.api_key}).status_code, 200)

    def test_backend_reads_env_file_instead_of_process_overrides(self):
        values = {
            "WEBULL_ENV": "sandbox", "WEBULL_APP_KEY": "file-key", "WEBULL_APP_SECRET": "file-secret",
            "WEBULL_ACCOUNT_ID": "file-account", "WEBULL_ENABLE_LIVE_TRADING": "false",
            "TRADING_API_KEY": "file-api-key-" + "y" * 40, "TRADING_MAX_ORDER_QUANTITY": "7",
        }
        with patch.dict(os.environ, {"WEBULL_ENV": "production", "WEBULL_ACCOUNT_ID": "stale-shell-account", "TRADING_MAX_ORDER_QUANTITY": "999"}):
            with patch("trading.config.dotenv_values", return_value=values):
                config = AppConfig.from_env()
            self.assertEqual(config.broker.environment, "sandbox")
            self.assertEqual(config.broker.account_id, "file-account")
            self.assertEqual(config.limits.max_order_quantity, 7)
            self.assertEqual(os.environ["WEBULL_ENV"], "production")


if __name__ == "__main__":
    unittest.main()
