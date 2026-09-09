"""
test_refund_headroom.py — where "how much is left to refund" comes from.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_refund_headroom -v

Written after the defect the diagnostic finally exposed on production
(2026-09-09).  `RefundTargets` asked for `maximumRefundableV2` inside
`order { transactions { ... } }`, and Shopify's own schema — 2026-01 included —
says of that field:

    "Specifies the available amount with currency to refund on the gateway.
     This value is only available for transactions of type `SuggestedRefund`."

Read off `order.transactions` it is **always null**.  `_paise(None)` is 0, so
every parent scored zero headroom, on every order, on every store: no refund
this app has ever attempted could pass its own gate, and each one presented as
an order that had already been fully refunded.

Two production orders proved it, and they are the two cases pinned below:

  * `EB3494` (REF-00207, ₹5, gateway `manual`) — one SALE row, no refunds, and
    the Shopify admin says "₹5.00 available for refund".  We reported
    *not reported* and refused.
  * `EB3484` (₹999.80, gateway `Cashfree Payments`) — a SALE and its REFUND,
    genuinely settled.  We reported *not reported* on both, i.e. the same
    nothing for an order with headroom and an order without.  Reporting the
    same answer either way is the whole tell.

So headroom now comes from up to three places, and the lowest reported one
wins:

  suggested_refund  `order.suggestedRefund.suggestedTransactions[]
                    .maximumRefundableSet` — Shopify's own figure, and the one
                    the admin shows.  Note the name: `maximumRefundableSet`, a
                    MoneyBag on `SuggestedOrderTransaction`, NOT
                    `maximumRefundableV2`.
  transaction       `maximumRefundableV2`, kept because it is in the schema and
                    costs nothing.  Null in practice; if Shopify ever fills it
                    we will use it.
  derived           this app's own arithmetic: the parent's `amountSet` less
                    every SUCCESS REFUND/VOID booked against it.  No new field,
                    no new failure mode, and on both orders above it reproduces
                    the admin exactly (5.00 and 0.00).

Over-reporting headroom is not the money risk it looks like — Shopify enforces
the real limit on `refundCreate` and refuses over it, which lands as
`rejected_by_shopify`/`failed_unsent` with nobody paid.  Under-reporting only
refuses.  So the lowest figure wins, and no figure at all still means zero.
"""

import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.tests.test_refund_writeback import (  # noqa: E402
    ORDER_GID,
    REFUND,
    WritebackTestCase,
    refund_created,
    targets_response,
)
from shopify_integration.utils import refund as r  # noqa: E402
from shopify_integration.utils.shopify_api import ShopifyAPIError  # noqa: E402


# ── Fixtures shaped like the two live orders ─────────────────────────────────

def node(txn_id="gid://shopify/OrderTransaction/1", kind="SALE", status="SUCCESS",
         gateway="manual", amount="5.00", parent=None, refundable="absent"):
    """An OrderTransaction as `order.transactions` really returns it.

    `refundable="absent"` is the live shape: no `maximumRefundableV2` key at
    all, which is what Shopify sends outside a SuggestedRefund.
    """
    row = {
        "id": txn_id,
        "kind": kind,
        "status": status,
        "gateway": gateway,
        "formattedGateway": (gateway or "").title(),
        "amountSet": {"presentmentMoney": {"amount": amount, "currencyCode": "INR"}},
        "parentTransaction": {"id": parent} if parent else None,
    }
    if refundable != "absent":
        row["maximumRefundableV2"] = (
            None if refundable is None
            else {"amount": refundable, "currencyCode": "INR"}
        )
    return row


def suggestion(parent="gid://shopify/OrderTransaction/1", refundable="5.00",
               gateway="manual", amount="5.00"):
    """One `suggestedTransactions` entry, as SuggestedOrderTransaction."""
    return {
        "kind": "SUGGESTED_REFUND",
        "gateway": gateway,
        "formattedGateway": (gateway or "").title(),
        "amountSet": {"presentmentMoney": {"amount": amount, "currencyCode": "INR"}},
        "maximumRefundableSet": (
            None if refundable is None
            else {"presentmentMoney": {"amount": refundable, "currencyCode": "INR"}}
        ),
        "parentTransaction": {"id": parent} if parent else None,
    }


def order(transactions, suggested=None):
    return {"id": ORDER_GID, "name": "#EB3494", "transactions": transactions,
            "suggestedRefund": (
                {"suggestedTransactions": suggested} if suggested is not None else None
            )}


