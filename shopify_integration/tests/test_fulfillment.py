"""
test_fulfillment.py — tests for the fulfillment planning logic.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_fulfillment -v

Covers the three places a fulfillment can go quietly wrong:

  * classify_fulfillment_orders — calling fulfillmentCreate on a fulfillment
    order that only supports REQUEST_FULFILLMENT
  * plan_fulfillment — over-fulfilling a line another Delivery Note already
    covered, or fulfilling the wrong line when one order carries the same SKU
    twice
  * how many times fulfillmentCreate is POSTED, and what the Delivery Note says
    afterwards.  A partly fulfilled order still has unfulfilled quantity on its
    fulfillment order, so a second post of the same input is ACCEPTED: a
    duplicate fulfillment, a second tracking email, quantity fulfilled twice.
    TestPostCount pins the post count; TestPossiblySentWarning pins the text a
    person reads before pressing retry.
"""

import json
import sys
import types
import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

import frappe  # noqa: E402

from shopify_integration.tests.frappe_stub import FakeSettings  # noqa: E402
from shopify_integration.utils import fulfillment as f  # noqa: E402
from shopify_integration.utils import shopify_api  # noqa: E402
from shopify_integration.utils.shopify_api import (  # noqa: E402
    ShopifyAPIError,
    _MAX_ATTEMPTS,
)


def fo(fo_id, status="OPEN", actions=("CREATE_FULFILLMENT",), lines=(), location="Main"):
    """Build a fulfillment order node as the GraphQL query returns it."""
    return {
        "id": fo_id,
        "status": status,
        "requestStatus": None,
        "supportedActions": [{"action": a} for a in actions],
        "assignedLocation": {"name": location},
        "lineItems": {"nodes": list(lines)},
    }


def fo_line(fo_line_id, line_item_id, sku, remaining, total=None):
    """Build a FulfillmentOrderLineItem node."""
    return {
        "id": fo_line_id,
        "remainingQuantity": remaining,
        "totalQuantity": total if total is not None else remaining,
        "sku": sku,
        "lineItem": {"id": line_item_id, "sku": sku},
    }


def want(sku, qty, line_item_id=""):
    return {"line_item_id": line_item_id, "sku": sku, "qty": qty}


# ── Classification ────────────────────────────────────────────────────────────

class TestClassifyFulfillmentOrders(unittest.TestCase):
    def test_empty(self):
        result = f.classify_fulfillment_orders([])
        self.assertEqual(result, {"creatable": [], "third_party": [], "inactive": []})
        self.assertEqual(f.classify_fulfillment_orders(None)["creatable"], [])

    def test_open_with_create_action_is_creatable(self):
        result = f.classify_fulfillment_orders([fo("fo/1")])
        self.assertEqual(len(result["creatable"]), 1)

    def test_third_party_is_not_creatable(self):
        """The bug this prevents: calling fulfillmentCreate on a 3PL order."""
        result = f.classify_fulfillment_orders(
            [fo("fo/1", actions=("REQUEST_FULFILLMENT", "MOVE"))]
        )
        self.assertEqual(result["creatable"], [])
        self.assertEqual(len(result["third_party"]), 1)

    def test_closed_is_inactive_even_with_create_action(self):
        result = f.classify_fulfillment_orders([fo("fo/1", status="CLOSED")])
        self.assertEqual(result["creatable"], [])
        self.assertEqual(len(result["inactive"]), 1)

    def test_in_progress_is_actionable(self):
        result = f.classify_fulfillment_orders([fo("fo/1", status="IN_PROGRESS")])
        self.assertEqual(len(result["creatable"]), 1)

    def test_on_hold_and_scheduled_are_actionable(self):
        for status in ("ON_HOLD", "SCHEDULED"):
            result = f.classify_fulfillment_orders([fo("fo/1", status=status)])
            self.assertEqual(len(result["creatable"]), 1, status)

    def test_no_useful_actions_is_inactive(self):
        result = f.classify_fulfillment_orders([fo("fo/1", actions=("MOVE", "HOLD"))])
        self.assertEqual(result["creatable"], [])
        self.assertEqual(len(result["inactive"]), 1)

    def test_status_is_case_insensitive(self):
        result = f.classify_fulfillment_orders([fo("fo/1", status="open")])
        self.assertEqual(len(result["creatable"]), 1)

    def test_mixed_set_is_split(self):
        result = f.classify_fulfillment_orders([
            fo("fo/1"),
            fo("fo/2", actions=("REQUEST_FULFILLMENT",)),
            fo("fo/3", status="CLOSED"),
        ])
        self.assertEqual(len(result["creatable"]), 1)
        self.assertEqual(len(result["third_party"]), 1)
        self.assertEqual(len(result["inactive"]), 1)

    def test_garbage_rows_ignored(self):
        result = f.classify_fulfillment_orders(["nope", None, fo("fo/1")])
        self.assertEqual(len(result["creatable"]), 1)


