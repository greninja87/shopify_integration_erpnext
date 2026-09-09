"""
test_refund.py — tests for the refund write-back planning logic.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_refund -v

Covers the pure half of utils/refund.py, which is where the correctness lives:

  * transaction_nodes    — order.transactions and refund.transactions do not
    agree on their shape, and guessing wrong reads as "no transactions"
  * refundable_parents   — refunding against a REFUND row, a VOID or an
    uncaptured AUTHORIZATION
  * plan_refund          — over-refunding a parent past maximumRefundableV2, or
    writing back a partial amount that looks settled and is not
  * build_refund_input   — restocking by accident, and losing the reason
"""

import inspect
import unittest
from pathlib import Path

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.utils import refund as r  # noqa: E402


def txn(
    txn_id="gid://shopify/OrderTransaction/1",
    kind="SALE",
    status="SUCCESS",
    gateway="manual",
    amount="12999.00",
    refundable=None,
    parent=None,
):
    """Build an OrderTransaction node as the RefundTargets query returns it."""
    return {
        "id": txn_id,
        "kind": kind,
        "status": status,
        "gateway": gateway,
        "formattedGateway": (gateway or "").title(),
        "amountSet": {"presentmentMoney": {"amount": amount, "currencyCode": "INR"}},
        "maximumRefundableV2": {
            "amount": amount if refundable is None else refundable,
            "currencyCode": "INR",
        },
        "parentTransaction": {"id": parent} if parent else None,
    }


def order_6518_transactions():
    """The #6518 shape: an order marked paid manually, so Shopify holds no
    gateway transaction of its own and the parent reads "manual".  That says
    nothing about whether the customer gets paid — on OCC orders they do."""
    return [txn(kind="SALE", gateway="manual", amount="12999.00")]


# ── Shape tolerance ───────────────────────────────────────────────────────────

class TestTransactionNodes(unittest.TestCase):
    """order.transactions takes `first:` but appears to return a plain list,
    while refund.transactions on the mutation result is a connection with
    edges/node.  Both forms appear in the official examples, so both are
    accepted rather than one being assumed.
    """

    def test_plain_list(self):
        nodes = r.transaction_nodes([txn("t/1"), txn("t/2")])
        self.assertEqual([n["id"] for n in nodes], ["t/1", "t/2"])

    def test_nodes_connection(self):
        nodes = r.transaction_nodes({"nodes": [txn("t/1")]})
        self.assertEqual([n["id"] for n in nodes], ["t/1"])

    def test_edges_connection(self):
        nodes = r.transaction_nodes({"edges": [{"node": txn("t/1")}, {"node": txn("t/2")}]})
        self.assertEqual([n["id"] for n in nodes], ["t/1", "t/2"])

    def test_empty_and_garbage(self):
        self.assertEqual(r.transaction_nodes(None), [])
        self.assertEqual(r.transaction_nodes([]), [])
        self.assertEqual(r.transaction_nodes({}), [])
        self.assertEqual(r.transaction_nodes("nope"), [])
        self.assertEqual(r.transaction_nodes({"edges": [None, {"node": None}]}), [])
        self.assertEqual(r.transaction_nodes(["nope", None]), [])


# ── Refundable parents ────────────────────────────────────────────────────────