EB3494 = order([node(amount="5.00")], [suggestion(refundable="5.00")])

EB3484 = order([
    node("gid://shopify/OrderTransaction/71", amount="999.80",
         gateway="Cashfree Payments"),
    node("gid://shopify/OrderTransaction/72", kind="REFUND", amount="999.80",
         gateway="Cashfree Payments", parent="gid://shopify/OrderTransaction/71"),
])


# ── The two live orders ──────────────────────────────────────────────────────

class TestTheTwoProductionOrders(unittest.TestCase):

    def test_eb3494_is_refundable_and_says_where_the_figure_came_from(self):
        """The refusal that started this.  Shopify's admin offers ₹5.00 and so,
        now, do we."""
        nodes = r.with_headroom(EB3494)
        plan = r.plan_refund(nodes, 5.0)

        self.assertIsNone(plan["problem"], plan)
        self.assertEqual(plan["transactions"][0]["amount"], "5.00")

        row = r.transaction_summary(nodes)[0]
        self.assertEqual(row["refundable"], "5.00")
        self.assertTrue(row["refundable_reported"])
        self.assertEqual(row["refundable_source"], r.HEADROOM_FROM_SUGGESTION)
        self.assertEqual(row["rejected_because"], "")

    def test_eb3494_is_refundable_from_arithmetic_alone(self):
        """Shopify offering no suggestion must not put us back where we were:
        one SALE of 5.00 with nothing refunded against it has 5.00 left."""
        nodes = r.with_headroom(order([node(amount="5.00")]))
        self.assertIsNone(r.plan_refund(nodes, 5.0)["problem"])
        self.assertEqual(r.transaction_summary(nodes)[0]["refundable_source"],
                         r.HEADROOM_DERIVED)

    def test_eb3484_is_settled_and_reads_zero_rather_than_unknown(self):
        """The order the write-back must still refuse — and the one whose
        old answer was indistinguishable from EB3494's."""
        nodes = r.with_headroom(EB3484)
        sale, refunded = r.transaction_summary(nodes)

        self.assertEqual(sale["refundable"], "0.00")
        self.assertEqual(sale["rejected_because"], "no_headroom")
        self.assertTrue(sale["refundable_reported"],
                        "0.00 known is not the same as nothing known")
        self.assertEqual(sale["refundable_source"], r.HEADROOM_DERIVED)
        self.assertEqual(refunded["rejected_because"], "kind")

        plan = r.plan_refund(nodes, 999.8)
        self.assertEqual(plan["problem_code"], "no_refundable_transactions")

    def test_the_two_orders_no_longer_give_the_same_answer(self):
        """The defect in one line: with headroom and without, both read
        'not reported'."""
        self.assertNotEqual(
            r.transaction_summary(r.with_headroom(EB3494))[0]["refundable"],
            r.transaction_summary(r.with_headroom(EB3484))[0]["refundable"],
        )


# ── Where the figure comes from ──────────────────────────────────────────────

