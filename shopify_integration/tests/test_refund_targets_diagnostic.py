"""
test_refund_targets_diagnostic.py — why every row was disqualified, answerable.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_refund_targets_diagnostic -v

Written after the first live attempt on production (`REF-00207`, order
`7804616802549`, store `electrobotic-in`, 2026-09-08) refused with
`no_refundable_transactions` and left nothing behind to say why:

  * the message asserted "every row is a refund, a void, unsuccessful, or
    already fully refunded" — but it is emitted from `plan_refund`'s
    `if not parents` branch, which fires just the same when the order has **no
    transactions at all**.  Those two readings have opposite fixes: pick a
    different order, versus this store can never be refunded this way.
  * the `RefundTargets` response was discarded.  The Shopify Log payload for
    both attempts was `{"refund_request": "REF-00207"}`, so the rows that had
    just been examined were thrown away at exactly the moment they were needed.

So: the message now distinguishes the two cases and counts the rows, the
refusal records what it saw, and `refund_targets_now` answers the question
without a Shopify login.  None of the three can post a mutation.
"""

import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.tests.test_refund_writeback import (
    REFUND,
    WritebackTestCase,
    targets_response,
)
from shopify_integration.utils import refund as r


def txn(**over):
    """One transaction node, refundable unless overridden."""
    node = {
        "id": "gid://shopify/OrderTransaction/1",
        "kind": "SALE",
        "status": "SUCCESS",
        "gateway": "manual",
        "amountSet": {"presentmentMoney": {"amount": "5.00", "currencyCode": "INR"}},
        "maximumRefundableV2": {"amount": "5.00", "currencyCode": "INR"},
        "parentTransaction": None,
    }
    node.update(over)
    return node


# ── The message has to say which of the two things happened ──────────────────

class TestTheRefusalNamesWhichCase(unittest.TestCase):
    """Frappe-free: `plan_refund` is a pure function and this is where the
    correctness lives."""

    def test_no_transactions_at_all_says_so(self):
        """The live case.  An order Shopify holds no transaction for cannot be
        refunded by any amount, and saying "every row was disqualified" sends
        the reader looking for rows that are not there."""
        plan = r.plan_refund([], 5.0)
        self.assertEqual(plan["problem_code"], "no_refundable_transactions")
        self.assertIn("no transactions", plan["problem"].lower())
        self.assertNotIn("every row", plan["problem"].lower())

    def test_a_none_container_is_the_same_case(self):
        """`order.get("transactions")` is None when the field came back null."""
        plan = r.plan_refund(None, 5.0)
        self.assertEqual(plan["problem_code"], "no_refundable_transactions")
        self.assertIn("no transactions", plan["problem"].lower())

    def test_rows_that_all_fail_say_that_instead_and_count_them(self):
        nodes = [
            txn(kind="REFUND"),
            txn(kind="VOID"),
            txn(status="FAILURE"),
            txn(maximumRefundableV2={"amount": "0.00"}),
        ]
        plan = r.plan_refund(nodes, 5.0)
        self.assertEqual(plan["problem_code"], "no_refundable_transactions")
        self.assertIn("4", plan["problem"])
        self.assertNotIn("no transactions", plan["problem"].lower())

    def test_both_messages_name_the_refund_amount(self):
        for nodes in ([], [txn(kind="REFUND")]):
            self.assertIn("5.00", r.plan_refund(nodes, 5.0)["problem"])

    def test_short_headroom_is_still_its_own_code(self):
        """Unchanged: there ARE parents, they are just not enough."""
        plan = r.plan_refund([txn(maximumRefundableV2={"amount": "2.00"})], 5.0)
        self.assertEqual(plan["problem_code"], "insufficient_refundable")

    def test_a_refundable_order_still_plans(self):
        plan = r.plan_refund([txn()], 5.0)
        self.assertIsNone(plan["problem"])
        self.assertEqual(len(plan["transactions"]), 1)


# ── The refusal must leave the evidence behind ───────────────────────────────