# ── Planning ──────────────────────────────────────────────────────────────────

class TestPlanFulfillment(unittest.TestCase):
    def test_simple_single_line(self):
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 2)])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 2, "li/1")])

        self.assertEqual(plan["allocated"], 2)
        self.assertEqual(plan["unallocated"], [])
        self.assertEqual(plan["line_items_by_fulfillment_order"], [{
            "fulfillmentOrderId": "fo/1",
            "fulfillmentOrderLineItems": [{"id": "fol/1", "quantity": 2}],
        }])

    def test_matches_by_line_item_id_not_sku(self):
        """
        Same SKU on two Shopify line items. Matching by SKU would fulfil the
        wrong one; matching by line item id gets it right.
        """
        orders = [fo("fo/1", lines=[
            fo_line("fol/1", "li/1", "SKU-A", 1),
            fo_line("fol/2", "li/2", "SKU-A", 1),
        ])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 1, "li/2")])

        rows = plan["line_items_by_fulfillment_order"][0]["fulfillmentOrderLineItems"]
        self.assertEqual(rows, [{"id": "fol/2", "quantity": 1}])

    def test_falls_back_to_sku_when_no_line_item_id(self):
        """Orders synced before custom_shopify_line_item_id existed."""
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 3)])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 3)])

        self.assertEqual(plan["allocated"], 3)

    def test_sku_match_is_case_insensitive(self):
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "sku-a", 1)])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 1)])
        self.assertEqual(plan["allocated"], 1)

    def test_never_exceeds_remaining_quantity(self):
        """Another Delivery Note already shipped part of this line."""
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 1, total=3)])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 3, "li/1")])

        self.assertEqual(plan["allocated"], 1)
        self.assertEqual(len(plan["unallocated"]), 1)
        self.assertEqual(plan["unallocated"][0]["qty"], 2)
        self.assertIn("already fulfilled", plan["unallocated"][0]["reason"])

    def test_zero_remaining_allocates_nothing(self):
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 0, total=2)])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 2, "li/1")])

        self.assertEqual(plan["allocated"], 0)
        self.assertEqual(plan["line_items_by_fulfillment_order"], [])
        self.assertIn("already fulfilled", plan["unallocated"][0]["reason"])

    def test_splits_across_two_fulfillment_orders(self):
        """A multi-location order: one SKU sitting in two fulfillment orders."""
        orders = [
            fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 1)], location="Delhi"),
            fo("fo/2", lines=[fo_line("fol/2", "li/1", "SKU-A", 2)], location="Mumbai"),
        ]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 3, "li/1")])

        self.assertEqual(plan["allocated"], 3)
        self.assertEqual(len(plan["line_items_by_fulfillment_order"]), 2)
        self.assertEqual(sorted(plan["locations"]), ["Delhi", "Mumbai"])

    def test_third_party_lines_are_not_allocated(self):
        orders = [fo("fo/1", actions=("REQUEST_FULFILLMENT",),
                     lines=[fo_line("fol/1", "li/1", "SKU-A", 2)])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 2, "li/1")])

        self.assertEqual(plan["allocated"], 0)
        self.assertEqual(plan["line_items_by_fulfillment_order"], [])
        self.assertEqual(plan["third_party"], ["fo/1"])

    def test_unmatched_sku_is_reported(self):
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 1)])]
        plan = f.plan_fulfillment(orders, [want("SKU-ZZZ", 1, "li/999")])

        self.assertEqual(plan["allocated"], 0)
        self.assertIn("no matching open", plan["unallocated"][0]["reason"])

    def test_multi_line_order(self):
        orders = [fo("fo/1", lines=[
            fo_line("fol/1", "li/1", "SKU-A", 2),
            fo_line("fol/2", "li/2", "SKU-B", 1),
        ])]
        plan = f.plan_fulfillment(
            orders, [want("SKU-A", 2, "li/1"), want("SKU-B", 1, "li/2")]
        )

        self.assertEqual(plan["allocated"], 3)
        rows = plan["line_items_by_fulfillment_order"][0]["fulfillmentOrderLineItems"]
        self.assertEqual(len(rows), 2)

    def test_partial_delivery_note_fulfils_only_what_shipped(self):
        """Order of 5, this DN ships 2. The rest stays unfulfilled in Shopify."""
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 5)])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 2, "li/1")])

        self.assertEqual(plan["allocated"], 2)
        self.assertEqual(plan["unallocated"], [])

    def test_zero_and_negative_quantities_skipped(self):
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 5)])]
        plan = f.plan_fulfillment(
            orders, [want("SKU-A", 0, "li/1"), want("SKU-A", -3, "li/1")]
        )
        self.assertEqual(plan["allocated"], 0)
        self.assertEqual(plan["line_items_by_fulfillment_order"], [])

    def test_empty_wanted_allocates_nothing(self):
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 5)])]
        self.assertEqual(f.plan_fulfillment(orders, [])["allocated"], 0)
        self.assertEqual(f.plan_fulfillment(orders, None)["allocated"], 0)

    def test_no_fulfillment_orders_at_all(self):
        plan = f.plan_fulfillment([], [want("SKU-A", 1, "li/1")])
        self.assertEqual(plan["allocated"], 0)
        self.assertEqual(len(plan["unallocated"]), 1)

    def test_closed_fulfillment_order_contributes_nothing(self):
        orders = [fo("fo/1", status="CLOSED",
                     lines=[fo_line("fol/1", "li/1", "SKU-A", 2)])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 2, "li/1")])
        self.assertEqual(plan["allocated"], 0)
        self.assertEqual(plan["third_party"], [])


