"""
test_review_fixes.py — regression tests for the code-review findings.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_review_fixes -v

Each class pins one fixed bug so it cannot come back. The scenario each was
found under is in the docstring, because a test that only asserts the new
behaviour loses the reason it exists.
"""

import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.utils import fulfillment as f  # noqa: E402


def fo(fo_id, status="OPEN", actions=("CREATE_FULFILLMENT",), lines=(), location="Main"):
    return {
        "id": fo_id,
        "status": status,
        "supportedActions": [{"action": a} for a in actions],
        "assignedLocation": {"name": location},
        "lineItems": {"nodes": list(lines)},
    }


def fo_line(fo_line_id, line_item_id, sku, remaining):
    return {
        "id": fo_line_id,
        "remainingQuantity": remaining,
        "totalQuantity": remaining,
        "sku": sku,
        "lineItem": {"id": line_item_id, "sku": sku},
    }


def want(sku, qty, line_item_id=""):
    return {"line_item_id": line_item_id, "sku": sku, "qty": qty}


# ── Finding 1 ─────────────────────────────────────────────────────────────────

class TestResolvedStatusIsNotRetried(unittest.TestCase):
    """
    An order fulfilled by hand in Shopify leaves the Delivery Note status
    Fulfilled with a BLANK fulfillment id.  The scheduler used to select on
    "status != Pending", so it re-picked that document every hour forever,
    spending a GraphQL query each time to rediscover the same answer.
    """

    def setUp(self):
        frappe_stub.reset()

    def test_retryable_statuses_are_a_whitelist(self):
        self.assertIn("", f.RETRYABLE_STATUSES)
        self.assertIn(f.STATUS_FAILED, f.RETRYABLE_STATUSES)

    def test_resolved_statuses_are_not_retryable(self):
        for status in (f.STATUS_FULFILLED, f.STATUS_PARTIAL, f.STATUS_CANCELLED):
            self.assertNotIn(status, f.RETRYABLE_STATUSES, status)

    def test_pending_is_not_in_the_whitelist(self):
        """Pending is handled by the separate stale-claim branch, not this list."""
        self.assertNotIn(f.STATUS_PENDING, f.RETRYABLE_STATUSES)

    def _eligibility_for(self, status):
        frappe_stub.META_FIELDS["Delivery Note"] = {
            f.FULFILLMENT_ID_FIELD, f.FULFILLMENT_STATUS_FIELD,
        }
        frappe_stub.set_doc("Delivery Note", "DN-0001", {
            "docstatus": 1,
            "is_return": 0,
            f.FULFILLMENT_ID_FIELD: "",
            f.FULFILLMENT_STATUS_FIELD: status,
        })
        return f.check_eligibility("DN-0001")

    def test_eligibility_refuses_a_fulfilled_dn_with_no_id(self):
        out = self._eligibility_for(f.STATUS_FULFILLED)
        self.assertFalse(out["ok"])
        self.assertIn("Already resolved", out["reason"])

    def test_eligibility_refuses_partially_fulfilled_and_cancelled(self):
        for status in (f.STATUS_PARTIAL, f.STATUS_CANCELLED):
            out = self._eligibility_for(status)
            self.assertFalse(out["ok"], status)
            self.assertIn("Already resolved", out["reason"])

    def test_eligibility_still_allows_a_failed_dn(self):
        """Failed must stay retryable — that is how transient errors self-heal."""
        out = self._eligibility_for(f.STATUS_FAILED)
        self.assertNotIn("Already resolved", out["reason"])


# ── Finding 4 ─────────────────────────────────────────────────────────────────