class TestTheRefusalRecordsWhatItSaw(WritebackTestCase):
    """`_log` imports `log_webhook` at call time, so the real target is what
    gets patched here — asserting on the module the code actually reaches rather
    than on a stand-in that could drift from it."""

    def setUp(self):
        super().setUp()
        from shopify_integration.utils import webhook

        self.webhook = webhook
        self._real_log_webhook = webhook.log_webhook
        self.logged = []

        def capture(**kwargs):
            self.logged.append(kwargs)
            return "LOG-0001"

        webhook.log_webhook = capture

    def tearDown(self):
        self.webhook.log_webhook = self._real_log_webhook
        super().tearDown()

    def rows_logged(self):
        """The transaction summary recorded on the attempt, if any."""
        return [call for call in self.logged if call.get("topic") == "refund/writeback"]

    def test_the_transactions_reach_the_log_payload(self):
        """The live failure logged only {"refund_request": ...}, so the one
        question a reader has -- why did every row fail -- could not be answered
        from ERPNext at all."""
        self.responses = [targets_response(transactions=[
            txn(kind="REFUND"), txn(maximumRefundableV2={"amount": "0.00"}),
        ])]
        r.write_back_refund(REFUND)

        logged = self.rows_logged()
        self.assertTrue(logged, "no refund/writeback log entry was written")
        payload = logged[-1]["order_data"]
        self.assertIn("transactions", payload,
                      "the rows examined were not recorded")
        self.assertEqual(len(payload["transactions"]), 2)
        self.assertEqual(payload["refund_request"], REFUND)

    def test_each_logged_row_says_why_it_was_rejected(self):
        self.responses = [targets_response(transactions=[
            txn(kind="REFUND"),
            txn(status="FAILURE"),
            txn(maximumRefundableV2={"amount": "0.00"}),
        ])]
        r.write_back_refund(REFUND)

        rows = self.rows_logged()[-1]["order_data"]["transactions"]
        reasons = [row["rejected_because"] for row in rows]
        self.assertEqual(reasons, ["kind", "status", "no_headroom"])

    def test_an_empty_order_logs_an_empty_list_not_a_missing_key(self):
        """"No rows" and "we did not look" must not read the same in the log —
        that is the ambiguity this whole file exists to remove."""
        self.responses = [targets_response(transactions=[])]
        r.write_back_refund(REFUND)
        payload = self.rows_logged()[-1]["order_data"]
        self.assertEqual(payload["transactions"], [])

    def test_the_row_summary_carries_no_customer_data(self):
        """It goes in a log a lot of people can read.  Ids, kinds, statuses,
        gateways and figures only."""
        self.responses = [targets_response(transactions=[txn(kind="REFUND")])]
        r.write_back_refund(REFUND)
        row = self.rows_logged()[-1]["order_data"]["transactions"][0]
        self.assertEqual(
            set(row),
            {"id", "kind", "status", "gateway", "amount", "refundable",
             # A slug naming which of the three sources produced the figure —
             # see tests/test_refund_headroom.py.  It is about this app's
             # reading, never about the order or the person behind it.
             "refundable_reported", "refundable_source", "rejected_because"},
        )
    def test_nothing_was_sent_on_that_path(self):
        self.responses = [targets_response(transactions=[])]
        result = r.write_back_refund(REFUND)
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)
        self.assertNothingSent()
        self.assertNoGid()