# ── Tracking + input assembly ─────────────────────────────────────────────────

class TestBuildTrackingInfo(unittest.TestCase):
    def test_nothing_returns_none(self):
        self.assertIsNone(f.build_tracking_info())
        self.assertIsNone(f.build_tracking_info("", "", ""))
        self.assertIsNone(f.build_tracking_info("   ", "  ", " "))

    def test_number_only(self):
        self.assertEqual(f.build_tracking_info(number="AWB123"), {"number": "AWB123"})

    def test_all_three(self):
        self.assertEqual(
            f.build_tracking_info("AWB123", "Delhivery", "https://track/AWB123"),
            {"number": "AWB123", "company": "Delhivery", "url": "https://track/AWB123"},
        )

    def test_values_are_stripped(self):
        self.assertEqual(
            f.build_tracking_info("  AWB123  ", " DTDC "),
            {"number": "AWB123", "company": "DTDC"},
        )

    def test_company_only_is_valid(self):
        self.assertEqual(f.build_tracking_info(company="India Post"),
                         {"company": "India Post"})


class TestBuildFulfillmentInput(unittest.TestCase):
    def _plan(self):
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 1)])]
        return f.plan_fulfillment(orders, [want("SKU-A", 1, "li/1")])

    def test_notify_customer_true(self):
        payload = f.build_fulfillment_input(self._plan(), notify_customer=True)
        self.assertIs(payload["notifyCustomer"], True)
        self.assertNotIn("trackingInfo", payload)

    def test_notify_customer_false(self):
        payload = f.build_fulfillment_input(self._plan(), notify_customer=False)
        self.assertIs(payload["notifyCustomer"], False)

    def test_tracking_included_when_present(self):
        tracking = f.build_tracking_info("AWB1", "Delhivery")
        payload = f.build_fulfillment_input(self._plan(), True, tracking)
        self.assertEqual(payload["trackingInfo"], tracking)

    def test_line_items_are_carried_through(self):
        payload = f.build_fulfillment_input(self._plan(), True)
        self.assertEqual(
            payload["lineItemsByFulfillmentOrder"][0]["fulfillmentOrderId"], "fo/1"
        )