class TestRefundableParents(unittest.TestCase):
    def test_sale_and_capture_are_parents(self):
        parents = r.refundable_parents([txn("t/1", kind="SALE"), txn("t/2", kind="CAPTURE")])
        self.assertEqual({p["id"] for p in parents}, {"t/1", "t/2"})

    def test_refund_row_is_not_a_parent(self):
        """A REFUND row is the result of a refund, never something to refund."""
        self.assertEqual(r.refundable_parents([txn(kind="REFUND")]), [])

    def test_void_is_not_a_parent(self):
        self.assertEqual(r.refundable_parents([txn(kind="VOID")]), [])

    def test_authorization_is_not_a_parent(self):
        """Authorized but not captured — no money to give back."""
        self.assertEqual(r.refundable_parents([txn(kind="AUTHORIZATION")]), [])

    def test_failed_status_is_not_a_parent(self):
        self.assertEqual(r.refundable_parents([txn(status="FAILURE")]), [])
        self.assertEqual(r.refundable_parents([txn(status="PENDING")]), [])

    def test_zero_headroom_is_not_a_parent(self):
        """Already fully refunded: the row is still SALE/SUCCESS, and useless."""
        self.assertEqual(r.refundable_parents([txn(refundable="0.00")]), [])
        self.assertEqual(r.refundable_parents([txn(refundable="0")]), [])

    def test_missing_maximum_refundable_is_not_a_parent(self):
        node = txn()
        node["maximumRefundableV2"] = None
        self.assertEqual(r.refundable_parents([node]), [])

    def test_kind_and_status_are_case_insensitive(self):
        parents = r.refundable_parents([txn(kind="sale", status="success")])
        self.assertEqual(len(parents), 1)

    def test_best_first_is_largest_headroom_first(self):
        parents = r.refundable_parents([
            txn("t/small", refundable="100.00"),
            txn("t/big", refundable="9000.00"),
            txn("t/mid", refundable="500.00"),
        ])
        self.assertEqual([p["id"] for p in parents], ["t/big", "t/mid", "t/small"])

    def test_mixed_set_keeps_only_the_parents(self):
        parents = r.refundable_parents([
            txn("t/1", kind="SALE"),
            txn("t/2", kind="REFUND"),
            txn("t/3", kind="VOID"),
            txn("t/4", kind="CAPTURE", refundable="0.00"),
        ])
        self.assertEqual([p["id"] for p in parents], ["t/1"])

    def test_accepts_a_connection_not_just_a_list(self):
        parents = r.refundable_parents({"edges": [{"node": txn("t/1")}]})
        self.assertEqual([p["id"] for p in parents], ["t/1"])

    def test_garbage_rows_ignored(self):
        parents = r.refundable_parents(["nope", None, txn("t/1")])
        self.assertEqual([p["id"] for p in parents], ["t/1"])


# ── Planning ──────────────────────────────────────────────────────────────────