class TestSkuFallback(unittest.TestCase):
    """
    plan_fulfillment documented "by line item id first, by SKU as a fallback"
    but never tried SKU once an id was present.  A Shopify-side edit replaces a
    line and issues a new line_item.id, so the stored id stops matching and a
    fulfillable Delivery Note was refused.
    """

    def setUp(self):
        frappe_stub.reset()

    def test_falls_back_to_sku_when_the_stored_id_is_stale(self):
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/NEW", "SKU-A", 2)])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 2, "li/OLD")])

        self.assertEqual(plan["allocated"], 2)
        self.assertEqual(plan["unallocated"], [])
        rows = plan["line_items_by_fulfillment_order"][0]["fulfillmentOrderLineItems"]
        self.assertEqual(rows, [{"id": "fol/1", "quantity": 2}])

    def test_exact_id_still_wins_over_sku(self):
        """The fallback must not weaken same-SKU-twice disambiguation."""
        orders = [fo("fo/1", lines=[
            fo_line("fol/1", "li/1", "SKU-A", 1),
            fo_line("fol/2", "li/2", "SKU-A", 1),
        ])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 1, "li/2")])

        rows = plan["line_items_by_fulfillment_order"][0]["fulfillmentOrderLineItems"]
        self.assertEqual(rows, [{"id": "fol/2", "quantity": 1}])

    def test_no_id_and_no_sku_match_is_still_unallocated(self):
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 1)])]
        plan = f.plan_fulfillment(orders, [want("SKU-ZZZ", 1, "li/999")])

        self.assertEqual(plan["allocated"], 0)
        self.assertIn("no matching open", plan["unallocated"][0]["reason"])

    def test_blank_sku_with_stale_id_does_not_match_everything(self):
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 1)])]
        plan = f.plan_fulfillment(orders, [want("", 1, "li/999")])
        self.assertEqual(plan["allocated"], 0)


# ── Finding 5 ─────────────────────────────────────────────────────────────────

class TestLineItemCap(unittest.TestCase):
    """
    rows[:512] truncated the payload after `allocated` had already counted the
    dropped units, so a truncated fulfillment reported full success and the
    Delivery Note was marked Fulfilled while Shopify still showed units
    outstanding.
    """

    def setUp(self):
        frappe_stub.reset()

    def _oversized_plan(self):
        n = f._MAX_LINE_ITEMS + 5
        lines = [fo_line("fol/%d" % i, "li/%d" % i, "SKU-%d" % i, 1) for i in range(n)]
        wanted = [want("SKU-%d" % i, 1, "li/%d" % i) for i in range(n)]
        return f.plan_fulfillment([fo("fo/1", lines=lines)], wanted)

    def test_payload_respects_the_cap(self):
        plan = self._oversized_plan()
        rows = plan["line_items_by_fulfillment_order"][0]["fulfillmentOrderLineItems"]
        self.assertEqual(len(rows), f._MAX_LINE_ITEMS)

    def test_allocated_matches_what_is_actually_sent(self):
        """The bug: allocated counted units the payload never carried."""
        plan = self._oversized_plan()
        sent = sum(
            r["quantity"]
            for g in plan["line_items_by_fulfillment_order"]
            for r in g["fulfillmentOrderLineItems"]
        )
        self.assertEqual(plan["allocated"], sent)

    def test_overflow_is_reported_so_the_status_becomes_partial(self):
        plan = self._oversized_plan()
        self.assertTrue(plan["unallocated"], "overflow must be visible, not silent")
        self.assertTrue(
            any("maximum" in (u.get("reason") or "") for u in plan["unallocated"])
        )

    def test_normal_sized_order_is_unaffected(self):
        orders = [fo("fo/1", lines=[fo_line("fol/1", "li/1", "SKU-A", 3)])]
        plan = f.plan_fulfillment(orders, [want("SKU-A", 3, "li/1")])
        self.assertEqual(plan["allocated"], 3)
        self.assertEqual(plan["unallocated"], [])


# ── Finding 2 ─────────────────────────────────────────────────────────────────

class TestGraphqlEvictsToken(unittest.TestCase):
    """
    shopify_api.get() evicted the cached minted token on 401/403 but
    shopify_graphql.execute() did not, so a rotated Client Secret left
    fulfillment failing for the ~24h cache TTL while REST recovered at once.
    """

    def test_graphql_module_calls_invalidate_cached_token(self):
        from shopify_integration.utils import shopify_graphql

        self.assertTrue(
            hasattr(shopify_graphql, "invalidate_cached_token"),
            "shopify_graphql must import invalidate_cached_token",
        )

    def test_both_clients_evict_on_401(self):
        import inspect

        from shopify_integration.utils import shopify_api, shopify_graphql

        for mod in (shopify_api.get, shopify_graphql.execute):
            src = inspect.getsource(mod)
            self.assertIn(
                "invalidate_cached_token", src,
                "%s must evict the cached token on an auth failure" % mod.__name__,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
