"""
test_fulfillment.py — tests for the fulfillment planning logic.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_fulfillment -v

Covers the two places a fulfillment can go quietly wrong:

  * classify_fulfillment_orders — calling fulfillmentCreate on a fulfillment
    order that only supports REQUEST_FULFILLMENT
  * plan_fulfillment — over-fulfilling a line another Delivery Note already
    covered, or fulfilling the wrong line when one order carries the same SKU
    twice
"""

import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.utils import fulfillment as f  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