class TestPlanRefund(unittest.TestCase):
    def test_single_parent_exact_amount(self):
        plan = r.plan_refund([txn("t/1", refundable="12999.00")], 12999.0)

        self.assertIsNone(plan["problem"])
        self.assertEqual(plan["allocated"], 12999.0)
        self.assertEqual(plan["transactions"], [{
            "parentId": "t/1",
            "kind": "REFUND",
            "gateway": "manual",
            "amount": "12999.00",
        }])

    def test_caps_at_maximum_refundable_not_at_the_charged_amount(self):
        """amountSet says 12999 was taken; maximumRefundableV2 says only 5000 is
        left because 7999 was already refunded.  The cap is the latter."""
        plan = r.plan_refund(
            [txn("t/1", amount="12999.00", refundable="5000.00")], 5000.0
        )
        self.assertIsNone(plan["problem"])
        self.assertEqual(plan["transactions"][0]["amount"], "5000.00")
        self.assertEqual(plan["allocated"], 5000.0)

    def test_short_headroom_allocates_nothing_and_names_both_figures(self):
        """A partial Shopify record is worse than none: it looks settled and is
        not."""
        plan = r.plan_refund([txn("t/1", refundable="5000.00")], 12999.0)

        self.assertEqual(plan["transactions"], [])
        self.assertEqual(plan["allocated"], 0.0)
        self.assertEqual(plan["gateways"], [])
        self.assertTrue(plan["problem"])
        self.assertIn("5000.00", plan["problem"])
        self.assertIn("12999.00", plan["problem"])

    def test_no_refundable_parents_is_a_problem_naming_zero(self):
        plan = r.plan_refund([txn(kind="REFUND")], 100.0)
        self.assertEqual(plan["transactions"], [])
        self.assertTrue(plan["problem"])
        self.assertIn("0.00", plan["problem"])
        self.assertIn("100.00", plan["problem"])

    def test_each_problem_carries_the_code_the_caller_reports(self):
        """Three different refusals shared one reason_code, so
        no_refundable_transactions was documented but unreachable and an order
        with no refundable rows was reported as merely short of headroom."""
        no_parents = r.plan_refund([txn(kind="REFUND")], 100.0)
        self.assertEqual(no_parents["problem_code"], "no_refundable_transactions")

        short = r.plan_refund([txn("t/1", refundable="5000.00")], 12999.0)
        self.assertEqual(short["problem_code"], "insufficient_refundable")

        nothing = r.plan_refund([txn("t/1")], 0)
        self.assertEqual(nothing["problem_code"], "nothing_to_refund")

    def test_a_successful_plan_has_no_problem_code(self):
        plan = r.plan_refund([txn("t/1")], 12999.0)
        self.assertIsNone(plan["problem"])
        self.assertEqual(plan["problem_code"], "")

    def test_every_problem_code_is_in_the_published_vocabulary(self):
        """They are handed straight to the caller as reason_code, so they cannot
        drift out of the closed set."""
        published = set().union(*r.REASON_CODES.values())
        for nodes, amount in (([txn(kind="REFUND")], 100.0),
                              ([txn("t/1", refundable="1.00")], 12999.0),
                              ([txn("t/1")], 0)):
            self.assertIn(r.plan_refund(nodes, amount)["problem_code"], published)

    def test_spreads_across_two_parents_when_one_is_too_small(self):
        plan = r.plan_refund([
            txn("t/1", gateway="cashfree", refundable="10000.00"),
            txn("t/2", gateway="cashfree", refundable="8000.00"),
        ], 15000.0)

        self.assertIsNone(plan["problem"])
        self.assertEqual(plan["allocated"], 15000.0)
        self.assertEqual(
            [(t["parentId"], t["amount"]) for t in plan["transactions"]],
            [("t/1", "10000.00"), ("t/2", "5000.00")],
        )

    def test_a_parent_that_gets_nothing_is_left_out(self):
        plan = r.plan_refund([
            txn("t/1", refundable="10000.00"),
            txn("t/2", refundable="8000.00"),
        ], 9000.0)
        self.assertEqual([t["parentId"] for t in plan["transactions"]], ["t/1"])

    def test_the_order_s_own_gateway_is_reported_verbatim(self):
        """Order #6518 — "manual", which is the normal value on an OCC order and
        says nothing either way about whether the customer gets paid."""
        plan = r.plan_refund(order_6518_transactions(), 12999.0)

        self.assertIsNone(plan["problem"])
        self.assertEqual(plan["gateways"], ["manual"])

    def test_every_allocated_gateway_is_reported_in_order(self):
        plan = r.plan_refund([
            txn("t/1", gateway="manual", refundable="10000.00"),
            txn("t/2", gateway="cashfree", refundable="8000.00"),
        ], 15000.0)

        self.assertEqual(plan["gateways"], ["manual", "cashfree"])

    def test_a_gateway_that_got_no_allocation_is_not_reported(self):
        """A Cashfree row with headroom we never touched is not part of this
        refund, and recording it would misattribute the payout."""
        plan = r.plan_refund([
            txn("t/1", gateway="manual", refundable="20000.00"),
            txn("t/2", gateway="cashfree", refundable="8000.00"),
        ], 5000.0)

        self.assertEqual(plan["gateways"], ["manual"])

    def test_no_moves_money_flag_is_offered(self):
        """Deleted deliberately: it was diagnostic only, nothing consumed it,
        and a boolean over the gateway name invited exactly the misreading that
        produced the wrong first draft of this feature's brief.  The gateway
        names are recorded; that is the whole of what is knowable here."""
        plan = r.plan_refund([txn("t/1")], 12999.0)
        self.assertNotIn("moves_money", plan)
        self.assertFalse(hasattr(r, "gateway_moves_money"))

    def test_gateways_are_deduplicated_in_allocation_order(self):
        plan = r.plan_refund([
            txn("t/1", gateway="cashfree", refundable="6000.00"),
            txn("t/2", gateway="cashfree", refundable="6000.00"),
        ], 9000.0)
        self.assertEqual(plan["gateways"], ["cashfree"])

    def test_zero_or_negative_amount_is_a_problem(self):
        for amount in (0, 0.0, -5):
            plan = r.plan_refund([txn()], amount)
            self.assertEqual(plan["transactions"], [], amount)
            self.assertTrue(plan["problem"], amount)

    def test_paise_are_not_lost_to_float_drift(self):
        """46952.16 across two parents must still total 46952.16 — the #6491
        figure, which is exactly the kind of amount that drifts."""
        plan = r.plan_refund([
            txn("t/1", refundable="46000.00"),
            txn("t/2", refundable="1000.00"),
        ], 46952.16)

        self.assertIsNone(plan["problem"])
        self.assertEqual(
            [t["amount"] for t in plan["transactions"]], ["46000.00", "952.16"]
        )
        self.assertEqual(plan["allocated"], 46952.16)

    def test_accepts_a_connection_not_just_a_list(self):
        plan = r.plan_refund({"nodes": [txn("t/1")]}, 12999.0)
        self.assertEqual([t["parentId"] for t in plan["transactions"]], ["t/1"])


