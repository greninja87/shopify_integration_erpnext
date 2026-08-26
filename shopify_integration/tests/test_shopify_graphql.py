"""
test_shopify_graphql.py — tests for the GraphQL client.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_shopify_graphql -v

The point of these tests is the two ways GraphQL fails at HTTP 200:

  * query-level `errors` — throttling arrives here as extensions.code THROTTLED
  * mutation `userErrors` — request accepted, nothing happened

A client that trusts the status code reads both as success.  The second is the
one that corrupts data: read it as success and a Delivery Note gets marked
fulfilled when Shopify rejected the fulfillment.
"""

import sys
import types
import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.utils import shopify_api, shopify_graphql  # noqa: E402
from shopify_integration.utils.shopify_api import ShopifyAPIError  # noqa: E402
from shopify_integration.utils.shopify_graphql import ShopifyUserError  # noqa: E402


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


class GraphQLTestCase(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()
        self.calls = []
        self.sleeps = []
        self._responses = []

        self._real_sleep = shopify_api.time.sleep
        self._real_monotonic = shopify_api.time.monotonic
        self._clock = [1000.0]

        shopify_api.time.sleep = self._record_sleep
        shopify_api.time.monotonic = lambda: self._clock[0]
        shopify_api._last_request_at = 0.0

        module = types.ModuleType("requests")
        module.post = self._post
        module.get = self._post
        sys.modules["requests"] = module

    def tearDown(self):
        shopify_api.time.sleep = self._real_sleep
        shopify_api.time.monotonic = self._real_monotonic
        shopify_api._last_request_at = 0.0
        sys.modules.pop("requests", None)

    def _record_sleep(self, seconds):
        self.sleeps.append(seconds)
        self._clock[0] += seconds

    def _post(self, url, headers=None, data=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": headers or {},
                           "data": data, "timeout": timeout})
        if not self._responses:
            raise AssertionError("ran out of scripted responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def script(self, *responses):
        self._responses = list(responses)

    @property
    def settings(self):
        return frappe_stub.FakeSettings()


class TestExecute(GraphQLTestCase):
    def test_returns_data(self):
        self.script(FakeResponse(200, {"data": {"order": {"id": "gid://shopify/Order/1"}}}))
        data = shopify_graphql.execute(self.settings, "query { x }")
        self.assertEqual(data["order"]["id"], "gid://shopify/Order/1")

    def test_posts_to_graphql_endpoint(self):
        self.script(FakeResponse(200, {"data": {}}))
        shopify_graphql.execute(self.settings, "query { x }")

        call = self.calls[0]
        self.assertEqual(
            call["url"],
            "https://notdrones.myshopify.com/admin/api/2026-01/graphql.json",
        )
        self.assertEqual(call["headers"]["X-Shopify-Access-Token"], "shpat_x")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")

    def test_sends_query_and_variables(self):
        import json
        self.script(FakeResponse(200, {"data": {}}))
        shopify_graphql.execute(self.settings, "query Q { x }", {"id": "gid://x/1"})

        body = json.loads(self.calls[0]["data"])
        self.assertEqual(body["query"], "query Q { x }")
        self.assertEqual(body["variables"], {"id": "gid://x/1"})

    def test_missing_token_raises_without_calling_out(self):
        self.script(FakeResponse(200, {"data": {}}))
        with self.assertRaises(ShopifyAPIError):
            shopify_graphql.execute(frappe_stub.FakeSettings(token=""), "query { x }")
        self.assertEqual(self.calls, [])

    def test_shares_the_rest_client_pacer(self):
        """
        REST and GraphQL must draw on ONE rate budget. Two independent pacers
        would each think they own the whole limit and together double the rate.
        """
        self.script(FakeResponse(200, {"data": {}}), FakeResponse(200, {"data": {}}))
        shopify_graphql.execute(self.settings, "query { x }")
        self.sleeps.clear()
        shopify_graphql.execute(self.settings, "query { x }")

        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], shopify_api._MIN_INTERVAL, places=6)

    def test_null_data_raises(self):
        self.script(FakeResponse(200, {"data": None}))
        with self.assertRaises(ShopifyAPIError):
            shopify_graphql.execute(self.settings, "query { x }")

    def test_unparseable_json_raises(self):
        self.script(FakeResponse(200, json_body=ValueError("nope")))
        with self.assertRaises(ShopifyAPIError):
            shopify_graphql.execute(self.settings, "query { x }")