class TestNullHeadroomIsNotZeroHeadroom(unittest.TestCase):
    """The distinction the live diagnosis turns on, and one my first summary
    lost.

    `_headroom` reads `maximumRefundableV2.amount` and `_paise(None)` is 0, so a
    field Shopify did **not** return and a field it returned as `0.00` both
    reject the row and both render as "0.00".  They mean opposite things:

      * `0.00` — Shopify has nothing left to refund on this transaction. The
        order is settled or already refunded; the guard is correct.
      * **absent** — we are reading a field this API version does not populate
        for this transaction, and then NO order is ever refundable, on any
        store. That is a defect in this app, not a fact about the order.

    Pinned because production evidence says `REF-00207`'s transaction passes
    kind and status (`select_gateway_transaction` found it and wrote
    `CASHFREE - UPI` to the Payment Entry), so headroom is the only filter left
    and which of these two it is decides the whole remedy.
    """

    def test_a_reported_zero_says_it_was_reported(self):
        row = r.transaction_summary([txn(maximumRefundableV2={"amount": "0.00"})])[0]
        self.assertEqual(row["refundable"], "0.00")
        self.assertTrue(row["refundable_reported"])
        self.assertEqual(row["rejected_because"], "no_headroom")

    def test_a_missing_field_says_it_was_not_reported(self):
        for node in (txn(maximumRefundableV2=None),
                     {k: v for k, v in txn().items() if k != "maximumRefundableV2"}):
            row = r.transaction_summary([node])[0]
            self.assertEqual(row["refundable"], "0.00")
            self.assertFalse(
                row["refundable_reported"],
                "a field Shopify never sent must not read as a reported zero",
            )

    def test_a_real_amount_is_reported(self):
        row = r.transaction_summary([txn()])[0]
        self.assertEqual(row["refundable"], "5.00")
        self.assertTrue(row["refundable_reported"])
        self.assertEqual(row["rejected_because"], "")

    def test_a_null_amount_inside_the_money_object_is_also_unreported(self):
        """`{"amount": null}` is the shape a deprecated field takes when it
        resolves but carries nothing."""
        row = r.transaction_summary([txn(maximumRefundableV2={"amount": None})])[0]
        self.assertFalse(row["refundable_reported"])

# ── The diagnostic itself ────────────────────────────────────────────────────