# ── The mutation input ────────────────────────────────────────────────────────

class TestBuildRefundInput(unittest.TestCase):
    def setUp(self):
        self.order_gid = "gid://shopify/Order/7843650535529"

    def plan(self, **kwargs):
        return r.plan_refund([txn("t/1", **kwargs)], 12999.0)

    def test_no_refund_line_items_so_shopify_never_restocks(self):
        """ERPNext is the inventory master; sending line items makes Shopify
        restock behind its back."""
        payload = r.build_refund_input(self.order_gid, self.plan(), "Damaged in transit")
        self.assertNotIn("refundLineItems", payload["input"])
        self.assertNotIn("shipping", payload["input"])

    def test_copies_each_parent_gateway_verbatim_rather_than_a_constant(self):
        plan = r.plan_refund([
            txn("t/1", gateway="cashfree", refundable="10000.00"),
            txn("t/2", gateway="manual", refundable="8000.00"),
        ], 15000.0)
        payload = r.build_refund_input(self.order_gid, plan, "note")

        self.assertEqual(
            [t["gateway"] for t in payload["input"]["transactions"]],
            ["cashfree", "manual"],
        )

    def test_every_transaction_carries_the_order_gid(self):
        payload = r.build_refund_input(self.order_gid, self.plan(), "note")
        self.assertEqual(payload["input"]["orderId"], self.order_gid)
        for t in payload["input"]["transactions"]:
            self.assertEqual(t["orderId"], self.order_gid)
            self.assertEqual(t["kind"], "REFUND")

    def test_reason_note_becomes_the_shopify_note(self):
        """RefundInput.note *is* the reason for refund — the admin's "Reason for
        refund" box writes it, and staff read it there."""
        payload = r.build_refund_input(self.order_gid, self.plan(), "Customer changed mind")
        self.assertEqual(payload["input"]["note"], "Customer changed mind")

    def test_blank_reason_falls_back_rather_than_reading_no_reason_provided(self):
        for note in ("", "   ", None):
            payload = r.build_refund_input(
                self.order_gid, self.plan(), note, fallback_note="Refund REF-0007"
            )
            self.assertEqual(payload["input"]["note"], "Refund REF-0007", repr(note))

    def test_note_is_omitted_entirely_when_there_is_nothing_to_say(self):
        payload = r.build_refund_input(self.order_gid, self.plan(), "")
        self.assertNotIn("note", payload["input"])

    def test_notify_defaults_to_off_so_the_customer_is_not_mailed_twice(self):
        payload = r.build_refund_input(self.order_gid, self.plan(), "note")
        self.assertIs(payload["input"]["notify"], False)

    def test_notify_can_be_turned_on(self):
        payload = r.build_refund_input(self.order_gid, self.plan(), "note", notify=True)
        self.assertIs(payload["input"]["notify"], True)

    def test_no_discrepancy_reason_is_sent(self):
        """discrepancyReason categorises an order-adjustment discrepancy, not the
        human reason for the refund."""
        payload = r.build_refund_input(self.order_gid, self.plan(), "note")
        self.assertNotIn("discrepancyReason", payload["input"])

    def test_an_unplannable_refund_builds_nothing(self):
        plan = r.plan_refund([txn("t/1", refundable="1.00")], 12999.0)
        self.assertIsNone(r.build_refund_input(self.order_gid, plan, "note"))


# ── The mutation document ─────────────────────────────────────────────────────

