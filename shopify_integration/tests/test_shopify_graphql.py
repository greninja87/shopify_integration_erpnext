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

The third property, and the expensive one, is HOW MANY TIMES a document is
posted.  execute() retries the identical document, which is right for
fulfillment and wrong for refundCreate: a successful refundCreate pays a real
customer through the Cashfree-OCC bridge and carries no idempotency key, so a
retry after a lost response pays them twice.  TestNonIdempotentExecute pins the
post count for that case, and TestExecute / TestHttpErrors pin that nothing
changed for everyone else.
"""

import sys
import types
import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.utils import shopify_api, shopify_graphql  # noqa: E402
from shopify_integration.utils.shopify_api import ShopifyAPIError  # noqa: E402
from shopify_integration.utils.shopify_graphql import ShopifyUserError  # noqa: E402


# Stands in for the RefundTargets read: idempotent, re-postable, and the caller
# refund.py names when it promises that query keeps its throttle resilience.
_READ = "query RefundTargets { order { transactions { gateway } } }"


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

    def test_429_is_retried_for_an_idempotent_caller(self):
        """
        Waiting out a rate limit is free for a re-postable document, so the
        default keeps this.  It is not proof of anything, and a money-moving
        caller does NOT get it — see TestBare429IsNotProof.
        """
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

    def test_missing_mutation_key_is_filed_as_an_answered_request(self):
        """
        refund.py splits "the transport failed" from "Shopify answered
        something unusable" on `status_code is None`, and this response was
        answered in FULL as HTTP 200 — it simply did not contain the mutation.
        Raising with no status_code files it as transport_error_after_send,
        which sends the reader to look at the network when Shopify had replied.
        """
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.check_user_errors({"other": {}}, "refundCreate",
                                              context="REF-00207")
        self.assertEqual(ctx.exception.status_code, 200)
        self.assertIn("REF-00207", str(ctx.exception))
        self.assertFalse(
            ctx.exception.proves_not_executed,
            "a 200 that lacks the mutation key is not proof it never ran",
        )

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


class TestNonIdempotentExecute(GraphQLTestCase):
    """
    execute(idempotent=False) — "this exact document must not be re-posted".

    refundCreate is the caller that needs it.  A refundCreate that succeeds
    pays a real customer real money through the Cashfree-OCC bridge, and
    build_refund_mutation() is called with no key, so the @idempotent directive
    is absent and Shopify has no way to recognise a second POST of the same
    document as the same refund.  One lost response is enough: attempt 1
    creates a real partial refund, its answer dies in the 30-second socket
    timeout, the retry re-posts the identical mutation, the order still has
    headroom because the refund was partial, and the customer is paid twice.
    Neither the stored GID nor the worker claim is consulted between attempts.

    The rule pinned here is "retry only on failures that PROVE the document did
    not execute".  A timeout or a 502 proves nothing — either may be hiding a
    mutation that ran.  Nor does a bare HTTP 429, which round 2 read as a
    refusal: Shopify throttles GraphQL with a 200 body, so a 429 here may be a
    CDN, a WAF or an egress proxy in front of the store.  Exactly one failure
    qualifies — HTTP 200 with THROTTLED in the shape the spec reserves for a
    request refused before execution began — and dropping THAT retry would
    park Shopify's ordinary cost limiting in Unverified where a person has to
    clear every row by hand.

    So these tests assert on the NUMBER OF POSTS, not just the exception.  The
    post count IS the safety property; an exception raised after two posts has
    already paid the customer twice.
    """

    def refund_create(self, **kwargs):
        return shopify_graphql.execute(
            self.settings,
            "mutation { refundCreate(input: $input) { refund { id } } }",
            {"input": {}},
            operation="refundCreate",
            **kwargs,
        )

    def test_transport_error_is_not_retried(self):
        """A socket timeout is the exact shape of the double-pay incident."""
        self.script(OSError("timed out"), FakeResponse(200, {"data": {"ok": 1}}))
        with self.assertRaises(ShopifyAPIError):
            self.refund_create(idempotent=False)
        self.assertEqual(
            len(self.calls), 1,
            "a lost response may be hiding a refund that already ran",
        )

    def test_http_500_is_not_retried(self):
        """
        A 502 from Shopify's edge can be raised after the mutation committed,
        so it is not proof of non-execution either.
        """
        self.script(FakeResponse(500), FakeResponse(200, {"data": {"ok": 1}}))
        with self.assertRaises(ShopifyAPIError):
            self.refund_create(idempotent=False)
        self.assertEqual(len(self.calls), 1)

    def test_429_is_not_retried_when_non_idempotent(self):
        """
        Round 2 retried this one on the reasoning "Shopify rejects a
        rate-limited request WITHOUT running it".  A bare HTTP 429 on the
        GraphQL endpoint is not Shopify saying that.  Shopify's own GraphQL
        throttling arrives as HTTP 200 with extensions.code THROTTLED — this
        module documents that itself — so a 429 here is as likely to come from
        a CDN, a WAF or an egress proxy in front of the store, and such a layer
        knows nothing at all about whether the document BEHIND it ran.
        Re-posting refundCreate on that premise is the original double-pay
        incident with somebody else's infrastructure as the excuse.
        """
        self.script(FakeResponse(429), FakeResponse(200, {"data": {"ok": 1}}))
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)

        self.assertEqual(
            len(self.calls), 1,
            "an HTTP-layer rate limit is no evidence about the document",
        )
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_throttled_body_is_still_retried(self):
        """
        The GraphQL flavour of the refusal, and the only one Shopify actually
        makes: HTTP 200, extensions.code THROTTLED, and NO `data` key at all —
        the spec's shape for "execution never began".
        """
        self.script(
            FakeResponse(200, {"errors": [{"extensions": {"code": "THROTTLED"}}]}),
            FakeResponse(200, {"data": {"ok": 1}}),
        )
        data = self.refund_create(idempotent=False)
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(len(self.calls), 2)

    def test_default_still_retries_5xx_to_the_attempt_ceiling(self):
        """
        Pinned so the fix cannot regress fulfillment.  Every existing caller
        omits the flag and must keep retrying byte-for-byte as before:
        fulfillmentCreate is re-postable, and a Delivery Note that fails to
        push because of one 503 is a worse outcome than a duplicate attempt.
        """
        self.script(*[FakeResponse(500) for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError):
            shopify_graphql.execute(self.settings, "mutation { fulfillmentCreate }")
        self.assertEqual(len(self.calls), shopify_api._MAX_ATTEMPTS)

    def test_default_still_retries_transport_errors(self):
        self.script(OSError("reset"), FakeResponse(200, {"data": {"ok": 1}}))
        self.assertEqual(
            shopify_graphql.execute(self.settings, "mutation { fulfillmentCreate }"),
            {"ok": 1},
        )
        self.assertEqual(len(self.calls), 2)


class TestErrorCodesOnTheException(GraphQLTestCase):
    """
    The proof of non-execution has to reach the caller.

    A refund write-back that dies on Shopify's own cost refusal — HTTP 200,
    THROTTLED, no `data` key, no errors[].path — was refused, not run.
    payment_portals can call that one "not sent" and let the operator retry it,
    instead of parking it in Unverified for a human to reconcile.  Every other
    HTTP 200 with `errors`, and the bare 429 as of round 3, carries no such
    promise, so the codes are handed over rather than interpreted here.
    """

    def test_exhausted_throttling_carries_the_throttled_code(self):
        throttled = lambda: FakeResponse(200, {  # noqa: E731
            "errors": [{"message": "Throttled",
                        "extensions": {"code": "THROTTLED"}}]
        })
        self.script(*[throttled() for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.execute(self.settings, "query { x }")

        self.assertEqual(ctx.exception.error_codes, ["THROTTLED"])
        self.assertEqual(len(self.calls), shopify_api._MAX_ATTEMPTS)

    def test_other_graphql_errors_carry_their_own_codes(self):
        self.script(FakeResponse(200, {
            "errors": [{"message": "no", "extensions": {"code": "ACCESS_DENIED"}}]
        }))
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.execute(self.settings, "query { x }")
        self.assertEqual(ctx.exception.error_codes, ["ACCESS_DENIED"])

    def test_codes_default_to_empty_not_none(self):
        """So a caller can write `"THROTTLED" in exc.error_codes` blind."""
        self.assertEqual(ShopifyAPIError("boom").error_codes, [])
        self.assertEqual(ShopifyAPIError("boom", 500).error_codes, [])

    def test_error_codes_is_a_copy_of_what_was_passed(self):
        codes = ["THROTTLED"]
        exc = ShopifyAPIError("boom", 200, codes)
        codes.append("MUTATED")
        self.assertEqual(exc.error_codes, ["THROTTLED"])

    def test_a_bare_string_is_one_code_not_nine_characters(self):
        """
        `list("THROTTLED")` is ["T","H","R","O","T","T","L","E","D"], on which
        `"THROTTLED" in exc.error_codes` is False while every character of it
        matches.  A raise site one bracket-pair away from the correct form
        would therefore look right in a log line and read as "no THROTTLED
        here" to anything that checks — the failure mode this module spends its
        whole docstring warning about, reached by a typo.
        """
        self.assertEqual(
            ShopifyAPIError("boom", 429, error_codes="THROTTLED").error_codes,
            ["THROTTLED"],
        )
        self.assertIn(
            "THROTTLED",
            ShopifyAPIError("boom", 429, error_codes="THROTTLED").error_codes,
        )

    def test_an_empty_string_is_no_codes_at_all(self):
        """"" is falsy and means "nothing to report", not a code named ""."""
        self.assertEqual(ShopifyAPIError("boom", 429, error_codes="").error_codes, [])

    def test_a_tuple_or_other_iterable_still_becomes_a_list(self):
        exc = ShopifyAPIError("boom", 200, error_codes=("THROTTLED", "MAX_COST"))
        self.assertEqual(exc.error_codes, ["THROTTLED", "MAX_COST"])

    def test_user_error_still_constructs_with_a_message_alone(self):
        """
        ShopifyUserError calls super().__init__(message) only, so every added
        parameter must stay optional — and a rejected mutation proves nothing
        about a document Shopify got as far as evaluating.
        """
        exc = ShopifyUserError("declined", user_errors=[{"message": "declined"}])
        self.assertEqual(str(exc), "declined")
        self.assertIsNone(exc.status_code)
        self.assertEqual(exc.error_codes, [])
        self.assertFalse(exc.proves_not_executed)
        self.assertEqual(len(exc.user_errors), 1)


class TestThrottledWithPartialData(GraphQLTestCase):
    """
    HTTP 200 carrying BOTH a populated `data` and a top-level THROTTLED error.

    Round 1 read every 200-body THROTTLED as proof the document had not run and
    re-posted it.  GraphQL makes no such promise: a document can resolve part of
    the way, exhaust the cost budget, and come back with `data` filled in AND
    errors[].extensions.code == "THROTTLED".  The refundCreate document resolves
    a `transactions(first: 10)` connection, which has real query cost, so that
    shape is reachable on the one document that pays real money — and re-posting
    it after Shopify committed the refund pays the customer twice.  That is the
    original double-pay defect, living inside the branch that was meant to be
    the safe one.

    So a body that shows execution began is never PROOF of non-execution, for
    anybody.  Whether it is re-posted is a different question and turns on
    `idempotent`: the money-path document is not, a read is (see
    TestReadsKeepTheirThrottleResilience).  These tests assert POST COUNTS,
    because the post count is the safety property: an exception raised after two
    posts has already paid twice.
    """

    def throttled_with_data(self):
        return FakeResponse(200, {
            "data": {"refundCreate": {
                "refund": {"id": "gid://shopify/Refund/55"},
                "userErrors": [],
            }},
            "errors": [{"message": "Throttled",
                        "extensions": {"code": "THROTTLED"}}],
        })

    def refund_create(self, **kwargs):
        return shopify_graphql.execute(
            self.settings,
            "mutation { refundCreate(input: $input) { refund { id } } }",
            {"input": {}},
            operation="refundCreate",
            **kwargs,
        )

    def test_partial_data_is_not_re_posted_when_non_idempotent(self):
        """The refund may already be committed inside that `data`."""
        self.script(self.throttled_with_data(), FakeResponse(200, {"data": {"ok": 1}}))
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)

        self.assertEqual(len(self.calls), 1)
        self.assertFalse(
            ctx.exception.proves_not_executed,
            "a THROTTLED body carrying data may be hiding a committed mutation",
        )

    def test_partial_data_is_still_re_posted_for_an_idempotent_caller(self):
        """
        Round 2 made the gate unconditional, which stripped the throttle
        resilience off every read.  refund.py's own comment promises the
        RefundTargets query keeps it — "it is a read, re-posting it cannot pay
        anybody" — and a re-read is free, so the gate belongs on `idempotent`,
        not on the body shape alone.

        Whether re-posting a PARTIAL fulfillmentCreate is safe is a separate,
        unaudited question the module docstring already names; this restores the
        pre-round-2 behaviour rather than resolving it by accident.
        """
        self.script(self.throttled_with_data(), FakeResponse(200, {"data": {"ok": 1}}))
        self.assertEqual(
            shopify_graphql.execute(
                self.settings, "mutation { fulfillmentCreate }",
                operation="fulfillmentCreate", idempotent=True,
            ),
            {"ok": 1},
        )
        self.assertEqual(len(self.calls), 2)

    def test_partial_data_never_proves_non_execution_even_when_re_posted(self):
        """
        The retry came back, the PROOF did not.  An idempotent caller that
        exhausts its attempts on this shape must still be told "unknown".
        """
        self.script(*[self.throttled_with_data()
                      for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.execute(
                self.settings, "mutation { fulfillmentCreate }",
                operation="fulfillmentCreate", idempotent=True,
            )
        self.assertEqual(len(self.calls), shopify_api._MAX_ATTEMPTS)
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_partial_data_still_hands_the_codes_over(self):
        """
        The codes keep travelling — what changed is that they are no longer the
        caller's evidence about execution.  THROTTLED-with-data is exactly the
        case where sniffing them gives the wrong, expensive answer.
        """
        self.script(self.throttled_with_data())
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)

        self.assertIn("THROTTLED", ctx.exception.error_codes)
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_throttled_with_missing_data_key_is_still_retried(self):
        self.script(
            FakeResponse(200, {"errors": [{"extensions": {"code": "THROTTLED"}}]}),
            FakeResponse(200, {"data": {"ok": 1}}),
        )
        self.assertEqual(
            shopify_graphql.execute(self.settings, "query { x }"), {"ok": 1}
        )
        self.assertEqual(len(self.calls), 2)


class TestRefusedBeforeExecution(GraphQLTestCase):
    """
    The two GraphQL error shapes THROTTLED can wear, and why only one is proof.

    Round 2 keyed the proof on `body.get("data") is None`, which lumps them
    together.  The GraphQL spec keeps them apart:

        no "data" KEY at all          the request was refused BEFORE execution
                                      began.  Shopify's ordinary cost refusal.
        a "data" key, possibly null,  execution BEGAN and died partway;
        plus errors[].path            errors[].path names the field it died on,
                                      and a path only exists for a field that
                                      was being resolved.

    A body of {"data": null, "errors": [{"path": ["refundCreate"]}]} is the
    second shape, and round 2 read it as the first — i.e. as "nobody was paid,
    safe to retry" on a mutation that had started running.  refundCreate is the
    document that makes that expensive, so the proof is narrowed to the first
    shape only and the post count is asserted, not just the flag.
    """

    def refund_create(self, **kwargs):
        return shopify_graphql.execute(
            self.settings,
            "mutation { refundCreate(input: $input) { refund { id } } }",
            {"input": {}},
            operation="refundCreate",
            **kwargs,
        )

    @staticmethod
    def refused_body():
        """Shopify's cost refusal: no `data` key, no path."""
        return FakeResponse(200, {
            "errors": [{"message": "Throttled",
                        "extensions": {"code": "THROTTLED"}}],
        })

    @staticmethod
    def died_during_execution_body():
        """`data` present and null, with a path naming the field it died on."""
        return FakeResponse(200, {
            "data": None,
            "errors": [{"message": "Throttled",
                        "path": ["refundCreate"],
                        "extensions": {"code": "THROTTLED"}}],
        })

    def test_no_data_key_is_retried_and_proves_non_execution(self):
        self.script(*[self.refused_body() for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)

        self.assertEqual(len(self.calls), shopify_api._MAX_ATTEMPTS)
        self.assertTrue(ctx.exception.proves_not_executed)

    def test_null_data_with_a_path_is_one_post_and_proves_nothing(self):
        """
        The shape round 2 got wrong.  `path` says a field was being resolved,
        so the document had begun executing and may have committed the refund.
        """
        self.script(self.died_during_execution_body(),
                    FakeResponse(200, {"data": {"ok": 1}}))
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)

        self.assertEqual(len(self.calls), 1)
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_null_data_without_a_path_is_not_proof_either(self):
        """
        Both halves of the rule are required.  The spec omits `data` entirely
        for a pre-execution failure, so a body that carries the key — even set
        to null — says execution began, and "no path was reported" does not
        take that back.
        """
        self.script(
            FakeResponse(200, {"data": None,
                               "errors": [{"extensions": {"code": "THROTTLED"}}]}),
            FakeResponse(200, {"data": {"ok": 1}}),
        )
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)

        self.assertEqual(len(self.calls), 1)
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_no_data_key_but_a_path_is_not_proof(self):
        """
        A path with no `data` key is a contradiction on the wire, and the two
        halves disagree about whether anything ran.  Between "might pay twice"
        and "might need a human to look", the disagreement resolves to the
        human.
        """
        self.script(FakeResponse(200, {
            "errors": [{"path": ["refundCreate"],
                        "extensions": {"code": "THROTTLED"}}],
        }))
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)

        self.assertEqual(len(self.calls), 1)
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_the_helper_states_the_rule_once(self):
        """
        Read directly, because the rule is the thing under test and reading it
        through five scripted HTTP conversations hides which half fired.
        """
        refused = shopify_graphql._refused_before_execution
        codes = ["THROTTLED"]

        self.assertTrue(refused({"errors": [{}]}, codes))
        self.assertFalse(refused({"data": None, "errors": [{}]}, codes))
        self.assertFalse(refused({"data": {"x": 1}, "errors": [{}]}, codes))
        self.assertFalse(
            refused({"errors": [{"path": ["refundCreate"]}]}, codes)
        )
        self.assertFalse(
            refused({"errors": [{}, {"path": ["refundCreate"]}]}, codes),
            "one error entry with a path is enough to withdraw the claim",
        )
        self.assertFalse(
            refused({"errors": [{}]}, ["INTERNAL_SERVER_ERROR"]),
            "only THROTTLED is Shopify's documented refusal",
        )