class TestRefundTargetsNow(WritebackTestCase):
    """A read that answers "what does Shopify say is refundable here", so the
    next failure does not need a Shopify login to explain."""

    def test_it_reports_every_row_and_why_each_was_rejected(self):
        # 10.00 charged against a 5.00 refund already given back, so the SALE
        # still has headroom and is reported as usable.  The amounts matter
        # since 2026-09-09: a REFUND row is subtracted from what its parent can
        # still take, so a 5.00 sale with a 5.00 refund against it is a settled
        # order and would be rejected here for the right reason — which is not
        # the reason this test is about.
        self.responses = [targets_response(transactions=[
            txn(id="gid://shopify/OrderTransaction/9", kind="SALE",
                amountSet={"presentmentMoney": {"amount": "10.00",
                                                "currencyCode": "INR"}}),
            txn(kind="REFUND"),
        ])]
        info = r.refund_targets_now(REFUND)

        self.assertTrue(info["ok"], info)
        self.assertEqual(len(info["transactions"]), 2)
        self.assertEqual(info["transactions"][0]["rejected_because"], "")
        self.assertEqual(info["transactions"][1]["rejected_because"], "kind")

    def test_it_reports_the_headroom_and_the_verdict(self):
        self.responses = [targets_response(transactions=[txn()])]
        info = r.refund_targets_now(REFUND)
        self.assertEqual(info["refundable_total"], 5.0)
        self.assertEqual(info["amount"], 12999.0)
        # 5.00 of headroom against a 12999.00 refund.
        self.assertEqual(info["would_refuse_with"], "insufficient_refundable")

    def test_it_names_the_empty_case_explicitly(self):
        """The live symptom, and the answer we could not get."""
        self.responses = [targets_response(transactions=[])]
        info = r.refund_targets_now(REFUND)
        self.assertEqual(info["transactions"], [])
        self.assertEqual(info["would_refuse_with"], "no_refundable_transactions")
        self.assertIn("no transactions", info["message"].lower())

    def test_it_posts_no_mutation_ever(self):
        """It exists to diagnose a payout, so it must not be able to make one.
        Asserted by reading the module as well as by exercising it, because a
        future edit is the risk, not this one."""
        self.responses = [targets_response(transactions=[txn()])]
        r.refund_targets_now(REFUND)
        self.assertNothingSent()
        self.assertEqual(
            [call["operation"] for call in self.calls], ["RefundTargets"]
        )

    def test_it_writes_nothing_to_the_refund_request(self):
        """Not a write-back and not an attempt: it must not touch the five
        fields, or a diagnostic would look like a payout in the log."""
        self.responses = [targets_response(transactions=[txn()])]
        before = dict(frappe_stub.get_doc_values(r.REFUND_REQUEST, REFUND))
        r.refund_targets_now(REFUND)
        self.assertEqual(frappe_stub.get_doc_values(r.REFUND_REQUEST, REFUND), before)

    def test_it_is_not_gated_on_the_payout_toggle(self):
        """Gating the one safe call on the dangerous switch would mean the
        diagnosis was only available while the payout was armed -- the same
        mistake REFUND-REPORT-CONTRACT.md section 7 refuses for the report."""
        frappe_stub.DB["Shopify Settings"]["Test Store"]["enable_refund_writeback"] = 0
        self.responses = [targets_response(transactions=[txn()])]
        self.assertTrue(r.refund_targets_now(REFUND)["ok"])

    def test_it_still_needs_credentials_and_says_so(self):
        r.has_admin_api_credentials = lambda settings: False
        info = r.refund_targets_now(REFUND)
        self.assertFalse(info["ok"])
        self.assertEqual(info["reason_code"], "no_api_credentials")

    def test_a_non_shopify_refund_is_reported_not_queried(self):
        frappe_stub.DB["Sales Order"]["SO-0001"]["shopify_order_id"] = ""
        info = r.refund_targets_now(REFUND)
        self.assertFalse(info["ok"])
        self.assertEqual(info["reason_code"], r.REASON_NOT_OURS)
        self.assertEqual(self.calls, [])

    def test_a_missing_order_is_reported_not_raised(self):
        self.responses = [targets_response(order=False)]
        info = r.refund_targets_now(REFUND)
        self.assertFalse(info["ok"])
        self.assertEqual(info["reason_code"], "shopify_order_not_found")

    def test_a_query_failure_is_reported_not_raised(self):
        from shopify_integration.utils.shopify_api import ShopifyAPIError
        self.responses = [ShopifyAPIError("throttled")]
        info = r.refund_targets_now(REFUND)
        self.assertFalse(info["ok"])
        self.assertEqual(info["reason_code"], "query_failed")

    def test_it_never_raises(self):
        for responses in ([], [targets_response(order=False)],
                          [targets_response(transactions=None)]):
            self.seed()
            self.responses = list(responses)
            try:
                r.refund_targets_now(REFUND)
            except Exception as exc:  # noqa: BLE001 — that is the assertion
                self.fail(f"{responses!r} raised {exc!r}")

    def test_it_is_whitelisted_and_the_payout_is_not(self):
        self.assertTrue(getattr(r.refund_targets_now, "__is_whitelisted__", False))
        self.assertFalse(getattr(r.write_back_refund, "__is_whitelisted__", False))

    def test_the_module_has_exactly_one_mutation_and_the_diagnostic_avoids_it(self):
        """A structural guard: the diagnostic shares the query with the payout,
        so a future edit that reached for the mutation constant here would be a
        payout behind a read-only name."""
        import inspect
        source = inspect.getsource(r.refund_targets_now)
        # The read itself moved into read_refund_targets when the suggestion
        # block gained a fallback, so the guard follows it: the diagnostic must
        # reach Shopify through that function and through nothing else.
        self.assertIn("read_refund_targets", source)
        self.assertNotIn("execute(", source)
        self.assertNotIn("_REFUND_CREATE_MUTATION", source)
        self.assertNotIn("refundCreate", source)

        reader = inspect.getsource(r.read_refund_targets)
        self.assertIn("_REFUND_TARGETS_QUERY", reader)
        self.assertNotIn("_REFUND_CREATE_MUTATION", reader)
        self.assertNotIn("refundCreate", reader)


if __name__ == "__main__":
    unittest.main()