# -- The post that must not be repeated ---------------------------------------
#
# fulfillmentCreate carries no idempotency key, and the justification for
# re-posting it -- "a fulfillmentCreate that already landed gets a userErrors
# rejection, not a second shipment" -- holds only for a FULL fulfillment.  This
# module builds PARTIAL ones on purpose (plan["unallocated"], STATUS_PARTIAL),
# and a partly fulfilled order still has unfulfilled quantity on the
# fulfillment order, so a second post of the same input is accepted.  Same
# headroom argument as the refund double-pay.

DN = "DN-0001"
ORDER_ID = "7843650535529"
ORDER_GID = "gid://shopify/Order/7843650535529"
LINE_GID = "gid://shopify/LineItem/1"
FULFILLMENT_GID = "gid://shopify/Fulfillment/112233"


def orders_response(nodes=None, display_status="UNFULFILLED"):
    """An orderFulfillmentOrders response with one creatable fulfillment order.

    Five units remaining against a Delivery Note that ships two, i.e. the
    partial case the duplicate-fulfillment hazard needs.
    """
    if nodes is None:
        nodes = [fo(
            "gid://shopify/FulfillmentOrder/1",
            lines=[fo_line("gid://shopify/FulfillmentOrderLineItem/1",
                           LINE_GID, "SKU-A", 5)],
        )]
    return {"order": {
        "id": ORDER_GID,
        "name": "#6518",
        "displayFulfillmentStatus": display_status,
        "fulfillmentOrders": {"pageInfo": {"hasNextPage": False}, "nodes": nodes},
    }}


def fulfillment_created(fulfillment_gid=FULFILLMENT_GID, user_errors=None,
                        fulfillment=True):
    payload = {"userErrors": list(user_errors or [])}
    payload["fulfillment"] = (
        {"id": fulfillment_gid, "status": "SUCCESS"} if fulfillment else None
    )
    return {"fulfillmentCreate": payload}


def fulfillment_cancelled(user_errors=None):
    return {"fulfillmentCancel": {
        "fulfillment": {"id": FULFILLMENT_GID, "status": "CANCELLED"},
        "userErrors": list(user_errors or []),
    }}


