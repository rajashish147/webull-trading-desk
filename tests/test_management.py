from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dotenv import dotenv_values

import manage
from trading.config import AppConfig
from trading.database import Database
from webull_cli import Settings


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig(broker=Settings(account_id="fixture"), api_key="x" * 48,
                                database=self.root / "active.sqlite3", reconcile_interval=0)
        self.db = Database(self.config.database, self.config.identity)
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_is_consistent_and_keeps_source(self):
        destination = self.root / "backup.sqlite3"
        with patch("sys.argv", ["manage.py", "backup", "--destination", str(destination)]):
            with patch.object(manage.AppConfig, "from_env", return_value=self.config), redirect_stdout(io.StringIO()):
                manage.main()
        backup = Database(destination, self.config.identity)
        self.assertEqual(backup.state("halt"), self.db.state("halt"))
        self.assertTrue(self.config.database.exists())

    def test_backup_refuses_overwrite(self):
        for destination in (self.config.database, self.root / "existing.sqlite3"):
            destination.touch(exist_ok=True)
            with patch("sys.argv", ["manage.py", "backup", "--destination", str(destination)]):
                with patch.object(manage.AppConfig, "from_env", return_value=self.config), self.assertRaises(ValueError):
                    manage.main()

    def test_backup_requires_existing_database(self):
        config = replace(self.config, database=self.root / "missing.sqlite3")
        with patch("sys.argv", ["manage.py", "backup"]):
            with patch.object(manage.AppConfig, "from_env", return_value=config), self.assertRaises(ValueError):
                manage.main()

    def test_initialization_updates_only_account_and_generates_backend_key(self):
        path = self.root / ".env"
        path.write_text("WEBULL_ENV=sandbox\nWEBULL_APP_KEY=fixture-key\nWEBULL_APP_SECRET=fixture-secret\nWEBULL_ACCOUNT_ID=old\nWEBULL_ENABLE_LIVE_TRADING=false\n")
        response = Mock(status_code=200)
        response.json.return_value = [{"account_class": "INDIVIDUAL_CASH", "account_id": "chosen-cash"}]
        trade = Mock()
        trade.account_v2.get_account_list.return_value = response
        output = io.StringIO()
        with patch.object(manage, "ROOT", self.root), patch.object(manage.Settings, "from_file", return_value=Settings()):
            with patch("webull_cli.api_client"), patch("webull.trade.trade_client.TradeClient", return_value=trade), redirect_stdout(output):
                manage.initialize()
        values = dotenv_values(path)
        self.assertEqual(values["WEBULL_APP_KEY"], "fixture-key")
        self.assertEqual(values["WEBULL_APP_SECRET"], "fixture-secret")
        self.assertEqual(values["WEBULL_ACCOUNT_ID"], "chosen-cash")
        self.assertEqual(values["WEBULL_ENABLE_LIVE_TRADING"], "false")
        self.assertGreaterEqual(len(values["TRADING_API_KEY"]), 32)
        self.assertNotIn(values["TRADING_API_KEY"], output.getvalue())
        trade.order_v3.place_order.assert_not_called()

    def test_initialization_refuses_production_and_live_flag(self):
        for settings in (Settings(environment="production"), Settings(enable_live_trading=True)):
            with patch.object(manage.Settings, "from_file", return_value=settings), self.assertRaises(ValueError):
                manage.initialize()


if __name__ == "__main__":
    unittest.main()
