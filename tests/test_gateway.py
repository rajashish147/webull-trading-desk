import unittest
from unittest.mock import Mock

from trading.broker import WebullGateway
from trading.models import BrokerError, DomainError
from webull_cli import Settings


class GatewayTests(unittest.TestCase):
    def gateway(self, environment="sandbox"):
        gateway = WebullGateway(Settings(environment=environment, account_id="fixture"))
        gateway.connected = True
        gateway._trade = Mock()
        return gateway

    def test_not_connected_fails_without_transport(self):
        with self.assertRaises(DomainError):
            WebullGateway(Settings(account_id="fixture")).balance()

    def test_production_place_replace_cancel_are_blocked(self):
        gateway = self.gateway("production")
        for operation in (lambda: gateway.place({}), lambda: gateway.replace("test", 1, "10"), lambda: gateway.cancel("test")):
            with self.assertRaises(DomainError) as error:
                operation()
            self.assertIn(error.exception.code, {"live_config_disabled", "live_confirmation_required"})
        gateway._trade.order_v3.place_order.assert_not_called()
        gateway._trade.order_v3.replace_order.assert_not_called()
        gateway._trade.order_v3.cancel_order.assert_not_called()

    def test_http_400_is_definitive_rejection(self):
        gateway = self.gateway()
        with self.assertRaises(BrokerError) as error:
            gateway._request("place", lambda: Mock(status_code=400))
        self.assertTrue(error.exception.definitive)

    def test_http_500_and_408_are_ambiguous(self):
        for status in (500, 408):
            with self.assertRaises(BrokerError) as error:
                self.gateway()._request("place", lambda status=status: Mock(status_code=status))
            self.assertFalse(error.exception.definitive)

    def test_transport_timeout_is_ambiguous(self):
        with self.assertRaises(BrokerError) as error:
            self.gateway()._request("place", Mock(side_effect=TimeoutError("not exposed")))
        self.assertFalse(error.exception.definitive)
        self.assertNotIn("not exposed", str(error.exception))

    def test_business_error_is_rejected(self):
        response = Mock(status_code=200)
        response.json.return_value = {"code": "REJECTED"}
        with self.assertRaises(BrokerError) as error:
            self.gateway()._request("place", lambda: response)
        self.assertTrue(error.exception.definitive)

    def test_sandbox_endpoint_quota(self):
        gateway = self.gateway()
        for _ in range(30):
            gateway._rate_limit("quotes")
        with self.assertRaises(DomainError) as error:
            gateway._rate_limit("quotes")
        self.assertEqual(error.exception.status, 429)
        gateway._rate_limit("bars")  # Independent endpoint quota.

    def test_complete_open_order_pagination(self):
        gateway = self.gateway()
        gateway._request = Mock(side_effect=[{"data": [{"id": 1}], "pagination_key": "next"}, {"data": [{"id": 2}]}])
        self.assertEqual(gateway.open_orders(), [{"id": 1}, {"id": 2}])

    def test_repeated_pagination_fails_closed(self):
        gateway = self.gateway()
        gateway._request = Mock(return_value={"data": [], "pagination_key": "same"})
        with self.assertRaises(BrokerError):
            gateway.open_orders()

    def test_missing_pagination_data_is_not_assumed_empty(self):
        gateway = self.gateway()
        gateway._request = Mock(return_value={"unexpected": []})
        with self.assertRaises(BrokerError):
            gateway.open_orders()


if __name__ == "__main__":
    unittest.main()