class FulfilTestCase(unittest.TestCase):
    """Seeds one fulfillable Delivery Note and captures every GraphQL call."""

    def setUp(self):
        self._real_execute = f.execute
        self._real_get_doc = frappe.get_doc
        self.seed()

    def seed(self):
        frappe_stub.reset()
        frappe_stub.META_FIELDS["Delivery Note"] = {
            f.FULFILLMENT_ID_FIELD, f.FULFILLMENT_STATUS_FIELD,
            f.FULFILLED_AT_FIELD, f.FULFILLMENT_ERROR_FIELD,
        }

        self.settings = FakeSettings()
        frappe_stub.set_doc("Shopify Settings", "Test Store", {
            "name": "Test Store",
            "shop_domain": "notdrones.myshopify.com",
            "enable_sync": 1,
            "enable_fulfillment": 1,
            "notify_customer_on_fulfillment": 1,
        })
        frappe_stub.set_doc("Delivery Note", DN, {
            "name": DN,
            "docstatus": 1,
            "is_return": 0,
            f.FULFILLMENT_ID_FIELD: "",
            f.FULFILLMENT_STATUS_FIELD: "",
        })
        # _linked_shopify_order and wanted_lines_for_dn both go through
        # frappe.db.sql; the stub keys canned rows on a substring of the query.
        frappe_stub.SQL_RESULTS["so.shopify_order_id, so.shopify_store"] = [
            {"shopify_order_id": ORDER_ID,
             "shopify_store": "notdrones.myshopify.com"}
        ]
        frappe_stub.SQL_RESULTS["dni.item_code"] = [
            {"item_code": "SKU-A", "qty": 2, "line_item_id": LINE_GID}
        ]

        real_get_doc = self._real_get_doc
        frappe.get_doc = lambda dt, name=None, **k: (
            self.settings if dt == "Shopify Settings"
            else real_get_doc(dt, name, **k)
        )

        self.calls = []
        self.responses = [orders_response(), fulfillment_created()]

        def fake_execute(settings, query, variables=None, operation="", **kwargs):
            # **kwargs rather than a declared `idempotent=True`, deliberately.
            # These tests assert on what fulfillment.py actually PASSED, and a
            # default here would make "posted with the ordinary retries" and
            # "posted at most once" indistinguishable -- which is the whole of
            # the property under test.
            self.calls.append({"operation": operation, "query": query,
                               "variables": variables or {},
                               "kwargs": dict(kwargs)})
            if not self.responses:
                raise AssertionError(f"unexpected GraphQL call: {operation}")
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        f.execute = fake_execute

    def tearDown(self):
        f.execute = self._real_execute
        frappe.get_doc = self._real_get_doc

    # -- helpers -------------------------------------------------------------

    def stored(self, fieldname):
        return frappe_stub.get_doc_values("Delivery Note", DN).get(fieldname)

    def set_field(self, **values):
        frappe_stub.DB["Delivery Note"][DN].update(values)

    def posts_of(self, operation):
        return [c for c in self.calls if c["operation"] == operation]

    def kwargs_for(self, operation):
        """The keyword arguments fulfillment.py passed execute() for that call.

        `{}` when it passed none, so a test can tell "posted at most once" from
        "did not say" -- the difference between one post Shopify could have run
        and up to _MAX_ATTEMPTS of them.
        """
        calls = self.posts_of(operation)
        self.assertTrue(calls, f"no {operation} call was made")
        return calls[0]["kwargs"]

    def assertNoWarning(self):
        recorded = self.stored(f.FULFILLMENT_ERROR_FIELD) or ""
        self.assertNotIn(
            "POSSIBLY FULFILLED", recorded,
            "a failure that never posted the mutation was labelled possibly-sent",
        )


# -- How many times the mutation is posted ------------------------------------

class TestPostCount(FulfilTestCase):
    def test_fulfillment_create_is_posted_non_idempotently(self):
        """The one document here that must not be re-posted.

        A duplicate fulfillment also emails the customer a second tracking
        notification, because notify comes from
        notify_customer_on_fulfillment.
        """
        result = f.fulfil_delivery_note(DN)

        self.assertTrue(result["ok"], result)
        self.assertIs(
            self.kwargs_for("fulfillmentCreate").get("idempotent"), False,
            "fulfillmentCreate was posted with the ordinary retries",
        )

    def test_the_fulfillment_orders_query_keeps_its_retries(self):
        """A READ.  Re-posting it cannot create anything, so losing its
        throttle resilience would buy nothing."""
        f.fulfil_delivery_note(DN)
        self.assertEqual(self.kwargs_for("orderFulfillmentOrders"), {})

    def test_fulfillment_cancel_keeps_its_retries(self):
        """Effect-idempotent: cancelling an already-cancelled fulfillment
        reaches the same end state and answers with userErrors, so a retry
        cannot produce a second anything."""
        self.set_field(**{f.FULFILLMENT_ID_FIELD: FULFILLMENT_GID})
        self.responses = [fulfillment_cancelled()]

        result = f.cancel_fulfillment_for_dn(DN)

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.kwargs_for("fulfillmentCancel"), {})

    def test_a_transport_error_after_the_post_posts_once(self):
        """The duplicate-fulfillment case: the POST landed and only the ANSWER
        was lost, so a second post would fulfil the remaining quantity again."""
        self.responses = [
            orders_response(),
            ShopifyAPIError("fulfillmentCreate failed: timed out"),
        ]

        result = f.fulfil_delivery_note(DN)

        self.assertEqual(len(self.posts_of("fulfillmentCreate")), 1)
        self.assertEqual(result["status"], f.STATUS_FAILED)
        self.assertEqual(self.stored(f.FULFILLMENT_STATUS_FIELD), f.STATUS_FAILED)