class TestRefundMutation(unittest.TestCase):
    """The document itself.  Whether it carries an `@idempotent` key is decided
    by the store's API version at the post — see
    tests/test_refund_idempotency.py, which owns that behaviour; this class only
    pins the formatter.
    """

    def test_default_document_carries_no_directive(self):
        mutation = r.build_refund_mutation()
        self.assertNotIn("@idempotent", mutation)
        self.assertIn("refundCreate", mutation)
        self.assertIn("userErrors", mutation)

    def test_a_key_adds_the_directive(self):
        mutation = r.build_refund_mutation("REF-0007:12999.00")
        self.assertIn('@idempotent(key: "REF-0007:12999.00")', mutation)

    def test_the_key_generator_is_wired_to_the_post(self):
        """A minted key that nothing sends would read as though retries were
        already protected — which is why there was no generator here for so
        long.  There is one now because the post uses it, above the version
        that requires it."""
        self.assertTrue(callable(getattr(r, "idempotency_key", None)))
        self.assertTrue(callable(getattr(r, "idempotency_required", None)))
        self.assertIn("idempotency_key", inspect.getsource(r.write_back_refund))

    def test_the_docstring_carries_the_version_that_makes_the_key_mandatory(self):
        self.assertIn(r.IDEMPOTENCY_REQUIRED_FROM, r.build_refund_mutation.__doc__)

    def test_a_key_with_a_quote_in_it_cannot_break_the_document(self):
        mutation = r.build_refund_mutation('bad"key')
        self.assertNotIn('"bad"key"', mutation)
        self.assertIn("badkey", mutation)


class TestTheBriefSaysWhereTheSafeRehearsalCanBeRun(unittest.TestCase):
    """`REFUND-WRITEBACK-BRIEF.md` section 10 prescribes "Check What Shopify
    Says" as the one live exercise that cannot pay anybody.

    What it did not say is that the button is not always there.  It renders only
    on a **submitted** Refund Request whose `refund_channel` is the Shopify
    channel — `refund_request.js` returns on `docstatus !== 1` and gates both
    action buttons on `dispatches_here`, and `get_refund_writeback_status`
    answers `is_shopify` only when `payout_owner == OWNER_SHOPIFY`.  So an
    operator following the brief on a `Bank Transfer` refund hunts for a button
    that will never appear, and the document that sent them looking is the one
    that has to say so.

    Asserted against the code as well as the prose, so the description cannot
    drift from the gate it describes.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        cls.brief = (root / "REFUND-WRITEBACK-BRIEF.md").read_text(
            encoding="utf-8")
        cls.js = (root / "shopify_integration" / "public" / "js"
                  / "refund_request.js").read_text(encoding="utf-8")

    def rehearsal_section(self):
        """The prose around the prescribed rehearsal.

        Windowed rather than searched whole: `Bank Transfer` and `submitted`
        both appear elsewhere in this document, so a whole-file match would
        pass while the step that sends somebody looking said nothing.
        """
        prose = " ".join(self.brief.split())
        at = prose.find("Check What Shopify Says")
        self.assertNotEqual(at, -1, "the brief no longer names the rehearsal")
        return prose[at:at + 1600]

    def test_the_step_says_which_refunds_show_the_button(self):
        section = self.rehearsal_section()
        self.assertRegex(section, r"(?i)submitted")
        self.assertIn("refund_channel", section)
        self.assertIn("Bank Transfer", section)

    def test_the_step_names_what_decides_it_so_it_can_be_checked(self):
        section = self.rehearsal_section()
        self.assertIn("get_refund_writeback_status", section)
        self.assertIn("payout_owner", section)

    def test_the_gate_the_brief_describes_is_the_gate_the_form_has(self):
        """The document is only worth trusting if these are the same two
        conditions the form actually applies."""
        self.assertIn("frm.doc.docstatus !== 1", self.js)
        self.assertIn("frm.doc.refund_channel === SHOPIFY_REFUND_CHANNEL",
                      self.js)
        # Both action buttons hang off that conjunction, the read-only one
        # included — which is exactly why the read-only one can be absent.
        self.assertIn("shopify_refund_targets_button(frm)", self.js)


if __name__ == "__main__":
    unittest.main()