class TestHttp200Failures(GraphQLTestCase):
    """The failures a status-code-only client would miss."""

    def test_query_errors_raise_despite_http_200(self):
        self.script(FakeResponse(200, {
            "errors": [{"message": "Field 'nope' doesn't exist"}],
        }))
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.execute(self.settings, "query { nope }")
        self.assertIn("nope", str(ctx.exception))

    def test_throttled_is_retried(self):
        self.script(
            FakeResponse(200, {
                "errors": [{"message": "Throttled",
                            "extensions": {"code": "THROTTLED"}}],
                "extensions": {"cost": {
                    "requestedQueryCost": 100,
                    "throttleStatus": {"currentlyAvailable": 0, "restoreRate": 50},
                }},
            }),
            FakeResponse(200, {"data": {"ok": True}}),
        )
        data = shopify_graphql.execute(self.settings, "query { x }")

        self.assertEqual(data, {"ok": True})
        # (100 - 0) / 50 + 0.25 == 2.25
        self.assertIn(2.25, self.sleeps)

    def test_throttled_without_cost_info_uses_default_wait(self):
        self.script(
            FakeResponse(200, {"errors": [{"extensions": {"code": "THROTTLED"}}]}),
            FakeResponse(200, {"data": {}}),
        )
        shopify_graphql.execute(self.settings, "query { x }")
        self.assertIn(shopify_graphql._DEFAULT_THROTTLE_WAIT, self.sleeps)

    def test_throttle_wait_is_capped(self):
        self.script(
            FakeResponse(200, {
                "errors": [{"extensions": {"code": "THROTTLED"}}],
                "extensions": {"cost": {
                    "requestedQueryCost": 1000000,
                    "throttleStatus": {"currentlyAvailable": 0, "restoreRate": 1},
                }},
            }),
            FakeResponse(200, {"data": {}}),
        )
        shopify_graphql.execute(self.settings, "query { x }")
        self.assertLessEqual(max(self.sleeps), shopify_api._MAX_SLEEP)

    def test_persistent_throttling_eventually_raises(self):
        throttled = lambda: FakeResponse(200, {  # noqa: E731
            "errors": [{"extensions": {"code": "THROTTLED"}}]
        })
        self.script(*[throttled() for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError):
            shopify_graphql.execute(self.settings, "query { x }")
        self.assertEqual(len(self.calls), shopify_api._MAX_ATTEMPTS)

    def test_non_throttle_errors_are_not_retried(self):
        self.script(FakeResponse(200, {"errors": [{"message": "bad query"}]}))
        with self.assertRaises(ShopifyAPIError):
            shopify_graphql.execute(self.settings, "query { x }")
        self.assertEqual(len(self.calls), 1)


class TestHttpErrors(GraphQLTestCase):
    def test_403_mentions_the_fulfillment_scopes(self):
        """The most likely first-run failure: a read_orders-only token."""
        self.script(FakeResponse(403))
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.execute(self.settings, "mutation { x }")

        message = str(ctx.exception)
        self.assertIn("write_merchant_managed_fulfillment_orders", message)
        self.assertIn("read_orders", message)
        self.assertEqual(len(self.calls), 1, "auth failures must not be retried")

    def test_500_is_retried(self):
        self.script(FakeResponse(500), FakeResponse(200, {"data": {"ok": 1}}))
        self.assertEqual(shopify_graphql.execute(self.settings, "query { x }"), {"ok": 1})

    def test_429_is_retried(self):
        self.script(FakeResponse(429), FakeResponse(200, {"data": {}}))
        shopify_graphql.execute(self.settings, "query { x }")
        self.assertEqual(len(self.calls), 2)

    def test_transport_error_is_retried(self):
        self.script(OSError("reset"), FakeResponse(200, {"data": {}}))
        self.assertEqual(shopify_graphql.execute(self.settings, "query { x }"), {})


class TestCheckUserErrors(unittest.TestCase):
    """
    The quiet failure: HTTP 200, no `errors`, and the mutation did nothing.
    Treating this as success is what would mark a Delivery Note fulfilled when
    Shopify refused.
    """

    def setUp(self):
        frappe_stub.reset()

    def test_clean_payload_is_returned(self):
        data = {"fulfillmentCreate": {
            "fulfillment": {"id": "gid://shopify/Fulfillment/1"},
            "userErrors": [],
        }}
        payload = shopify_graphql.check_user_errors(data, "fulfillmentCreate")
        self.assertEqual(payload["fulfillment"]["id"], "gid://shopify/Fulfillment/1")

    def test_user_errors_raise(self):
        data = {"fulfillmentCreate": {
            "fulfillment": None,
            "userErrors": [
                {"field": ["fulfillment", "lineItemsByFulfillmentOrder"],
                 "message": "Line items are already fulfilled"}
            ],
        }}
        with self.assertRaises(ShopifyUserError) as ctx:
            shopify_graphql.check_user_errors(data, "fulfillmentCreate", context="DN-001")

        self.assertIn("already fulfilled", str(ctx.exception))
        self.assertIn("DN-001", str(ctx.exception))
        self.assertEqual(len(ctx.exception.user_errors), 1)

    def test_user_error_without_field(self):
        data = {"fulfillmentCancel": {"userErrors": [{"message": "Cannot cancel"}]}}
        with self.assertRaises(ShopifyUserError) as ctx:
            shopify_graphql.check_user_errors(data, "fulfillmentCancel")
        self.assertIn("Cannot cancel", str(ctx.exception))

    def test_missing_mutation_key_raises(self):
        with self.assertRaises(ShopifyAPIError):
            shopify_graphql.check_user_errors({"other": {}}, "fulfillmentCreate")

    def test_user_error_is_a_shopify_api_error_subclass(self):
        """So a caller catching ShopifyAPIError catches userErrors too."""
        self.assertTrue(issubclass(ShopifyUserError, ShopifyAPIError))


class TestGid(unittest.TestCase):
    def test_builds_a_gid(self):
        self.assertEqual(shopify_graphql.gid("Order", 6428), "gid://shopify/Order/6428")

    def test_passes_existing_gid_through(self):
        existing = "gid://shopify/Order/6428"
        self.assertEqual(shopify_graphql.gid("Order", existing), existing)

    def test_handles_string_input(self):
        self.assertEqual(shopify_graphql.gid("Order", " 6428 "), "gid://shopify/Order/6428")

    def test_numeric_id_extracts_the_tail(self):
        self.assertEqual(shopify_graphql.numeric_id("gid://shopify/Fulfillment/99"), "99")

    def test_numeric_id_passes_plain_values_through(self):
        self.assertEqual(shopify_graphql.numeric_id("99"), "99")


if __name__ == "__main__":
    unittest.main(verbosity=2)