class TestHeadroomSources(unittest.TestCase):

    def test_the_lowest_reported_source_wins(self):
        nodes = r.with_headroom(order(
            [node(amount="1000.00", refundable="500.00")],
            [suggestion(refundable="200.00", amount="200.00")],
        ))
        self.assertEqual(r.transaction_summary(nodes)[0]["refundable"], "200.00")

    def test_arithmetic_can_win_over_a_generous_suggestion(self):
        """Not expected from Shopify, and the safe direction if it happens."""
        nodes = r.with_headroom(order(
            [node("t/1", amount="1000.00"),
             node("t/2", kind="REFUND", amount="600.00", parent="t/1")],
            [suggestion(parent="t/1", refundable="900.00")],
        ))
        row = r.transaction_summary(nodes)[0]
        self.assertEqual(row["refundable"], "400.00")
        self.assertEqual(row["refundable_source"], r.HEADROOM_DERIVED)

    def test_a_partial_refund_leaves_the_difference(self):
        nodes = r.with_headroom(order([
            node("t/1", amount="1000.00"),
            node("t/2", kind="REFUND", amount="300.00", parent="t/1"),
        ]))
        self.assertEqual(r.transaction_summary(nodes)[0]["refundable"], "700.00")

    def test_a_refund_with_no_parent_still_reduces_the_pool(self):
        """A REFUND row we cannot attribute is money that has gone back all the
        same.  Left out of the arithmetic it would invite a second payout, so it
        comes off the largest parent."""
        nodes = r.with_headroom(order([
            node("t/1", amount="1000.00"),
            node("t/2", kind="REFUND", amount="300.00", parent=None),
        ]))
        self.assertEqual(r.transaction_summary(nodes)[0]["refundable"], "700.00")

    def test_an_unattributed_refund_spills_across_parents_largest_first(self):
        nodes = r.with_headroom(order([
            node("t/1", amount="100.00"),
            node("t/2", amount="1000.00"),
            node("t/3", kind="REFUND", amount="1050.00", parent=None),
        ]))
        by_id = {n["id"]: r._headroom(n) for n in nodes if n["kind"] == "SALE"}
        self.assertEqual(by_id["t/2"], 0)
        self.assertEqual(by_id["t/1"], 5000)  # 100.00 less the 50.00 spill

    def test_a_void_reduces_headroom_too(self):
        nodes = r.with_headroom(order([
            node("t/1", amount="1000.00"),
            node("t/2", kind="VOID", amount="1000.00", parent="t/1"),
        ]))
        self.assertEqual(r.transaction_summary(nodes)[0]["refundable"], "0.00")

    def test_an_unsuccessful_refund_gives_nothing_back(self):
        nodes = r.with_headroom(order([
            node("t/1", amount="1000.00"),
            node("t/2", kind="REFUND", status="FAILURE", amount="300.00",
                 parent="t/1"),
        ]))
        self.assertEqual(r.transaction_summary(nodes)[0]["refundable"], "1000.00")

    def test_a_suggestion_matches_by_parent_id_and_not_by_position(self):
        nodes = r.with_headroom(order(
            [node("t/1", amount="100.00"), node("t/2", amount="200.00")],
            [suggestion(parent="t/2", refundable="7.00")],
        ))
        rows = {row["id"]: row for row in r.transaction_summary(nodes)}
        self.assertEqual(rows["t/2"]["refundable"], "7.00")
        self.assertEqual(rows["t/2"]["refundable_source"], r.HEADROOM_FROM_SUGGESTION)
        self.assertEqual(rows["t/1"]["refundable_source"], r.HEADROOM_DERIVED)

    def test_a_suggestion_without_a_figure_is_not_a_figure(self):
        nodes = r.with_headroom(order(
            [node(amount="5.00")], [suggestion(refundable=None)],
        ))
        self.assertEqual(r.transaction_summary(nodes)[0]["refundable_source"],
                         r.HEADROOM_DERIVED)

    def test_a_transaction_field_is_used_when_shopify_ever_fills_it(self):
        nodes = r.with_headroom(order([node(amount="1000.00", refundable="600.00")]))
        row = r.transaction_summary(nodes)[0]
        self.assertEqual(row["refundable"], "600.00")
        self.assertEqual(row["refundable_source"], r.HEADROOM_FROM_TRANSACTION)

    def test_nothing_to_go_on_at_all_is_zero_and_says_so(self):
        """No amount, no field, no suggestion.  Refusing on a number we do not
        have is still the safe direction — it just has to be legible."""
        bare = {"id": "t/1", "kind": "SALE", "status": "SUCCESS", "gateway": "manual"}
        row = r.transaction_summary(r.with_headroom(order([bare])))[0]
        self.assertEqual(row["refundable"], "0.00")
        self.assertFalse(row["refundable_reported"])
        self.assertEqual(row["refundable_source"], r.HEADROOM_NONE)
        self.assertEqual(row["rejected_because"], "no_headroom")

    def test_the_response_is_not_mutated(self):
        """The order dict is evidence: it goes into the log and into the
        diagnostic, and it must read as what Shopify said."""
        payload = order([node(amount="5.00")], [suggestion()])
        r.with_headroom(payload)
        self.assertEqual(payload["transactions"][0].keys(),
                         node(amount="5.00").keys())

    def test_an_order_with_no_transactions_is_still_the_empty_case(self):
        for payload in (order([]), order(None), {}, None):
            self.assertEqual(r.with_headroom(payload), [])

    def test_the_connection_shape_still_works(self):
        nodes = r.with_headroom(
            {"transactions": {"edges": [{"node": node(amount="5.00")}]}}
        )
        self.assertEqual(r.transaction_summary(nodes)[0]["refundable"], "5.00")


# ── The documents ────────────────────────────────────────────────────────────