class TestFiveHundredIsPostedOnce(FulfilTestCase):
    """
    The end-to-end version of the post count, through the REAL client.

    A fake `execute` can only pin what fulfillment.py passed; this pins what
    that argument does, so the wiring cannot rot while the kwarg assertion
    stays green.  Before idempotent=False a 5xx on fulfillmentCreate was
    re-posted to the attempt ceiling, and each of those posts could have
    created a fulfillment.
    """

    class _HttpResponse:
        def __init__(self, status_code=200, json_body=None, text=""):
            self.status_code = status_code
            self._json = json_body if json_body is not None else {}
            self.headers = {}
            self.text = text

        def json(self):
            return self._json

    def setUp(self):
        super().setUp()
        # The real client, faked at the socket instead.
        f.execute = self._real_execute

        self.posts = []
        self._http = [
            self._HttpResponse(200, {"data": orders_response()}),
        ] + [self._HttpResponse(500) for _ in range(_MAX_ATTEMPTS + 1)]

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
        super().tearDown()

    def _record_sleep(self, seconds):
        self._clock[0] += seconds

    def _post(self, url, headers=None, data=None, timeout=None, **kwargs):
        self.posts.append(json.loads(data)["query"])
        if not self._http:
            raise AssertionError("ran out of scripted HTTP responses")
        return self._http.pop(0)

    def test_a_5xx_is_posted_once_not_five(self):
        result = f.fulfil_delivery_note(DN)

        mutations = [q for q in self.posts if "fulfillmentCreate" in q]
        self.assertEqual(
            len(mutations), 1,
            "a 502 can be answered after the fulfillment committed, so every "
            "re-post could be a duplicate fulfillment",
        )
        self.assertEqual(result["status"], f.STATUS_FAILED)

    def test_the_5xx_row_says_the_fulfillment_may_exist(self):
        """The honest cost of the fix: a transient 5xx used to recover and now
        lands on Failed, so those rows appear MORE often -- and a person about
        to retry has to be told to look in Shopify first."""
        f.fulfil_delivery_note(DN)
        self.assertTrue(
            (self.stored(f.FULFILLMENT_ERROR_FIELD) or "").startswith(
                "POSSIBLY FULFILLED"
            ),
            self.stored(f.FULFILLMENT_ERROR_FIELD),
        )


# -- What the Delivery Note says before somebody retries ----------------------