class TestReadsKeepTheirThrottleResilience(GraphQLTestCase):
    """
    Round 2 gated the THROTTLED retry on the body shape for EVERY caller, which
    silently stripped the resilience off the reads.  refund.py's comment at the
    RefundTargets query promises the opposite — "it is a read, re-posting it
    cannot pay anybody, and losing that resilience would trade a safe retry for
    a fragile one" — and a re-read costs nothing but a wait.

    So the retry gate is `idempotent or refused_before_execution`.  The
    money-path guard is untouched: a non-idempotent document is still only
    retried on a refusal that proves non-execution.
    """

    def throttled_with_data(self):
        return FakeResponse(200, {
            "data": {"order": {"id": "gid://shopify/Order/1"}},
            "errors": [{"message": "Throttled",
                        "path": ["order", "transactions"],
                        "extensions": {"code": "THROTTLED"}}],
        })

    def test_a_read_retries_a_throttle_that_carries_data(self):
        self.script(self.throttled_with_data(), FakeResponse(200, {"data": {"ok": 1}}))
        self.assertEqual(
            shopify_graphql.execute(self.settings, _READ, operation="RefundTargets"),
            {"ok": 1},
        )
        self.assertEqual(len(self.calls), 2)

    def test_a_read_retries_to_the_pre_round_2_ceiling(self):
        """The count is the claim: five posts, as before round 2."""
        self.script(*[self.throttled_with_data()
                      for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError):
            shopify_graphql.execute(self.settings, _READ, operation="RefundTargets")
        self.assertEqual(len(self.calls), shopify_api._MAX_ATTEMPTS)

    def test_the_money_path_guard_is_unchanged_by_that(self):
        """Same body, idempotent=False: still one post, still no proof."""
        self.script(self.throttled_with_data(), FakeResponse(200, {"data": {"ok": 1}}))
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.execute(
                self.settings, "mutation { refundCreate }",
                operation="refundCreate", idempotent=False,
            )
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(ctx.exception.proves_not_executed)


class TestBare429IsNotProof(GraphQLTestCase):
    """
    An HTTP 429 says a rate limiter refused the request.  It does not say WHICH
    rate limiter, and that is the whole problem: Shopify's own GraphQL
    throttling arrives as HTTP 200 with extensions.code THROTTLED, so a 429 on
    this endpoint is as likely to be a CDN, a WAF or an egress proxy in front
    of the store.  Such a layer is no evidence at all about whether the
    document BEHIND it ran.  The branch is inherited from the REST client's
    shape, not from anything Shopify documents for GraphQL.

    The trade is deliberate and costs a person's time: an HTTP-layer rate limit
    on the refund path now lands as "unknown", so somebody opens the order in
    Shopify.  The alternative asserts a premise about somebody else's
    infrastructure and pays for being wrong with a second real refund.
    """

    def test_exhausted_429_proves_nothing_when_non_idempotent(self):
        self.script(*[FakeResponse(429) for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.execute(
                self.settings, "mutation { refundCreate }",
                operation="refundCreate", idempotent=False,
            )

        self.assertEqual(len(self.calls), 1, "post once, then stop")
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_exhausted_429_proves_nothing_when_idempotent_either(self):
        """The post count is unchanged for reads; only the CLAIM changed."""
        self.script(*[FakeResponse(429) for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.execute(self.settings, _READ)

        self.assertEqual(len(self.calls), shopify_api._MAX_ATTEMPTS)
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_the_429_still_carries_the_throttled_code_for_triage(self):
        """
        Diagnostic, not evidence.  A human triaging a rate limit wants to see
        THROTTLED on it; nobody may conclude non-execution from it.  That the
        code and the proof point opposite ways on the same exception is exactly
        why callers must not sniff error_codes for a payout decision.
        """
        self.script(*[FakeResponse(429) for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.execute(self.settings, _READ)

        self.assertEqual(ctx.exception.error_codes, ["THROTTLED"])
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertFalse(ctx.exception.proves_not_executed)


class TestProvesNotExecuted(GraphQLTestCase):
    """
    `proves_not_executed` — the one fact a money-moving caller may branch on.

    Only the client knows whether Shopify produced its answer INSTEAD of running
    the document, so the client states it outright instead of leaving the caller
    to infer it from error codes.  refund.py used to read "THROTTLED in
    error_codes" as "nobody was paid"; a THROTTLED body carrying `data` breaks
    that inference, and the price of breaking it is a refund reported
    failed_unsent / retry_safe after Shopify has already paid the customer.

    The default is the safe one: False means "assume it may have run".
    """

    def refund_create(self, **kwargs):
        return shopify_graphql.execute(
            self.settings, "mutation { refundCreate }",
            operation="refundCreate", **kwargs,
        )

    def test_exhausted_throttling_refused_before_execution_proves_not_executed(self):
        """
        Shopify's cost refusal, in the shape the spec reserves for a request
        that never began: no `data` key, and no errors[].path.
        """
        throttled = lambda: FakeResponse(200, {  # noqa: E731
            "errors": [{"extensions": {"code": "THROTTLED"}}],
        })
        self.script(*[throttled() for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)

        self.assertEqual(len(self.calls), shopify_api._MAX_ATTEMPTS)
        self.assertTrue(ctx.exception.proves_not_executed)

    def test_a_bare_429_does_not_prove_not_executed(self):
        """
        Round 2 claimed proof here.  It is somebody else's rate limiter as
        often as Shopify's — see TestBare429IsNotProof — so the claim is
        withdrawn and the row goes to a human.
        """
        self.script(*[FakeResponse(429) for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)

        self.assertEqual(len(self.calls), 1)
        self.assertFalse(ctx.exception.proves_not_executed)
        self.assertEqual(
            ctx.exception.error_codes, ["THROTTLED"],
            "the code stays for triage even though the proof is gone",
        )

    def test_401_proves_not_executed(self):
        self.script(FakeResponse(401))
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(ctx.exception.proves_not_executed)

    def test_403_proves_not_executed(self):
        self.script(FakeResponse(403))
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)
        self.assertTrue(ctx.exception.proves_not_executed)

    def test_transport_error_proves_nothing(self):
        """The answer was lost; the document may still have run."""
        self.script(OSError("timed out"))
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_exhausted_transport_errors_prove_nothing(self):
        self.script(*[OSError("reset") for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.execute(self.settings, "mutation { fulfillmentCreate }")
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_5xx_proves_nothing(self):
        """A 502 from the edge can arrive after the mutation committed."""
        self.script(FakeResponse(502))
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_exhausted_5xx_proves_nothing(self):
        self.script(*[FakeResponse(503) for _ in range(shopify_api._MAX_ATTEMPTS)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            shopify_graphql.execute(self.settings, "mutation { fulfillmentCreate }")
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_other_graphql_errors_prove_nothing(self):
        self.script(FakeResponse(200, {
            "data": None,
            "errors": [{"extensions": {"code": "INTERNAL_SERVER_ERROR"}}],
        }))
        with self.assertRaises(ShopifyAPIError) as ctx:
            self.refund_create(idempotent=False)
        self.assertFalse(ctx.exception.proves_not_executed)

    def test_default_is_the_safe_one(self):
        self.assertFalse(ShopifyAPIError("boom").proves_not_executed)
        self.assertFalse(ShopifyAPIError("boom", 500).proves_not_executed)
        exc = ShopifyAPIError("boom", 200, ["THROTTLED"])
        self.assertFalse(exc.proves_not_executed)

    def test_flag_is_coerced_to_a_bool(self):
        """So a caller can write `is True` / `is False` without surprises."""
        exc = ShopifyAPIError("boom", 429, proves_not_executed=1)
        self.assertIs(exc.proves_not_executed, True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