class TestTheQueryAsksForTheRightField(unittest.TestCase):

    def test_it_asks_for_the_suggested_refund_figure(self):
        self.assertIn("suggestedRefund", r._REFUND_TARGETS_QUERY)
        self.assertIn("maximumRefundableSet", r._REFUND_TARGETS_QUERY)
        self.assertIn("suggestedTransactions", r._REFUND_TARGETS_QUERY)

    def test_it_still_asks_for_the_transaction_field(self):
        """Free, in the schema, and the moment Shopify fills it we want it."""
        self.assertIn("maximumRefundableV2", r._REFUND_TARGETS_QUERY)

    def test_the_narrow_document_drops_only_the_suggestion(self):
        self.assertNotIn("suggestedRefund", r._REFUND_TARGETS_NARROW_QUERY)
        self.assertIn("transactions", r._REFUND_TARGETS_NARROW_QUERY)

    def test_it_reads_enough_transactions_for_the_arithmetic_to_be_right(self):
        """`first:` truncates the array (Shopify's word for it — order
        .transactions is a plain list, not a connection), and derived headroom
        is only correct if every REFUND row on the order is in it.  Truncation
        used to cost us parents we could not use anyway; it can now hide money
        already given back, which is the direction that matters.
        """
        for document in (r._REFUND_TARGETS_QUERY, r._REFUND_TARGETS_NARROW_QUERY):
            self.assertIn("transactions(first: 100)", document)

    def test_neither_document_can_refund_anybody(self):
        for document in (r._REFUND_TARGETS_QUERY, r._REFUND_TARGETS_NARROW_QUERY):
            self.assertNotIn("refundCreate", document)
            self.assertNotIn("mutation", document)


# ── Through the payout ───────────────────────────────────────────────────────

class TestTheWriteBackUsesIt(WritebackTestCase):

    def test_an_order_shopify_reports_no_transaction_headroom_for_still_sends(self):
        """The regression, end to end: before this, every order on every store
        refused here."""
        self.responses = [
            targets_response(
                transactions=[node("t/99", amount="12999.00", gateway="manual")],
                suggested=[suggestion(parent="t/99", refundable="12999.00",
                                      amount="12999.00")],
            ),
            refund_created(),
        ]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_PAID, result)
        self.assertEqual(len(self.mutations), 1)

    def test_arithmetic_alone_is_enough_to_send(self):
        self.responses = [
            targets_response(
                transactions=[node("t/99", amount="12999.00", gateway="manual")]
            ),
            refund_created(),
        ]
        self.assertEqual(r.write_back_refund(REFUND)["outcome"], r.OUTCOME_PAID)

    def test_a_settled_order_is_still_refused_before_the_mutation(self):
        """EB3484 through the payout.  The fix must not turn a refunded order
        into a second payout."""
        self.responses = [targets_response(transactions=[
            node("t/99", amount="12999.00"),
            node("t/98", kind="REFUND", amount="12999.00", parent="t/99"),
        ])]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)
        self.assertEqual(result["reason_code"], "no_refundable_transactions")
        self.assertNothingSent()

    def test_a_partly_refunded_order_is_short_and_says_so(self):
        self.responses = [targets_response(transactions=[
            node("t/99", amount="12999.00"),
            node("t/98", kind="REFUND", amount="1.00", parent="t/99"),
        ])]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["reason_code"], "insufficient_refundable")
        self.assertNothingSent()

    def test_the_refusal_records_the_source_of_every_figure(self):
        """A refusal that cannot be explained is what produced this whole
        investigation."""
        self.responses = [targets_response(transactions=[
            node("t/99", amount="12999.00"),
            node("t/98", kind="REFUND", amount="12999.00", parent="t/99"),
        ])]
        r.write_back_refund(REFUND)
        logged = [d for d in frappe_stub.INSERTS if d.get("doctype") == "Shopify Log"]
        self.assertIn("derived", str(logged))

    def test_one_query_and_one_mutation_and_nothing_else(self):
        self.responses = [
            targets_response(
                transactions=[node("t/99", amount="12999.00")],
                suggested=[suggestion(parent="t/99", refundable="12999.00")],
            ),
            refund_created(),
        ]
        r.write_back_refund(REFUND)
        self.assertEqual([call["operation"] for call in self.calls],
                         ["RefundTargets", "refundCreate"])


