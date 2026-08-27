"""
test_client_credentials.py — tests for obtaining an Admin API access token.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_client_credentials -v

Two credential styles have to coexist:

  * a static token from a legacy custom app (non-expiring, paste once)
  * Client ID + Secret from a Dev Dashboard app, exchanged for a 24-hour token

The things worth pinning down are that the cheap "is this configured?" check
never makes an HTTP call, that a minted token is cached rather than re-minted
per request, and that a rejected token clears the cache instead of being
replayed for the rest of the day.
"""

import sys
import types
import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.utils import shopify_api  # noqa: E402
from shopify_integration.utils.shopify_api import ShopifyAPIError  # noqa: E402

MINTED = "shpat_minted_abc123"


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


class CredentialsTestCase(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()
        self.posts = []
        self.gets = []
        self._post_responses = []
        self._get_responses = []

        self._real_sleep = shopify_api.time.sleep
        self._real_monotonic = shopify_api.time.monotonic
        self._clock = [1000.0]
        shopify_api.time.sleep = self._sleep
        shopify_api.time.monotonic = lambda: self._clock[0]
        shopify_api._last_request_at = 0.0

        module = types.ModuleType("requests")
        module.post = self._post
        module.get = self._get
        sys.modules["requests"] = module

    def tearDown(self):
        shopify_api.time.sleep = self._real_sleep
        shopify_api.time.monotonic = self._real_monotonic
        shopify_api._last_request_at = 0.0
        sys.modules.pop("requests", None)

    def _sleep(self, seconds):
        self._clock[0] += seconds

    def _post(self, url, data=None, headers=None, timeout=None, **kwargs):
        self.posts.append({"url": url, "data": data, "headers": headers or {}})
        if not self._post_responses:
            raise AssertionError("ran out of scripted POST responses")
        nxt = self._post_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def _get(self, url, headers=None, params=None, timeout=None, **kwargs):
        self.gets.append({"url": url, "headers": headers or {}})
        if not self._get_responses:
            raise AssertionError("ran out of scripted GET responses")
        nxt = self._get_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def script_post(self, *responses):
        self._post_responses = list(responses)

    def script_get(self, *responses):
        self._get_responses = list(responses)

    def token_response(self, token=MINTED, expires_in=86399, scope="read_orders"):
        return FakeResponse(200, {
            "access_token": token, "scope": scope, "expires_in": expires_in,
        })

    def cc_settings(self, **kwargs):
        kwargs.setdefault("token", "")
        kwargs.setdefault("client_id", "cid_123")
        kwargs.setdefault("client_secret", "csec_456")
        return frappe_stub.FakeSettings(**kwargs)


# ── has_admin_api_credentials: must never call out ────────────────────────────

class TestHasCredentials(CredentialsTestCase):
    def test_static_token_counts(self):
        self.assertTrue(shopify_api.has_admin_api_credentials(
            frappe_stub.FakeSettings(token="shpat_static")
        ))

    def test_client_credentials_count(self):
        self.assertTrue(shopify_api.has_admin_api_credentials(self.cc_settings()))

    def test_nothing_configured(self):
        self.assertFalse(shopify_api.has_admin_api_credentials(
            frappe_stub.FakeSettings(token="")
        ))

    def test_half_filled_pair_does_not_count(self):
        """Only one of the two is useless — better to read as 'not configured'."""
        self.assertFalse(shopify_api.has_admin_api_credentials(
            frappe_stub.FakeSettings(token="", client_id="cid_123", client_secret="")
        ))
        self.assertFalse(shopify_api.has_admin_api_credentials(
            frappe_stub.FakeSettings(token="", client_id="", client_secret="csec_456")
        ))

    def test_no_shop_domain(self):
        settings = self.cc_settings()
        settings._values["shop_domain"] = ""
        self.assertFalse(shopify_api.has_admin_api_credentials(settings))

    def test_none_settings(self):
        self.assertFalse(shopify_api.has_admin_api_credentials(None))

    def test_never_makes_a_request(self):
        """
        This runs once per order. If it minted a token it would add an HTTP
        round trip to every single order sync.
        """
        shopify_api.has_admin_api_credentials(self.cc_settings())
        self.assertEqual(self.posts, [], "must not mint a token just to check config")
        self.assertEqual(self.gets, [])


# ── Minting ───────────────────────────────────────────────────────────────────

class TestMinting(CredentialsTestCase):
    def test_static_token_short_circuits(self):
        token = shopify_api.get_admin_api_token(
            frappe_stub.FakeSettings(token="shpat_static")
        )
        self.assertEqual(token, "shpat_static")
        self.assertEqual(self.posts, [], "a static token needs no minting")

    def test_static_token_wins_over_client_credentials(self):
        settings = frappe_stub.FakeSettings(
            token="shpat_static", client_id="cid_123", client_secret="csec_456"
        )
        self.assertEqual(shopify_api.get_admin_api_token(settings), "shpat_static")
        self.assertEqual(self.posts, [])

    def test_mints_when_only_client_credentials(self):
        self.script_post(self.token_response())
        self.assertEqual(shopify_api.get_admin_api_token(self.cc_settings()), MINTED)
        self.assertEqual(len(self.posts), 1)

    def test_request_shape(self):
        self.script_post(self.token_response())
        shopify_api.get_admin_api_token(self.cc_settings())

        post = self.posts[0]
        self.assertEqual(post["url"], "https://notdrones.myshopify.com/admin/oauth/access_token")
        self.assertEqual(post["data"], {
            "grant_type": "client_credentials",
            "client_id": "cid_123",
            "client_secret": "csec_456",
        })

    def test_returns_blank_when_nothing_configured(self):
        """
        "" means "not configured" so a caller can tell a disabled feature from a
        broken one. It must not raise.
        """
        self.assertEqual(
            shopify_api.get_admin_api_token(frappe_stub.FakeSettings(token="")), ""
        )
        self.assertEqual(self.posts, [])

    def test_missing_shop_domain_raises(self):
        settings = self.cc_settings()
        settings._values["shop_domain"] = ""
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get_admin_api_token(settings)


# ── Caching ───────────────────────────────────────────────────────────────────

class TestCaching(CredentialsTestCase):
    def test_second_call_uses_the_cache(self):
        self.script_post(self.token_response())
        settings = self.cc_settings()

        first = shopify_api.get_admin_api_token(settings)
        second = shopify_api.get_admin_api_token(settings)

        self.assertEqual(first, MINTED)
        self.assertEqual(second, MINTED)
        self.assertEqual(len(self.posts), 1, "must not re-mint while cached")

    def test_ttl_leaves_a_refresh_margin(self):
        """Expiring exactly on the boundary would strand an in-flight request."""
        self.script_post(self.token_response(expires_in=86399))
        shopify_api.get_admin_api_token(self.cc_settings())

        ttl = list(frappe_stub.CACHE_TTL.values())[0]
        self.assertEqual(ttl, 86399 - shopify_api._TOKEN_REFRESH_MARGIN)
        self.assertLess(ttl, 86399)

    def test_short_expiry_still_gets_a_usable_ttl(self):
        self.script_post(self.token_response(expires_in=60))
        shopify_api.get_admin_api_token(self.cc_settings())
        self.assertGreaterEqual(list(frappe_stub.CACHE_TTL.values())[0], 60)

    def test_missing_expires_in_falls_back(self):
        self.script_post(FakeResponse(200, {"access_token": MINTED}))
        self.assertEqual(shopify_api.get_admin_api_token(self.cc_settings()), MINTED)
        self.assertGreater(list(frappe_stub.CACHE_TTL.values())[0], 0)

    def test_garbage_expires_in_falls_back(self):
        self.script_post(FakeResponse(200, {"access_token": MINTED, "expires_in": "soon"}))
        self.assertEqual(shopify_api.get_admin_api_token(self.cc_settings()), MINTED)

    def test_cache_is_per_store(self):
        """Two stores must never share a token."""
        self.script_post(
            self.token_response(token="token_store_a"),
            self.token_response(token="token_store_b"),
        )
        a = self.cc_settings(name="Store A", shop_domain="a.myshopify.com")
        b = self.cc_settings(name="Store B", shop_domain="b.myshopify.com")

        self.assertEqual(shopify_api.get_admin_api_token(a), "token_store_a")
        self.assertEqual(shopify_api.get_admin_api_token(b), "token_store_b")
        self.assertEqual(shopify_api.get_admin_api_token(a), "token_store_a")
        self.assertEqual(len(self.posts), 2)

    def test_bytes_from_cache_are_decoded(self):
        """Redis can hand back bytes."""
        settings = self.cc_settings()
        frappe_stub.CACHE[shopify_api._token_cache_key(settings)] = b"shpat_bytes"
        self.assertEqual(shopify_api.get_admin_api_token(settings), "shpat_bytes")

    def test_invalidate_clears_the_cache(self):
        self.script_post(self.token_response())
        settings = self.cc_settings()
        shopify_api.get_admin_api_token(settings)
        self.assertTrue(frappe_stub.CACHE)

        shopify_api.invalidate_cached_token(settings)
        self.assertEqual(frappe_stub.CACHE, {})

    def test_invalidate_is_safe_on_none(self):
        shopify_api.invalidate_cached_token(None)  # must not raise


# ── Rejection handling ────────────────────────────────────────────────────────

class TestRejection(CredentialsTestCase):
    def test_401_explains_the_organization_restriction(self):
        """
        The most likely real cause: pointing client credentials at a store in a
        different Shopify organization. The message has to say so.
        """
        self.script_post(FakeResponse(401))
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_api.get_admin_api_token(self.cc_settings())

        message = str(ctx.exception)
        self.assertIn("organization", message)
        self.assertIn("Client ID", message)

    def test_secret_never_appears_in_the_error(self):
        """A logged secret is a leaked secret."""
        self.script_post(FakeResponse(400, text="client_secret=csec_456 was rejected"))
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_api.get_admin_api_token(self.cc_settings())
        self.assertNotIn("csec_456", str(ctx.exception))

    def test_400_401_403_all_raise(self):
        for status in (400, 401, 403):
            frappe_stub.reset()
            self.script_post(FakeResponse(status))
            with self.assertRaises(ShopifyAPIError):
                shopify_api.get_admin_api_token(self.cc_settings())

    def test_500_raises(self):
        self.script_post(FakeResponse(500))
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get_admin_api_token(self.cc_settings())

    def test_no_token_in_response_raises(self):
        self.script_post(FakeResponse(200, {"scope": "read_orders"}))
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get_admin_api_token(self.cc_settings())

    def test_unparseable_response_raises(self):
        self.script_post(FakeResponse(200, json_body=ValueError("nope")))
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get_admin_api_token(self.cc_settings())

    def test_transport_error_raises(self):
        self.script_post(OSError("dns"))
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get_admin_api_token(self.cc_settings())

    def test_nothing_is_cached_on_failure(self):
        self.script_post(FakeResponse(401))
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get_admin_api_token(self.cc_settings())
        self.assertEqual(frappe_stub.CACHE, {})


# ── A rejected token must not be replayed all day ─────────────────────────────

class TestStaleTokenRecovery(CredentialsTestCase):
    def test_401_on_a_request_clears_the_cached_token(self):
        """
        Rotate the Client Secret and the cached token becomes worthless. Without
        clearing it, every call fails for up to 24 hours while we keep sending a
        token Shopify has stopped accepting.
        """
        settings = self.cc_settings()
        frappe_stub.CACHE[shopify_api._token_cache_key(settings)] = "shpat_stale"

        self.script_get(FakeResponse(401))
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get(settings, "orders/1/transactions.json")

        self.assertEqual(frappe_stub.CACHE, {}, "stale token must be evicted")

    def test_next_call_after_a_401_mints_fresh(self):
        settings = self.cc_settings()
        frappe_stub.CACHE[shopify_api._token_cache_key(settings)] = "shpat_stale"

        self.script_get(FakeResponse(401))
        with self.assertRaises(ShopifyAPIError):
            shopify_api.get(settings, "orders/1/transactions.json")

        # Cache is now empty, so the next request mints and succeeds.
        self.script_post(self.token_response())
        self.script_get(FakeResponse(200, {"transactions": [{"id": 1}]}))
        body = shopify_api.get(settings, "orders/1/transactions.json")

        self.assertEqual(body["transactions"][0]["id"], 1)
        self.assertEqual(len(self.posts), 1, "should have minted a fresh token")

    def test_minted_token_is_sent_on_the_request(self):
        self.script_post(self.token_response())
        self.script_get(FakeResponse(200, {"transactions": []}))
        shopify_api.get(self.cc_settings(), "orders/1/transactions.json")

        self.assertEqual(self.gets[0]["headers"]["X-Shopify-Access-Token"], MINTED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
