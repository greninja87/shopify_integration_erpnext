"""
test_shopify_api.py — tests for the Admin API client's rate limiting, retry
behaviour and error mapping.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_shopify_api -v

`requests` is faked in sys.modules (shopify_api imports it lazily inside get(),
precisely so this is possible) and time.sleep is stubbed, so the suite stays
instant while still asserting exactly how long the client *would* have waited.
"""

import sys
import types
import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.utils import shopify_api  # noqa: E402
from shopify_integration.utils.shopify_api import ShopifyAPIError  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class FakeRequests:
    """Serves a scripted list of responses and records every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {},
                           "params": params, "timeout": timeout})
        if not self._responses:
            raise AssertionError("FakeRequests ran out of scripted responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class ApiTestCase(unittest.TestCase):
    """Installs the requests fake and neutralises sleeping."""

    def setUp(self):
        frappe_stub.reset()
        self.sleeps = []
        self._real_sleep = shopify_api.time.sleep
        self._real_monotonic = shopify_api.time.monotonic
        self._clock = [1000.0]

        shopify_api.time.sleep = self._record_sleep
        shopify_api.time.monotonic = lambda: self._clock[0]
        # Start with the pacer "cold" so throttling does not colour assertions
        # about retry sleeps.
        shopify_api._last_request_at = 0.0

    def tearDown(self):
        shopify_api.time.sleep = self._real_sleep
        shopify_api.time.monotonic = self._real_monotonic
        sys.modules.pop("requests", None)
        shopify_api._last_request_at = 0.0

    def _record_sleep(self, seconds):
        self.sleeps.append(seconds)
        self._clock[0] += seconds

    def _install_requests(self, responses):
        fake = FakeRequests(responses)
        module = types.ModuleType("requests")
        module.get = fake.get
        sys.modules["requests"] = module
        return fake

    @property
    def settings(self):
        return frappe_stub.FakeSettings()


class TestGet(ApiTestCase):
    def test_successful_request(self):
        self._install_requests([FakeResponse(200, {"transactions": [{"id": 1}]})])
        body = shopify_api.get(self.settings, "orders/6428/transactions.json")
        self.assertEqual(body, {"transactions": [{"id": 1}]})

    def test_url_and_auth_header(self):
        fake = self._install_requests([FakeResponse(200, {})])
        shopify_api.get(self.settings, "orders/6428/transactions.json")

        call = fake.calls[0]
        self.assertEqual(
            call["url"],
            "https://notdrones.myshopify.com/admin/api/2026-01/orders/6428/transactions.json",
        )
        self.assertEqual(call["headers"]["X-Shopify-Access-Token"], "shpat_x")
        self.assertEqual(call["timeout"], shopify_api._TIMEOUT)

    def test_default_api_version_when_unset(self):
        settings = frappe_stub.FakeSettings()
        settings._values["api_version"] = ""
        self.assertEqual(shopify_api.get_api_version(settings), shopify_api.DEFAULT_API_VERSION)

    def test_missing_token_raises_without_calling_out(self):
        fake = self._install_requests([FakeResponse(200, {})])
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get(frappe_stub.FakeSettings(token=""), "orders/1/transactions.json")
        self.assertEqual(fake.calls, [], "must not hit the network without a token")

    def test_missing_shop_domain_raises(self):
        self._install_requests([FakeResponse(200, {})])
        settings = frappe_stub.FakeSettings()
        settings._values["shop_domain"] = ""
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get(settings, "orders/1/transactions.json")


class TestRateLimiting(ApiTestCase):
    def test_paces_consecutive_requests_to_two_per_second(self):
        self._install_requests([FakeResponse(200, {}), FakeResponse(200, {})])

        shopify_api.get(self.settings, "orders/1/transactions.json")
        self.sleeps.clear()
        shopify_api.get(self.settings, "orders/2/transactions.json")

        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], shopify_api._MIN_INTERVAL, places=6)
        self.assertAlmostEqual(shopify_api._MIN_INTERVAL, 0.5, places=6)  # == 2 req/sec

    def test_does_not_sleep_when_enough_time_has_passed(self):
        self._install_requests([FakeResponse(200, {}), FakeResponse(200, {})])

        shopify_api.get(self.settings, "orders/1/transactions.json")
        self._clock[0] += 5.0          # a long gap between calls
        self.sleeps.clear()
        shopify_api.get(self.settings, "orders/2/transactions.json")

        self.assertEqual(self.sleeps, [])


class TestRetry(ApiTestCase):
    def test_429_is_retried_and_honours_retry_after(self):
        self._install_requests([
            FakeResponse(429, headers={"Retry-After": "2.0"}),
            FakeResponse(200, {"transactions": [{"id": 9}]}),
        ])

        body = shopify_api.get(self.settings, "orders/1/transactions.json")

        self.assertEqual(body["transactions"][0]["id"], 9)
        self.assertIn(2.0, self.sleeps)

    def test_429_without_retry_after_uses_a_sane_default(self):
        self._install_requests([FakeResponse(429), FakeResponse(200, {})])
        shopify_api.get(self.settings, "orders/1/transactions.json")
        self.assertIn(shopify_api._MIN_INTERVAL * 2, self.sleeps)

    def test_429_retry_after_is_capped(self):
        """A hostile or broken header must not park a worker for hours."""
        self._install_requests([
            FakeResponse(429, headers={"Retry-After": "99999"}),
            FakeResponse(200, {}),
        ])
        shopify_api.get(self.settings, "orders/1/transactions.json")
        self.assertLessEqual(max(self.sleeps), shopify_api._MAX_SLEEP)

    def test_429_garbage_retry_after_falls_back(self):
        self._install_requests([
            FakeResponse(429, headers={"Retry-After": "soon"}),
            FakeResponse(200, {}),
        ])
        shopify_api.get(self.settings, "orders/1/transactions.json")
        self.assertIn(shopify_api._MIN_INTERVAL * 2, self.sleeps)

    def test_persistent_429_raises_after_max_attempts(self):
        fake = self._install_requests([FakeResponse(429)] * shopify_api._MAX_ATTEMPTS)

        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_api.get(self.settings, "orders/1/transactions.json")

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(len(fake.calls), shopify_api._MAX_ATTEMPTS)

    def test_500_is_retried_with_backoff(self):
        self._install_requests([FakeResponse(500), FakeResponse(200, {"transactions": []})])
        body = shopify_api.get(self.settings, "orders/1/transactions.json")
        self.assertEqual(body, {"transactions": []})
        self.assertIn(shopify_api._BACKOFF_BASE, self.sleeps)

    def test_transport_error_is_retried(self):
        self._install_requests([OSError("connection reset"), FakeResponse(200, {})])
        self.assertEqual(shopify_api.get(self.settings, "orders/1/transactions.json"), {})

    def test_persistent_transport_error_raises(self):
        self._install_requests([OSError("down")] * shopify_api._MAX_ATTEMPTS)
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get(self.settings, "orders/1/transactions.json")


class TestErrorMapping(ApiTestCase):
    def test_401_is_not_retried(self):
        fake = self._install_requests([FakeResponse(401)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_api.get(self.settings, "orders/1/transactions.json")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(len(fake.calls), 1, "auth failures must not be retried")
        self.assertIn("read_orders", str(ctx.exception))

    def test_403_is_not_retried(self):
        fake = self._install_requests([FakeResponse(403)])
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get(self.settings, "orders/1/transactions.json")
        self.assertEqual(len(fake.calls), 1)

    def test_404_is_not_retried(self):
        fake = self._install_requests([FakeResponse(404)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_api.get(self.settings, "orders/1/transactions.json")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(len(fake.calls), 1)

    def test_other_4xx_is_not_retried(self):
        fake = self._install_requests([FakeResponse(422, text="unprocessable")])
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get(self.settings, "orders/1/transactions.json")
        self.assertEqual(len(fake.calls), 1)

    def test_unparseable_json_raises(self):
        self._install_requests([FakeResponse(200, json_body=ValueError("no json"))])
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get(self.settings, "orders/1/transactions.json")


class TestGetOrderTransactions(ApiTestCase):
    def test_returns_the_transactions_list(self):
        self._install_requests([FakeResponse(200, {"transactions": [{"id": 1}, {"id": 2}]})])
        txns = shopify_api.get_order_transactions(self.settings, 6428)
        self.assertEqual([t["id"] for t in txns], [1, 2])

    def test_missing_key_returns_empty_list(self):
        self._install_requests([FakeResponse(200, {})])
        self.assertEqual(shopify_api.get_order_transactions(self.settings, 6428), [])

    def test_non_list_payload_returns_empty_list(self):
        self._install_requests([FakeResponse(200, {"transactions": None})])
        self.assertEqual(shopify_api.get_order_transactions(self.settings, 6428), [])

    def test_blank_order_id_raises_without_calling_out(self):
        fake = self._install_requests([FakeResponse(200, {})])
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get_order_transactions(self.settings, "")
        self.assertEqual(fake.calls, [])

    def test_has_admin_api_credentials(self):
        self.assertTrue(shopify_api.has_admin_api_credentials(frappe_stub.FakeSettings()))
        self.assertFalse(shopify_api.has_admin_api_credentials(frappe_stub.FakeSettings(token="")))
        self.assertFalse(shopify_api.has_admin_api_credentials(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