class TestPossiblySentWarning(FulfilTestCase):
    """
    idempotent=False cuts the AUTOMATIC re-posts of a possibly-committed
    mutation from _MAX_ATTEMPTS to one.  It does not close the hole: every
    failure path still lands on STATUS_FAILED, which the scheduler and the
    Fulfil button both re-pick, so the remaining risk moves to the OUTER retry.
    Mitigating that is the error text's job.
    """

    def test_a_transport_error_after_the_post_warns_at_the_front(self):
        self.responses = [
            orders_response(),
            ShopifyAPIError("fulfillmentCreate failed: read timed out"),
        ]
        f.fulfil_delivery_note(DN)

        recorded = self.stored(f.FULFILLMENT_ERROR_FIELD) or ""
        self.assertTrue(recorded.startswith("POSSIBLY FULFILLED"), recorded)
        self.assertIn(ORDER_ID, recorded)
        self.assertIn("read timed out", recorded)
        self.assertEqual(self.stored(f.FULFILLMENT_STATUS_FIELD), f.STATUS_FAILED)

    def test_a_huge_upstream_error_cannot_push_the_warning_out_of_the_field(self):
        """_release_claim stores only the first 1000 characters, and the
        natural response to a truncated error message is to retry."""
        self.responses = [
            orders_response(),
            ShopifyAPIError(
                "fulfillmentCreate returned GraphQL errors: " + "x" * 4000
            ),
        ]
        f.fulfil_delivery_note(DN)

        recorded = self.stored(f.FULFILLMENT_ERROR_FIELD) or ""
        self.assertLessEqual(len(recorded), 1000)
        self.assertIn("POSSIBLY FULFILLED", recorded)
        self.assertIn("not retry", recorded.lower())

    def test_a_401_after_the_post_is_not_labelled_possibly_sent(self):
        """Shopify refused at the auth layer and never ran the document, so the
        row is plainly retryable -- and claiming otherwise would send a person
        hunting in Shopify for a fulfillment that cannot be there."""
        for status in (401, 403):
            with self.subTest(status=status):
                self.seed()
                self.responses = [
                    orders_response(),
                    ShopifyAPIError(
                        f"fulfillmentCreate returned HTTP {status}",
                        status,
                        proves_not_executed=True,
                    ),
                ]
                f.fulfil_delivery_note(DN)

                self.assertEqual(
                    self.stored(f.FULFILLMENT_STATUS_FIELD), f.STATUS_FAILED
                )
                self.assertNoWarning()

    def test_a_failure_before_the_post_is_not_labelled_possibly_sent(self):
        """The fulfillment-orders read failed, so nothing was ever posted."""
        self.responses = [
            ShopifyAPIError("orderFulfillmentOrders failed: timed out")
        ]
        f.fulfil_delivery_note(DN)

        self.assertEqual(self.posts_of("fulfillmentCreate"), [])
        self.assertEqual(self.stored(f.FULFILLMENT_STATUS_FIELD), f.STATUS_FAILED)
        self.assertNoWarning()

    def test_a_planning_refusal_is_not_labelled_possibly_sent(self):
        """No line matched, so the mutation was never built."""
        self.responses = [orders_response(nodes=[])]
        f.fulfil_delivery_note(DN)

        self.assertEqual(self.posts_of("fulfillmentCreate"), [])
        self.assertNoWarning()

    def test_user_errors_are_not_labelled_possibly_sent(self):
        """Shopify read the mutation and declined it, and with idempotent=False
        that answer describes the only post that could have run."""
        self.responses = [
            orders_response(),
            fulfillment_created(user_errors=[
                {"field": ["fulfillment"], "message": "Line items are invalid."}
            ]),
        ]
        f.fulfil_delivery_note(DN)

        self.assertEqual(self.stored(f.FULFILLMENT_STATUS_FIELD), f.STATUS_FAILED)
        self.assertNoWarning()

    def test_a_200_with_no_fulfillment_object_is_labelled_possibly_sent(self):
        """Shopify answered without complaining and without a fulfillment, so
        "nothing happened" is an assumption, not a fact."""
        self.responses = [orders_response(), fulfillment_created(fulfillment=False)]
        f.fulfil_delivery_note(DN)

        recorded = self.stored(f.FULFILLMENT_ERROR_FIELD) or ""
        self.assertTrue(recorded.startswith("POSSIBLY FULFILLED"), recorded)
        self.assertEqual(self.stored(f.FULFILLMENT_STATUS_FIELD), f.STATUS_FAILED)

    def test_a_successful_fulfillment_carries_no_warning(self):
        result = f.fulfil_delivery_note(DN)

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.stored(f.FULFILLMENT_ID_FIELD), FULFILLMENT_GID)
        self.assertNoWarning()

    def test_the_warning_stands_on_its_own(self):
        """It is the whole message when there is no detail to append."""
        warning = f._possibly_sent_warning(ORDER_ID)
        self.assertTrue(warning.startswith("POSSIBLY FULFILLED"))
        self.assertIn(ORDER_ID, warning)
        self.assertIn("not retry", warning.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