class TestTheSuggestionBlockIsNotLoadBearing(WritebackTestCase):
    """A query-level rejection of the new block must degrade to arithmetic, not
    take the payout down with it.

    The lesson from the field: reading a field that does not exist fails the
    WHOLE document, and that is worse than the wrong number it replaced.  So the
    document that can be rejected is retried without the part that can be
    rejected, once, and only for a rejection of the document itself.
    """

    def rejected_document(self):
        return ShopifyAPIError(
            "Field 'suggestedRefund' doesn't exist on type 'Order'",
            status_code=200,
        )

    def test_a_rejected_document_falls_back_to_the_narrow_one(self):
        self.responses = [
            self.rejected_document(),
            targets_response(transactions=[node("t/99", amount="12999.00")]),
            refund_created(),
        ]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_PAID, result)
        self.assertEqual([call["operation"] for call in self.calls],
                         ["RefundTargets", "RefundTargets", "refundCreate"])
        self.assertNotIn("suggestedRefund", self.calls[1]["query"])

    def test_the_fallback_is_a_read_and_happens_at_most_once(self):
        self.responses = [self.rejected_document(), self.rejected_document()]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["reason_code"], "query_failed")
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)
        self.assertFalse(result["possibly_paid"])
        self.assertNothingSent()
        self.assertEqual(len(self.calls), 2)

    def test_a_throttle_is_not_re_asked(self):
        """Shopify refusing on cost is not the document being wrong, and asking
        again immediately is the one thing that makes a throttle worse."""
        self.responses = [ShopifyAPIError("throttled", status_code=200,
                                          error_codes=["THROTTLED"])]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["reason_code"], "query_failed")
        self.assertEqual(len(self.calls), 1)

    def test_a_transport_failure_is_not_re_asked(self):
        self.responses = [ShopifyAPIError("connection reset")]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["reason_code"], "query_failed")
        self.assertEqual(len(self.calls), 1)


# ── Through the diagnostic ───────────────────────────────────────────────────

class TestTheDiagnosticExplainsTheFigure(WritebackTestCase):

    def test_it_names_the_source_for_every_row(self):
        self.responses = [targets_response(
            transactions=[node("t/99", amount="12999.00")],
            suggested=[suggestion(parent="t/99", refundable="12999.00")],
        )]
        info = r.refund_targets_now(REFUND)

        self.assertTrue(info["ok"], info)
        self.assertEqual(info["refundable_total"], 12999.0)
        self.assertEqual(info["transactions"][0]["refundable_source"],
                         r.HEADROOM_FROM_SUGGESTION)
        self.assertEqual(info["would_refuse_with"], "")

    def test_it_says_when_shopify_offered_no_suggestion(self):
        self.responses = [targets_response(
            transactions=[node("t/99", amount="12999.00")]
        )]
        info = r.refund_targets_now(REFUND)

        self.assertFalse(info["suggestion_available"])
        self.assertEqual(info["transactions"][0]["refundable_source"],
                         r.HEADROOM_DERIVED)

    def test_it_reports_shopifys_own_order_level_figure(self):
        """`SuggestedRefund.maximumRefundableSet` is what the admin prints as
        "available for refund" for the whole order.  Reported, never spent: it
        is the number to compare ours against when they disagree, and capping by
        it would refuse orders whose refundable money is not in line items."""
        self.responses = [targets_response(
            transactions=[node("t/99", amount="12999.00")],
            suggested=[suggestion(parent="t/99", refundable="12999.00")],
            suggested_total="12999.00",
        )]
        info = r.refund_targets_now(REFUND)
        self.assertEqual(info["order_refundable_total"], 12999.0)

    def test_the_order_level_figure_is_none_when_shopify_sent_none(self):
        self.responses = [targets_response(
            transactions=[node("t/99", amount="12999.00")]
        )]
        self.assertIsNone(r.refund_targets_now(REFUND)["order_refundable_total"])

    def test_it_still_posts_no_mutation(self):
        self.responses = [targets_response(
            transactions=[node("t/99", amount="12999.00")],
            suggested=[suggestion(parent="t/99", refundable="12999.00")],
        )]
        r.refund_targets_now(REFUND)
        self.assertNothingSent()
        self.assertEqual([call["operation"] for call in self.calls],
                         ["RefundTargets"])

    def test_it_degrades_the_same_way_the_payout_does(self):
        self.responses = [
            ShopifyAPIError("Field 'suggestedRefund' doesn't exist on type 'Order'",
                            status_code=200),
            targets_response(transactions=[node("t/99", amount="12999.00")]),
        ]
        info = r.refund_targets_now(REFUND)

        self.assertTrue(info["ok"], info)
        self.assertFalse(info["suggestion_available"])
        self.assertEqual(info["refundable_total"], 12999.0)


if __name__ == "__main__":
    unittest.main()
