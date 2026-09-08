"""
test_refund_writeback.py — tests for the frappe-bound half of the refund
write-back.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_refund_writeback -v

A successful refundCreate is a payout, so the tests that matter most are the
ones about NOT sending: every guard, and every way a response can look like
success without being one.

  * each guard in §6 skips, records a reason, and does not raise
  * userErrors → Failed, error stored, no GID written
  * HTTP 200 with empty userErrors and no refund object → never Done, and
    reported as possibly-paid rather than as a plain failure
  * the sent/not-sent boundary: everything before the mutation is safe to retry,
    everything after it may already have paid the customer
  * success writes the GID, Done, and the gateway Shopify actually used
  * a second call on a row that already has a GID sends nothing
  * the credit-note webhook guard returns early for a refund we wrote
"""

import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

import frappe  # noqa: E402

from shopify_integration.tests.frappe_stub import FakeSettings  # noqa: E402
from shopify_integration.utils import refund as r  # noqa: E402
from shopify_integration.utils.shopify_api import ShopifyAPIError  # noqa: E402

REFUND = "REF-0007"
ORDER_ID = "7843650535529"
ORDER_GID = "gid://shopify/Order/7843650535529"
REFUND_GID = "gid://shopify/Refund/1234567890"

WRITEBACK_FIELDS = {
    r.REFUND_GID_FIELD,
    r.WRITEBACK_STATUS_FIELD,
    r.REFUND_GATEWAY_FIELD,
    r.WRITEBACK_ERROR_FIELD,
    r.WRITEBACK_AT_FIELD,
}


def install_sales_order_lookup(testcase):
    """Give `frappe.get_all` the multi-row reads this module makes, answered
    from the fake DB, and restore it on cleanup.

    The stub's `get_all` returns [] for everything, which is the right default
    for a fake that records writes — but `unverified_writebacks_for_order` has
    to see EVERY Sales Order carrying a Shopify order id, because an amended
    order leaves two of them and the Refund Request points at one.  Without
    this the amended case cannot be tested at all, and it is the case that
    exists on this site (see the `already_paid` GID test below).

    Filters go through the stub's own `_filter_matches`, not a bare `==`, for
    a reason that only appeared once the lookup returned every match: it reads
    Refund Requests with `sales_order: ["in", [...]]` and `docstatus: 1`, and a
    fake that compared the two-element operator list for equality would match
    nothing and report "no unresolved write-back" for an order that had one.
    Names are sorted, so a test pins one order rather than dict insertion.
    """
    real = frappe.get_all

    def _get_all(doctype, filters=None, fields=None, pluck=None, **kwargs):
        names = sorted(
            name for name, doc in frappe_stub.DB.get(doctype, {}).items()
            if frappe_stub._filter_matches(doc, filters)
        )
        if pluck:
            return [
                name if pluck == "name"
                else frappe_stub.DB[doctype][name].get(pluck)
                for name in names
            ]
        return [{"name": name} for name in names]

    frappe.get_all = _get_all
    testcase.addCleanup(lambda: setattr(frappe, "get_all", real))
    return _get_all


def targets_response(transactions=None, order=True):
    """A RefundTargets response.  order.transactions as a plain list, which is
    the shape the docs show for that field."""
    if not order:
        return {"order": None}
    if transactions is None:
        transactions = [{
            "id": "gid://shopify/OrderTransaction/99",
            "kind": "SALE",
            "status": "SUCCESS",
            "gateway": "manual",
            "formattedGateway": "Manual",
            "amountSet": {"presentmentMoney": {"amount": "12999.00", "currencyCode": "INR"}},
            "maximumRefundableV2": {"amount": "12999.00", "currencyCode": "INR"},
            "parentTransaction": None,
        }]
    return {"order": {"id": ORDER_GID, "name": "#6518", "transactions": transactions}}


def refund_created(gid=REFUND_GID, gateways=("manual",), user_errors=None, refund=True):
    """A refundCreate response.  refund.transactions as an edges/node connection,
    which is the shape the docs show for THAT field."""
    payload = {"userErrors": list(user_errors or [])}
    if refund:
        payload["refund"] = {
            "id": gid,
            "note": "Damaged in transit",
            "totalRefundedSet": {"presentmentMoney": {"amount": "12999.00", "currencyCode": "INR"}},
            "transactions": {"edges": [
                {"node": {"id": f"gid://shopify/OrderTransaction/1{i}",
                          "gateway": gateway, "kind": "REFUND", "status": "SUCCESS",
                          "amountSet": {"presentmentMoney": {"amount": "12999.00"}}}}
                for i, gateway in enumerate(gateways)
            ]},
        }
    else:
        payload["refund"] = None
    return {"refundCreate": payload}


class WritebackTestCase(unittest.TestCase):
    """Seeds one write-backable Refund Request and captures every GraphQL call."""

    def setUp(self):
        # Originals are captured ONCE per test.  seed() is what the loop tests
        # re-enter; if they re-entered setUp, the second capture would take the
        # already-patched frappe.get_doc as the original and tearDown would
        # restore a stub into every test module that runs after this one.
        self._real_execute = r.execute
        self._real_creds = r.has_admin_api_credentials
        self._real_get_doc = frappe.get_doc
        self.seed()

    def seed(self):
        frappe_stub.reset()
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set(WRITEBACK_FIELDS)

        self.settings = FakeSettings()
        frappe_stub.set_doc("Shopify Settings", "Test Store", {
            "name": "Test Store",
            "shop_domain": "notdrones.myshopify.com",
            "enable_sync": 1,
            "enable_refund_writeback": 1,
            "notify_customer_on_refund": 0,
        })
        frappe_stub.set_doc("Sales Order", "SO-0001", {
            "shopify_order_id": ORDER_ID,
            "shopify_store": "notdrones.myshopify.com",
        })
        frappe_stub.set_doc(r.REFUND_REQUEST, REFUND, {
            "name": REFUND,
            "docstatus": 1,
            # A refund payment_portals has authorised and not yet booked, on the
            # channel that dispatches a payout.  Approved rather than Completed
            # since CONTRACT_VERSION 4: refundCreate PAYS the customer, so the
            # dispatchable state is the one before ERPNext records the payment,
            # not after it.  See tests/test_refund_dispatch_gate.py.
            "status": "Approved",
            "refund_channel": r.CHANNEL_DISPATCH,
            "payment_entry": "",
            "sales_order": "SO-0001",
            "net_refund_amount": 12999.0,
            "reason_note": "Damaged in transit",
            r.REFUND_GID_FIELD: "",
            r.WRITEBACK_STATUS_FIELD: "",
        })

        # Capture GraphQL calls and serve canned responses in order.
        self.calls = []
        self.responses = [targets_response(), refund_created()]

        def fake_execute(settings, query, variables=None, operation="", **kwargs):
            # **kwargs rather than a declared `idempotent=True`, deliberately.
            # The tests below assert on what refund.py actually PASSED, and a
            # default here would make "posted with the ordinary retries" and
            # "posted with idempotent=False" indistinguishable — which is the
            # single fact every post-send "nothing was sent" claim rests on.
            self.calls.append({"query": query, "variables": variables or {},
                               "operation": operation, "kwargs": dict(kwargs)})
            if not self.responses:
                raise AssertionError(f"unexpected GraphQL call: {operation}")
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        r.execute = fake_execute
        r.has_admin_api_credentials = lambda settings: True

        # get_doc("Shopify Settings", …) must return something with .get()
        real_get_doc = self._real_get_doc
        frappe.get_doc = lambda dt, name=None, **k: (
            self.settings if dt == "Shopify Settings" else real_get_doc(dt, name, **k)
        )

    def tearDown(self):
        r.execute = self._real_execute
        r.has_admin_api_credentials = self._real_creds
        frappe.get_doc = self._real_get_doc

    # ── helpers ──────────────────────────────────────────────────────────────

    def stored(self, fieldname):
        return frappe_stub.get_doc_values(r.REFUND_REQUEST, REFUND).get(fieldname)

    def set_field(self, **values):
        frappe_stub.DB[r.REFUND_REQUEST][REFUND].update(values)

    @property
    def mutations(self):
        return [c for c in self.calls if c["operation"] == "refundCreate"]

    def posted_kwargs(self, operation):
        """The keyword arguments refund.py passed to execute() for that call.

        `{}` when it passed none, so a test can tell "posted with
        idempotent=False" from "did not say" — which is the difference between
        a post that happened at most once and one that may have happened up to
        five times.
        """
        calls = [c for c in self.calls if c["operation"] == operation]
        self.assertTrue(calls, f"no {operation} call was made")
        return calls[0]["kwargs"]

    def assertNothingSent(self):
        self.assertEqual(
            self.mutations, [], "a refundCreate was sent when nothing should have been"
        )

    def assertNoGid(self):
        self.assertFalse(
            (self.stored(r.REFUND_GID_FIELD) or "").strip(),
            "a refund GID was recorded for a refund Shopify did not accept",
        )


# ── The happy path ────────────────────────────────────────────────────────────

class TestSuccess(WritebackTestCase):
    def test_success_records_gid_status_and_gateway(self):
        result = r.write_back_refund(REFUND)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], r.STATUS_DONE)
        self.assertEqual(result["refund_gid"], REFUND_GID)
        self.assertEqual(self.stored(r.REFUND_GID_FIELD), REFUND_GID)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_DONE)
        self.assertEqual(self.stored(r.REFUND_GATEWAY_FIELD), "manual")
        self.assertFalse(self.stored(r.WRITEBACK_ERROR_FIELD))

    def test_the_gateway_comes_from_the_response_not_from_our_plan(self):
        """Shopify is the authority on which gateway it used."""
        self.responses = [targets_response(), refund_created(gateways=("cashfree",))]
        r.write_back_refund(REFUND)
        self.assertEqual(self.stored(r.REFUND_GATEWAY_FIELD), "cashfree")

    def test_several_response_gateways_are_all_recorded(self):
        self.responses = [
            targets_response(),
            refund_created(gateways=("manual", "cashfree", "manual")),
        ]
        r.write_back_refund(REFUND)
        self.assertEqual(self.stored(r.REFUND_GATEWAY_FIELD), "manual, cashfree")

    def test_the_mutation_carries_the_reason_and_does_not_notify(self):
        r.write_back_refund(REFUND)
        payload = self.mutations[0]["variables"]["input"]

        self.assertEqual(payload["note"], "Damaged in transit")
        self.assertIs(payload["notify"], False)
        self.assertEqual(payload["orderId"], ORDER_GID)
        self.assertNotIn("refundLineItems", payload)

    def test_notify_follows_the_store_setting(self):
        self.settings._values["notify_customer_on_refund"] = 1
        r.write_back_refund(REFUND)
        self.assertIs(self.mutations[0]["variables"]["input"]["notify"], True)

    def test_a_blank_reason_falls_back_to_the_refund_name(self):
        self.set_field(reason_note="")
        r.write_back_refund(REFUND)
        self.assertEqual(self.mutations[0]["variables"]["input"]["note"], f"Refund {REFUND}")

    def test_the_amount_sent_is_net_refund_amount(self):
        """Not refund_amount (gross) and not total_payout (with
        reimbursements) — net_refund_amount is what the customer receives."""
        self.set_field(net_refund_amount=5000.0, refund_amount=12999.0, total_payout=20000.0)
        self.responses = [
            targets_response(), refund_created(),
        ]
        r.write_back_refund(REFUND)
        transactions = self.mutations[0]["variables"]["input"]["transactions"]
        self.assertEqual([t["amount"] for t in transactions], ["5000.00"])

    def test_the_gid_is_committed_so_the_webhook_cannot_beat_it(self):
        """Our own write fires refunds/create.  If the GID is not committed
        before that arrives, the credit-note guard cannot see it and ERPNext
        gets a second Credit Note."""
        before = len(frappe_stub.COMMITS)
        r.write_back_refund(REFUND)
        self.assertGreater(len(frappe_stub.COMMITS), before)

    def test_a_shopify_log_entry_is_written(self):
        r.write_back_refund(REFUND)
        logs = [d for d in frappe_stub.INSERTS if d.get("doctype") == "Shopify Log"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["status"], "Processed")
        self.assertEqual(logs[0]["shopify_order_id"], ORDER_ID)

    def test_the_writeback_timestamp_is_recorded(self):
        r.write_back_refund(REFUND)
        self.assertTrue(self.stored(r.WRITEBACK_AT_FIELD))


# ── Ways a response can lie about success ─────────────────────────────────────

class TestResponseFailures(WritebackTestCase):
    def test_user_errors_fail_and_store_the_message_without_a_gid(self):
        self.responses = [
            targets_response(),
            refund_created(user_errors=[
                {"field": ["input", "transactions", "0", "amount"],
                 "message": "Refund amount exceeds the amount available to refund."},
            ]),
        ]
        result = r.write_back_refund(REFUND)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], r.STATUS_FAILED)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_FAILED)
        self.assertIn("exceeds the amount available", self.stored(r.WRITEBACK_ERROR_FIELD))
        self.assertNoGid()

    def test_http_200_with_no_user_errors_and_no_refund_is_never_done(self):
        """The quiet failure this codebase already guards elsewhere: the request
        was accepted and nothing said what happened.  It must not be Done — and
        it must not be a plain failure either, because Shopify answered without
        complaining and "nothing happened" is an assumption."""
        self.responses = [targets_response(), refund_created(refund=False)]
        result = r.write_back_refund(REFUND)

        self.assertFalse(result["ok"])
        self.assertNotEqual(result["status"], r.STATUS_DONE)
        self.assertEqual(result["status"], r.STATUS_UNVERIFIED)
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertFalse(result["retry_safe"])
        self.assertTrue(result["possibly_paid"])
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED)
        self.assertNoGid()

    def test_a_refund_object_with_no_id_is_also_unverified(self):
        self.responses = [targets_response(), {"refundCreate": {
            "refund": {"id": "", "transactions": {"edges": []}}, "userErrors": [],
        }}]
        result = r.write_back_refund(REFUND)
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertNoGid()

    def test_a_missing_mutation_key_is_unverified(self):
        """check_user_errors raises ShopifyAPIError here, but the mutation was
        already posted, so the response being unintelligible does not mean the
        refund did not happen."""
        self.responses = [targets_response(), {}]
        result = r.write_back_refund(REFUND)
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertFalse(result["retry_safe"])
        self.assertNoGid()

    def test_a_transport_error_after_the_mutation_is_possibly_paid(self):
        """execute() retries internally, so a lost response on any attempt may be
        hiding a refund that went through."""
        self.responses = [targets_response(), ShopifyAPIError("connection reset")]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_UNVERIFIED)
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(result["reason_code"], "transport_error_after_send")
        self.assertIn("connection reset", self.stored(r.WRITEBACK_ERROR_FIELD))
        self.assertNoGid()

    def test_the_unverified_note_tells_the_reader_not_to_retry(self):
        self.responses = [targets_response(), ShopifyAPIError("connection reset")]
        r.write_back_refund(REFUND)
        recorded = self.stored(r.WRITEBACK_ERROR_FIELD).lower()

        self.assertIn("possibly paid", recorded)
        self.assertIn("not retry", recorded)
        self.assertIn(ORDER_ID, self.stored(r.WRITEBACK_ERROR_FIELD))

    def test_an_unverified_outcome_is_shouted_into_the_error_log(self):
        """Nothing chases this state, so it has to be loud where somebody looks."""
        self.responses = [targets_response(), ShopifyAPIError("connection reset")]
        r.write_back_refund(REFUND)
        titles = [title for _, title in frappe_stub.ERRORS]
        self.assertTrue(
            any("Outcome Unknown" in title for title in titles), titles
        )

    def test_an_auth_rejection_is_unsent_not_unverified(self):
        """Rejected at the auth layer before the document ran.  Calling this
        unknown would park every refund on a mis-scoped token in Unverified for a
        person to clear by hand.

        `proves_not_executed=True` is the shape execute() raises for a 401/403,
        and since round 3 it is the only thing this side reads."""
        self.responses = [
            targets_response(),
            ShopifyAPIError("forbidden", 403, proves_not_executed=True),
        ]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)
        self.assertEqual(result["reason_code"], "not_authorised")
        self.assertTrue(result["retry_safe"])
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_FAILED)

    def test_user_errors_are_unsent_and_safe_to_retry(self):
        """userErrors is unambiguous: Shopify read the request and declined it."""
        self.responses = [
            targets_response(),
            refund_created(user_errors=[{"field": None, "message": "Refund too large."}]),
        ]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)
        self.assertEqual(result["reason_code"], "rejected_by_shopify")
        self.assertTrue(result["retry_safe"])
        self.assertFalse(result["possibly_paid"])

    def test_a_failed_query_never_reaches_the_mutation(self):
        self.responses = [ShopifyAPIError("order query failed")]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_FAILED)
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)
        self.assertEqual(result["reason_code"], "query_failed")
        self.assertTrue(result["retry_safe"])
        self.assertNothingSent()

    def test_nothing_that_stopped_short_of_the_mutation_is_possibly_paid(self):
        """The sent/not-sent boundary, swept.  Every one of these gives up before
        refundCreate is posted, so all of them are safe to retry and none may be
        reported as possibly paid."""
        cases = {
            "query_failed": [ShopifyAPIError("boom")],
            "shopify_order_not_found": [targets_response(order=False)],
            "insufficient_refundable": [targets_response([{
                "id": "gid://shopify/OrderTransaction/99",
                "kind": "SALE", "status": "SUCCESS", "gateway": "manual",
                "amountSet": {"presentmentMoney": {"amount": "12999.00"}},
                "maximumRefundableV2": {"amount": "1.00"},
            }])],
        }
        for reason_code, responses in cases.items():
            self.seed()
            self.responses = list(responses)
            result = r.write_back_refund(REFUND)

            self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT, reason_code)
            self.assertEqual(result["reason_code"], reason_code)
            self.assertTrue(result["retry_safe"], reason_code)
            self.assertFalse(result["possibly_paid"], reason_code)
            self.assertNothingSent()
            self.assertEqual(
                self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_FAILED, reason_code
            )

    def test_a_missing_order_fails_without_sending(self):
        self.responses = [targets_response(order=False)]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_FAILED)
        self.assertNothingSent()
        self.assertIn(ORDER_ID, self.stored(r.WRITEBACK_ERROR_FIELD))

    def test_short_headroom_fails_without_sending_and_names_both_figures(self):
        """A partial refund in Shopify looks settled and is not — and here it
        would also be a partial payout."""
        self.responses = [targets_response([{
            "id": "gid://shopify/OrderTransaction/99",
            "kind": "SALE", "status": "SUCCESS", "gateway": "manual",
            "amountSet": {"presentmentMoney": {"amount": "12999.00"}},
            "maximumRefundableV2": {"amount": "5000.00"},
        }])]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_FAILED)
        self.assertNothingSent()
        error = self.stored(r.WRITEBACK_ERROR_FIELD)
        self.assertIn("5000.00", error)
        self.assertIn("12999.00", error)

    def test_an_already_fully_refunded_order_fails_without_sending(self):
        """The safe first live exercise (§10): Shopify has no headroom left, so
        this is refused before the mutation is even built."""
        self.responses = [targets_response([{
            "id": "gid://shopify/OrderTransaction/99",
            "kind": "SALE", "status": "SUCCESS", "gateway": "manual",
            "amountSet": {"presentmentMoney": {"amount": "12999.00"}},
            "maximumRefundableV2": {"amount": "0.00"},
        }])]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_FAILED)
        self.assertNothingSent()

    def test_a_failure_writes_a_failed_shopify_log(self):
        self.responses = [targets_response(), refund_created(refund=False)]
        r.write_back_refund(REFUND)
        logs = [d for d in frappe_stub.INSERTS if d.get("doctype") == "Shopify Log"]
        self.assertEqual([log["status"] for log in logs], ["Failed"])

    def test_the_order_transactions_connection_shape_also_works(self):
        """The one thing unverifiable offline: order.transactions is documented
        as a plain list, but the connection form appears in the examples too."""
        plain = targets_response()["order"]["transactions"]
        self.responses = [
            {"order": {"id": ORDER_GID, "name": "#6518",
                       "transactions": {"nodes": plain}}},
            refund_created(),
        ]
        result = r.write_back_refund(REFUND)
        self.assertTrue(result["ok"], result)


# ── Guards: none of these may raise, all record a reason ──────────────────────

class TestGuards(WritebackTestCase):
    def assertSkipped(self, result, needle=""):
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], r.STATUS_SKIPPED)
        self.assertTrue(result["message"], "a skip must say why")
        if needle:
            self.assertIn(needle, result["message"].lower())
        self.assertNothingSent()

    def test_an_existing_gid_sends_nothing_and_keeps_the_original(self):
        """Idempotency, and it must survive a retry, a requeue and an amendment."""
        self.set_field(**{
            r.REFUND_GID_FIELD: REFUND_GID,
            r.WRITEBACK_STATUS_FIELD: r.STATUS_DONE,
        })
        result = r.write_back_refund(REFUND)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], r.STATUS_DONE)
        self.assertNothingSent()
        self.assertEqual(self.stored(r.REFUND_GID_FIELD), REFUND_GID)

    def test_an_existing_gid_is_not_overwritten_by_a_skip_record(self):
        self.set_field(**{r.REFUND_GID_FIELD: REFUND_GID,
                          r.WRITEBACK_STATUS_FIELD: r.STATUS_DONE})
        r.write_back_refund(REFUND)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_DONE)

    def test_a_refund_that_came_from_shopify_is_skipped(self):
        """refund_channel "Manual Portal Refund" means somebody refunded it in
        Shopify; writing it back would refund it twice."""
        self.set_field(refund_channel=r.CHANNEL_FROM_SHOPIFY)
        self.assertSkipped(r.write_back_refund(REFUND), "shopify")

    def test_a_refund_in_an_undispatchable_status_is_skipped(self):
        """Was `..._that_is_not_completed_...` until CONTRACT_VERSION 4, when the
        gate inverted: `Approved` and `Queued` are now the states a payout is
        dispatched from, and `Completed` is refused because ERPNext has by then
        already recorded the payment.  The full sweep, including the
        `already_booked` case and the channel allow-list, is in
        tests/test_refund_dispatch_gate.py."""
        for status in ("Draft", "Processing", "Failed", "Cancelled", "Completed"):
            self.seed()
            self.set_field(status=status)
            self.assertSkipped(r.write_back_refund(REFUND))

    def test_an_unsubmitted_refund_is_skipped(self):
        for docstatus in (0, 2):
            self.seed()
            self.set_field(docstatus=docstatus)
            self.assertSkipped(r.write_back_refund(REFUND))

    def test_a_refund_with_no_sales_order_is_skipped(self):
        self.set_field(sales_order="")
        self.assertSkipped(r.write_back_refund(REFUND), "sales order")

    def test_a_sales_order_with_no_shopify_order_id_is_skipped(self):
        """Payment links and direct Cashfree payments — not Shopify's to refund,
        and payment_portals keeps that path."""
        frappe_stub.DB["Sales Order"]["SO-0001"]["shopify_order_id"] = ""
        self.assertSkipped(r.write_back_refund(REFUND), "shopify order")

    def test_a_zero_or_negative_amount_is_skipped(self):
        for amount in (0, -1):
            self.seed()
            self.set_field(net_refund_amount=amount)
            self.assertSkipped(r.write_back_refund(REFUND))

    def test_a_store_with_the_writeback_disabled_is_skipped(self):
        frappe_stub.DB["Shopify Settings"]["Test Store"]["enable_refund_writeback"] = 0
        self.assertSkipped(r.write_back_refund(REFUND), "write-back")

    def test_a_store_with_sync_disabled_is_skipped(self):
        frappe_stub.DB["Shopify Settings"]["Test Store"]["enable_sync"] = 0
        self.assertSkipped(r.write_back_refund(REFUND))

    def test_an_unresolvable_store_is_skipped(self):
        frappe_stub.DB["Sales Order"]["SO-0001"]["shopify_store"] = "unknown.myshopify.com"
        self.assertSkipped(r.write_back_refund(REFUND))

    def test_a_store_without_admin_api_credentials_is_skipped(self):
        r.has_admin_api_credentials = lambda settings: False
        self.assertSkipped(r.write_back_refund(REFUND), "credential")

    def test_a_missing_refund_request_doctype_is_skipped(self):
        """shopify_integration must stay inert on a site with no
        payment_portals."""
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set()
        self.assertSkipped(r.write_back_refund(REFUND), "migrate")

    def test_the_payout_never_consults_the_permission_system_at_all(self):
        """Not "the check is lenient" — there is no check.  Two permission models
        over one payout deadlocked, so authorisation belongs to whoever
        dispatches this.  Asserted by making any call to frappe.has_permission
        blow up, rather than by reading the source."""
        called = []

        real = frappe.has_permission

        def explode(*a, **k):
            called.append((a, k))
            raise AssertionError(
                "write_back_refund consulted frappe.has_permission; a caller "
                "payment_portals authorised can then still be refused here"
            )

        frappe.has_permission = explode
        try:
            result = r.write_back_refund(REFUND)
        finally:
            frappe.has_permission = real

        self.assertEqual(called, [])
        self.assertTrue(result["ok"], result)

    def test_an_unmigrated_site_says_migrate(self):
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set()
        result = r.write_back_refund(REFUND)

        self.assertIn("migrate", result["message"].lower())
        self.assertEqual(result["reason_code"], "not_installed")
        self.assertEqual(result["payout_owner"], r.OWNER_UNKNOWN)

    def test_a_nonexistent_refund_request_is_skipped(self):
        self.assertSkipped(r.write_back_refund("REF-NOPE"))

    def test_every_guard_records_its_reason_on_the_document(self):
        self.set_field(refund_channel=r.CHANNEL_FROM_SHOPIFY)
        r.write_back_refund(REFUND)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_SKIPPED)
        self.assertTrue(self.stored(r.WRITEBACK_ERROR_FIELD))

    def test_no_guard_raises(self):
        """A payout path that throws on an edge case is worse than one that
        refuses.  Every guard returns a result dict."""
        cases = (
            {"refund_channel": r.CHANNEL_FROM_SHOPIFY},
            {"status": "Draft"},
            {"docstatus": 0},
            {"sales_order": ""},
            {"net_refund_amount": 0},
            {"reason_note": None},
            {"net_refund_amount": None},
            {"status": None},
            {"refund_channel": None},
        )
        for case in cases:
            self.seed()
            self.set_field(**case)
            try:
                result = r.write_back_refund(REFUND)
            except Exception as exc:  # noqa: BLE001 — that is the assertion
                self.fail(f"{case} raised {exc!r}")
            self.assertIn("status", result)


# ── The worker claim ──────────────────────────────────────────────────────────

class TestClaim(WritebackTestCase):
    def test_a_live_pending_claim_blocks_a_second_worker(self):
        self.set_field(**{
            r.WRITEBACK_STATUS_FIELD: r.STATUS_PENDING,
            r.WRITEBACK_AT_FIELD: frappe.utils.now_datetime(),
        })
        result = r.write_back_refund(REFUND)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], r.STATUS_PENDING)
        self.assertNothingSent()

    def test_a_stale_claim_is_taken_over(self):
        """A worker killed mid-request must not make a refund permanently
        unwritable — that would fail silently, which is the worst direction."""
        self.set_field(**{
            r.WRITEBACK_STATUS_FIELD: r.STATUS_PENDING,
            r.WRITEBACK_AT_FIELD: frappe.utils.add_to_date(
                frappe.utils.now_datetime(), minutes=-(r.STALE_CLAIM_MINUTES + 1)
            ),
        })
        result = r.write_back_refund(REFUND)
        self.assertTrue(result["ok"], result)

    def test_a_pending_claim_with_no_timestamp_is_taken_over(self):
        self.set_field(**{r.WRITEBACK_STATUS_FIELD: r.STATUS_PENDING,
                          r.WRITEBACK_AT_FIELD: None})
        self.assertTrue(r.write_back_refund(REFUND)["ok"])

    def test_a_failed_row_can_be_retried(self):
        self.set_field(**{r.WRITEBACK_STATUS_FIELD: r.STATUS_FAILED,
                          r.WRITEBACK_ERROR_FIELD: "previous failure"})
        self.assertTrue(r.write_back_refund(REFUND)["ok"])

    def test_a_skipped_row_can_be_retried_once_the_toggle_is_on(self):
        """Unlike fulfillment, Skipped is retryable here: the commonest skip is
        the store toggle being off, and there is no scheduler re-selecting rows,
        so nothing is spent by allowing it."""
        self.set_field(**{r.WRITEBACK_STATUS_FIELD: r.STATUS_SKIPPED,
                          r.WRITEBACK_ERROR_FIELD: "write-back disabled"})
        self.assertTrue(r.write_back_refund(REFUND)["ok"])

    def test_the_claim_is_committed_before_any_http(self):
        """A claim another worker cannot see is not a claim."""
        commits_at_first_call = []

        real_execute = r.execute

        def counting_execute(*a, **k):
            commits_at_first_call.append(len(frappe_stub.COMMITS))
            return real_execute(*a, **k)

        r.execute = counting_execute
        r.write_back_refund(REFUND)
        self.assertGreater(commits_at_first_call[0], 0)

    def test_the_claim_is_released_on_failure(self):
        self.responses = [ShopifyAPIError("boom")]
        r.write_back_refund(REFUND)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_FAILED)


# ── The whitelisted endpoints ─────────────────────────────────────────────────

class TestEndpoints(WritebackTestCase):
    def test_writeback_now_checks_submit_permission(self):
        checked = []
        real = frappe.has_permission
        frappe.has_permission = lambda *a, **k: checked.append((a, k)) or True
        try:
            r.writeback_now(REFUND)
        finally:
            frappe.has_permission = real
        self.assertTrue(checked, "writeback_now must check permission before paying anybody")

    def test_the_payout_function_is_not_reachable_over_http(self):
        """write_back_refund pays a customer.  Whitelisting it would put that one
        HTTP call away from anyone logged in; writeback_now is the door, and it
        checks permission."""
        self.assertFalse(
            getattr(r.write_back_refund, "__is_whitelisted__", False),
            "write_back_refund is whitelisted — a payout is one HTTP call away",
        )
        self.assertTrue(getattr(r.writeback_now, "__is_whitelisted__", False))
        self.assertTrue(
            getattr(r.get_refund_writeback_status, "__is_whitelisted__", False)
        )
        self.assertTrue(
            getattr(r.resolve_unverified_writeback, "__is_whitelisted__", False)
        )

    def test_write_back_refund_does_not_second_guess_the_caller(self):
        """Authorisation belongs to whoever dispatches it.  An earlier version
        checked submit permission here as well, and two permission models over
        one payout deadlocked: payment_portals authorises on PAYOUT_ROLES, so a
        Refund Approver without doctype submit permission passed its gate and
        failed this one — and the resulting "unknown" meant neither app would pay
        the refund."""
        real = frappe.has_permission
        frappe.has_permission = lambda *a, **k: False
        try:
            result = r.write_back_refund(REFUND)
        finally:
            frappe.has_permission = real

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outcome"], r.OUTCOME_PAID)

    def test_the_http_door_still_refuses_an_unauthorised_person(self):
        real = frappe.has_permission

        def deny(*a, **k):
            if k.get("throw"):
                raise frappe.PermissionError("not allowed")
            return False

        frappe.has_permission = deny
        try:
            with self.assertRaises(frappe.PermissionError):
                r.writeback_now(REFUND)
        finally:
            frappe.has_permission = real
        self.assertNothingSent()

    def test_write_back_refund_takes_no_settings_argument(self):
        """An HTTP caller must not be able to aim this at another store's
        credentials; the store comes from the refund's own Sales Order."""
        import inspect

        self.assertEqual(
            list(inspect.signature(r.write_back_refund).parameters),
            ["refund_name", "triggered_by"],
        )

    def test_writeback_now_returns_the_result(self):
        result = r.writeback_now(REFUND)
        self.assertEqual(result["status"], r.STATUS_DONE)

    def test_the_status_endpoint_is_read_only_and_sends_nothing(self):
        info = r.get_refund_writeback_status(REFUND)
        self.assertNothingSent()
        self.assertTrue(info["is_shopify"])
        self.assertTrue(info["can_write_back"])
        self.assertEqual(info["status"], "")

    def test_the_status_endpoint_reports_a_non_shopify_refund(self):
        frappe_stub.DB["Sales Order"]["SO-0001"]["shopify_order_id"] = ""
        info = r.get_refund_writeback_status(REFUND)
        self.assertFalse(info["is_shopify"])

    def test_the_status_endpoint_says_why_it_cannot_write_back(self):
        frappe_stub.DB["Shopify Settings"]["Test Store"]["enable_refund_writeback"] = 0
        info = r.get_refund_writeback_status(REFUND)
        self.assertFalse(info["can_write_back"])
        self.assertTrue(info["reason"])

    def test_writeback_now_reports_an_unmigrated_site_rather_than_raising(self):
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set()
        real = frappe.has_permission
        frappe.has_permission = lambda *a, **k: (_ for _ in ()).throw(
            Exception("DocType Refund Request not found")
        )
        try:
            result = r.writeback_now(REFUND)
        finally:
            frappe.has_permission = real

        self.assertIn("migrate", result["message"].lower())
        self.assertNothingSent()

    def test_the_status_endpoint_survives_an_unmigrated_site(self):
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set()
        info = r.get_refund_writeback_status(REFUND)
        self.assertFalse(info["is_shopify"])
        self.assertFalse(info["migrated"])


# ── The durable "we posted it" marker ─────────────────────────────────────────

class TestSentMarker(WritebackTestCase):
    """The `sent` flag is a local, so it cannot survive the worker.  A refund
    posted by a process that is then killed must not look like one that was
    never posted, because the stale-claim escape would re-send it — and on a
    partial refund the order still has headroom, so Shopify would pay twice.
    """

    def test_the_document_says_unverified_before_the_mutation_is_posted(self):
        """Written and committed BEFORE the risk, not after it.  Anything after
        the post cannot run if the process dies during it."""
        seen = {}
        real_execute = r.execute

        def watching_execute(settings, query, variables=None, operation="",
                             **kwargs):
            if operation == "refundCreate":
                seen["status"] = frappe_stub.get_doc_values(
                    r.REFUND_REQUEST, REFUND
                ).get(r.WRITEBACK_STATUS_FIELD)
                seen["commits"] = len(frappe_stub.COMMITS)
            return real_execute(settings, query, variables, operation, **kwargs)

        r.execute = watching_execute
        result = r.write_back_refund(REFUND)

        self.assertEqual(seen.get("status"), r.STATUS_UNVERIFIED)
        self.assertGreater(seen.get("commits", 0), 0, "the marker must be committed")
        # And the happy path still finishes as Done.
        self.assertEqual(result["status"], r.STATUS_DONE)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_DONE)

    def test_a_worker_killed_mid_mutation_leaves_it_unverified(self):
        """SystemExit models a hard kill: it is a BaseException, so none of the
        except clauses unwind it.  A real SIGKILL would not even unwind — which
        is the point of writing the marker first."""
        self.responses = [targets_response()]

        real_execute = r.execute

        def killed(settings, query, variables=None, operation="", **kwargs):
            if operation == "refundCreate":
                raise SystemExit("worker killed")
            return real_execute(settings, query, variables, operation, **kwargs)

        r.execute = killed
        with self.assertRaises(SystemExit):
            r.write_back_refund(REFUND)

        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED)
        self.assertNoGid()

    def test_a_stale_claim_on_a_killed_attempt_is_not_re_sent(self):
        """The finding, as a test.  Before the marker this re-sent the refund
        30 minutes later and paid the customer twice."""
        self.responses = [targets_response()]
        real_execute = r.execute

        def killed(settings, query, variables=None, operation="", **kwargs):
            if operation == "refundCreate":
                raise SystemExit("worker killed")
            return real_execute(settings, query, variables, operation, **kwargs)

        r.execute = killed
        with self.assertRaises(SystemExit):
            r.write_back_refund(REFUND)
        r.execute = real_execute

        # Age the row well past the staleness window, as a later caller would
        # find it, and give the order the headroom a partial refund leaves.
        self.set_field(**{
            r.WRITEBACK_AT_FIELD: frappe.utils.add_to_date(
                frappe.utils.now_datetime(), minutes=-(r.STALE_CLAIM_MINUTES * 4)
            ),
        })
        self.calls = []
        self.responses = [targets_response(), refund_created()]
        again = r.write_back_refund(REFUND)

        self.assertNothingSent()
        self.assertEqual(again["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertTrue(again["possibly_paid"])
        self.assertFalse(again["retry_safe"])
        self.assertFalse(r._claim(REFUND), "a killed attempt must not be re-claimable")

    def test_the_marker_alone_already_warns_against_retrying(self):
        """Nothing appends the fuller message when the process dies, so the text
        written before the post has to stand on its own."""
        self.responses = [targets_response()]
        real_execute = r.execute

        def killed(settings, query, variables=None, operation="", **kwargs):
            if operation == "refundCreate":
                raise SystemExit("worker killed")
            return real_execute(settings, query, variables, operation, **kwargs)

        r.execute = killed
        with self.assertRaises(SystemExit):
            r.write_back_refund(REFUND)

        recorded = self.stored(r.WRITEBACK_ERROR_FIELD).lower()
        self.assertIn("not retry", recorded)
        self.assertIn("possibly paid", recorded)

    def test_a_clean_rejection_after_the_marker_goes_back_to_failed(self):
        """userErrors and a 401/403 are decided by Shopify, so the marker must
        not leave them parked in Unverified for a person to clear by hand."""
        for responses, code in (
            ([targets_response(),
              refund_created(user_errors=[{"field": None, "message": "too large"}])],
             "rejected_by_shopify"),
            # The flag is what carries the 401/403 claim; execute() sets it, and
            # since round 3 nothing here infers it from the status code.
            ([targets_response(),
              ShopifyAPIError("forbidden", 403, proves_not_executed=True)],
             "not_authorised"),
        ):
            self.seed()
            self.responses = list(responses)
            result = r.write_back_refund(REFUND)

            self.assertEqual(result["reason_code"], code)
            self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT, code)
            self.assertEqual(
                self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_FAILED, code
            )
            self.assertTrue(result["retry_safe"], code)

    def test_a_failure_before_the_post_never_reaches_unverified(self):
        for responses in ([ShopifyAPIError("boom")], [targets_response(order=False)]):
            self.seed()
            self.responses = list(responses)
            r.write_back_refund(REFUND)
            self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_FAILED)


# ── Posted at most once ───────────────────────────────────────────────────────

class TestWhatProvesNonExecution(unittest.TestCase):
    """`_proves_not_executed` is the assertion two `failed_unsent` codes stand
    on, so it is pinned as a pure function as well as through the write-back.

    The evidence it reads is the client's own verdict,
    `ShopifyAPIError.proves_not_executed`, and nothing else.  Only the raise
    site saw the response body, and one code covers two opposite facts: a
    THROTTLED 200 refused before execution began is Shopify refusing to run the
    document, while a THROTTLED 200 that shows execution began may be hiding a
    mutation that committed.  Reading the code here gets that second one
    exactly backwards, and backwards means reporting a paid refund as
    never-sent.

    There is no second source of truth beside the flag any more, and that is
    round 3's change.  The helper used to fall back to
    `status_code in (401, 403)`, justified as belt-and-braces for "an older
    code path that predates the flag" — a path that does not exist: every raise
    in the client sets the flag and the class defaults it False.  Two sources
    of truth for one money decision is the deadlock CONTRACT_VERSION 3 already
    removed once, so the inference is gone and the tests below pin its absence.
    """

    def test_the_clients_own_verdict_is_what_proves_it(self):
        """Set by execute() on exactly two kinds of raise — 401/403, and a
        THROTTLED 200 that Shopify refused BEFORE execution began."""
        for exc in (ShopifyAPIError("returned HTTP 401 — …", 401,
                                    proves_not_executed=True),
                    ShopifyAPIError("GraphQL errors: throttled", 200,
                                    error_codes=["THROTTLED"],
                                    proves_not_executed=True),
                    ShopifyAPIError("no data", None, proves_not_executed=True)):
            self.assertTrue(r._proves_not_executed(exc), str(exc))

    def test_a_throttled_error_code_alone_proves_nothing(self):
        """The defect this replaced.  A THROTTLED 200 whose body carried `data`
        raises with the same extensions.code and the opposite meaning: the
        document may have executed and paid the customer.  Sniffing the code
        reported that as "nobody was paid, safe to retry"."""
        exc = ShopifyAPIError("GraphQL errors: throttled", 200,
                              error_codes=["THROTTLED"])
        self.assertFalse(exc.proves_not_executed)
        self.assertFalse(
            r._proves_not_executed(exc),
            "a THROTTLED code without the client's verdict was read as proof; "
            "that is the response that may already have paid the customer",
        )

    def test_a_429_proves_nothing_and_the_client_no_longer_says_it_does(self):
        """The bare HTTP 429, which is now what execute() actually raises for
        one: `proves_not_executed=False`, because Shopify throttles GraphQL
        with a 200 body — so a 429 on `graphql.json` may be a CDN, a WAF or an
        egress proxy in front of the store, and such a layer knows nothing
        about whether the document behind it ran."""
        exc = ShopifyAPIError("rate limited (429).", 429,
                              error_codes=["THROTTLED"])
        self.assertFalse(exc.proves_not_executed)
        self.assertFalse(r._proves_not_executed(exc))
        # And with no status_code fallback left, the bare form is no different.
        self.assertFalse(
            r._proves_not_executed(ShopifyAPIError("rate limited", 429))
        )

    def test_an_auth_status_code_alone_no_longer_proves_anything(self):
        """The round-3 removal, pinned so nobody reintroduces it.

        `status_code in (401, 403)` was a SECOND source of truth for the one
        decision that pays a customer, and the "older code path" it was
        defending against does not exist: execute() sets the flag on both auth
        raises, and ShopifyAPIError defaults it False.  What the fallback could
        still reach was an exception from somewhere that declined to make the
        claim — and making it on their behalf is the whole inference this
        helper exists to refuse."""
        for status in (401, 403):
            exc = ShopifyAPIError("declined", status)
            self.assertFalse(exc.proves_not_executed, status)
            self.assertFalse(
                r._proves_not_executed(exc),
                f"an HTTP {status} with no verdict from the client was read as "
                f"proof that nobody was paid",
            )

    def test_a_flagged_auth_refusal_is_still_proof(self):
        """The removal is of the inference, not of the case: the client's own
        401/403 raise carries the flag, and that is what classifies it."""
        for status in (401, 403):
            self.assertTrue(
                r._proves_not_executed(
                    ShopifyAPIError("declined", status, proves_not_executed=True)
                ),
                status,
            )

    def test_a_lost_answer_proves_nothing(self):
        """The double-pay cases, and the reason the default is False."""
        for exc in (ShopifyAPIError("connection reset"),
                    ShopifyAPIError("read timed out"),
                    ShopifyAPIError("returned HTTP 500.", 500),
                    ShopifyAPIError("returned HTTP 502.", 502),
                    ShopifyAPIError("errors", 200,
                                    error_codes=["INTERNAL_SERVER_ERROR"])):
            self.assertFalse(r._proves_not_executed(exc), str(exc))

    def test_it_never_raises_on_an_exception_carrying_neither_attribute(self):
        """It is called inside the handler that decides whether a customer was
        paid, so a bare Exception has to read as "proves nothing" rather than
        blow that handler up."""
        self.assertFalse(r._proves_not_executed(Exception("boom")))

    def test_nothing_but_the_explicit_proof_is_consulted_at_all(self):
        """A structural tripwire on the BODY, not the docstring — which talks
        about `error_codes` and about 401 at length, because saying why each
        inference is gone is the only thing that stops it coming back as an
        `or` beside the flag.  The flag is False on exactly the responses those
        inferences fire on, so such an `or` restores the defect while reading
        as a safety belt."""
        import inspect

        source = inspect.getsource(r._proves_not_executed)
        body = source.split('"""')[-1]
        self.assertIn("proves_not_executed", body)
        self.assertNotIn("error_codes", body)
        self.assertNotIn("THROTTLED", body)
        self.assertNotIn(
            "status_code", body,
            "the status-code inference is back; two sources of truth for one "
            "payout decision is what round 3 removed",
        )
        self.assertNotIn("401", body)
        self.assertNotIn("403", body)


class TestPostedAtMostOnce(WritebackTestCase):
    """The delivery guarantee every post-send claim in this module rests on.

    `build_refund_mutation()` is called with no key, so the `@idempotent`
    directive is absent and Shopify cannot recognise a second POST of the same
    document as the same refund.  `idempotent=False` is what makes the mutation
    posted at most once in any way that could have executed, and it is what
    makes the classification below sound rather than hopeful: a `userErrors`
    answer, a 401 or an exhausted 429 now describe the ONLY request that could
    have run.

    Under `execute()`'s default retries they described the last of up to five
    attempts, and the scenario a code review named is this: attempt 1 creates
    the refund, its response dies in the socket timeout, attempt 2 is declined
    with "Refund amount exceeds the amount refundable on this order" *precisely
    because attempt 1 consumed the headroom* — and `write_back_refund` then
    wrote `Failed` over the durable `Unverified` marker, returned
    `retry_safe: true`, and `refund_request.js` offered "Retry Shopify Refund"
    on a refund the customer had already been paid.
    """

    def test_the_mutation_is_posted_at_most_once(self):
        """The structural guard, and it asserts on the argument `execute()`
        actually received rather than on the source, because here the argument
        *is* the behaviour."""
        result = r.write_back_refund(REFUND)

        self.assertTrue(result["ok"], result)
        self.assertIs(
            self.posted_kwargs("refundCreate").get("idempotent"), False,
            "refundCreate was posted with the ordinary retries; a lost response "
            "on attempt 1 then pays the customer a second time",
        )

    def test_the_modules_own_comments_do_not_overstate_the_guarantee(self):
        """The comments at the post and at CONTRACT_VERSION 5 said flatly that
        "refundCreate is posted AT MOST ONCE".  It is not: an exhausted 429
        posts the identical mutation five times, and every one of them is
        refused — which is exactly why it is safe.  The property is at most one
        post that could have EXECUTED, and every post-send "nobody was paid"
        verdict rests on the refusal rather than on the count.

        A reader who takes the headline at face value is reasoning from
        something false, and the thing they would most plausibly do with it is
        restore `execute()`'s default retries here."""
        import re

        source = open(r.__file__, encoding="utf-8").read()
        for match in re.finditer(r"(?i)at most once", source):
            tail = " ".join(source[match.end():match.end() + 160].split())
            self.assertIn(
                "execut", tail.lower(),
                f"an unqualified 'at most once' in refund.py: "
                f"...{tail[:120]}...",
            )

    def test_the_targets_query_keeps_its_retries(self):
        """`RefundTargets` is a read.  Re-posting it cannot pay anybody, so it
        keeps `execute()`'s resilience — trading a safe retry for a fragile one
        buys nothing and costs every transient 5xx a refusal somebody has to
        re-drive by hand."""
        r.write_back_refund(REFUND)

        self.assertIsNot(
            self.posted_kwargs("RefundTargets").get("idempotent"), False,
            "the RefundTargets read lost its retries; only the mutation needs "
            "the at-most-once rule",
        )

    def test_user_errors_after_send_are_unsent_on_a_single_post(self):
        """The classification was always right and was not sound: with retries
        on, `check_user_errors` only ever saw the last of up to five attempts,
        so "Shopify read this and declined it" could be a rejection *caused* by
        an earlier attempt that had already paid the customer."""
        self.responses = [
            targets_response(),
            refund_created(user_errors=[
                {"field": ["input", "transactions", "0", "amount"],
                 "message": "Refund amount exceeds the amount refundable on "
                            "this order."},
            ]),
        ]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)
        self.assertEqual(result["reason_code"], "rejected_by_shopify")
        self.assertTrue(result["retry_safe"])
        self.assertFalse(result["possibly_paid"])
        self.assertEqual(result["status"], r.STATUS_FAILED)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_FAILED)
        self.assertNoGid()
        # And the claim above is true only because of this argument.
        self.assertIs(self.posted_kwargs("refundCreate").get("idempotent"), False)

    def test_a_bare_429_after_send_is_possibly_paid_not_rate_limited(self):
        """Round 3's correction, and it is a de-escalation the other way.

        `execute()` now raises a bare HTTP 429 with `proves_not_executed=False`
        and does not re-post it for a non-idempotent document: Shopify's own
        GraphQL throttling arrives as a 200 body, so a 429 on `graphql.json` is
        as likely to be a CDN, a WAF or an egress proxy in FRONT of the store,
        and such a layer knows nothing about whether the mutation behind it
        ran.  So this falls through to `fail_unknown` and lands on
        `Unverified`, where a person decides — deliberately, at the cost of
        somebody opening the order in Shopify.  The alternative asserted a
        premise about infrastructure nobody here controls, and the price of
        that premise being wrong is a second real refund."""
        self.responses = [
            targets_response(),
            ShopifyAPIError("refundCreate rate limited (429).", 429,
                            error_codes=["THROTTLED"]),
        ]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_UNVERIFIED)
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertNotEqual(
            result["reason_code"], "rate_limited",
            "an HTTP-layer 429 was reported as Shopify's own refusal, which is "
            "retry-safe — on a mutation that may have executed",
        )
        # Shopify (or something wearing its clothes) ANSWERED, so it is the
        # answered-but-unusable code rather than a transport failure.
        self.assertEqual(result["reason_code"], "response_unverifiable")
        self.assertTrue(result["possibly_paid"])
        self.assertFalse(result["retry_safe"])
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED)
        self.assertNoGid()

    def test_shopifys_own_pre_execution_refusal_is_what_rate_limited_means(self):
        """The one remaining route to `rate_limited`, and the only thing it
        means now: HTTP 200, `errors[].extensions.code` of `THROTTLED`, in the
        shape the GraphQL spec reserves for a request refused BEFORE execution
        began — no `data` key and no `errors[].path`.  `execute()` re-posts
        exactly that, even for this non-idempotent document, and raises with
        `proves_not_executed=True` once the attempts run out.

        The flag is the only thing that separates it from the THROTTLED below
        that may have committed: same extensions.code, opposite meaning."""
        self.responses = [
            targets_response(),
            ShopifyAPIError("refundCreate returned GraphQL errors: throttled",
                            200, error_codes=["THROTTLED"],
                            proves_not_executed=True),
        ]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)
        self.assertEqual(result["reason_code"], "rate_limited")
        self.assertTrue(result["retry_safe"])
        self.assertFalse(result["possibly_paid"])
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_FAILED)
        self.assertNoGid()

    def test_a_throttle_that_may_have_committed_is_possibly_paid(self):
        """The one the round-1 fix got backwards, and the dangerous direction.

        A GraphQL document can resolve part way and then blow the cost budget,
        so Shopify answers 200 with BOTH a populated `data` and a THROTTLED
        error.  `refundCreate` resolves a `transactions(first: 10)` connection,
        which has real query cost, so that shape is reachable on the very
        mutation that pays a customer.  `execute()` refuses to re-post it and
        raises with `proves_not_executed=False` — the same extensions.code as a
        pure refusal and the opposite meaning.  It must land where a person
        looks, never as retry-safe."""
        self.responses = [
            targets_response(),
            ShopifyAPIError("refundCreate returned GraphQL errors: throttled",
                            200, error_codes=["THROTTLED"]),
        ]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_UNVERIFIED)
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertNotEqual(
            result["reason_code"], "rate_limited",
            "a THROTTLED response that may carry a committed refund was "
            "reported as an ordinary rate limit, which is retry-safe",
        )
        self.assertEqual(result["reason_code"], "response_unverifiable")
        self.assertTrue(result["possibly_paid"])
        self.assertFalse(result["retry_safe"])
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED)
        self.assertNoGid()

    def test_a_401_after_send_is_still_not_authorised(self):
        """Unchanged behaviour, now carrying the guarantee its comment always
        asserted: rejected at the auth layer, before the document ran.

        The fixture carries `proves_not_executed=True` because that is what
        `execute()` raises for a 401 — and since round 3 it is the ONLY thing
        that classifies it."""
        self.responses = [
            targets_response(),
            ShopifyAPIError("unauthorized", 401, proves_not_executed=True),
        ]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)
        self.assertEqual(result["reason_code"], "not_authorised")
        self.assertTrue(result["retry_safe"])
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_FAILED)

    def test_a_401_that_carries_no_verdict_is_not_read_as_proof(self):
        """The removal of the status-code inference, end to end.

        Only the raise site knows whether Shopify answered INSTEAD of running
        the document, so a 401 arriving here without the client's verdict came
        from somewhere that declined to make that claim — a wrapper, a mock, a
        future code path — and this side must not make it on their behalf.  It
        is the safe direction: `Unverified` costs somebody opening the order in
        Shopify, while the inference costs a second real refund."""
        self.responses = [targets_response(), ShopifyAPIError("unauthorized", 401)]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(result["reason_code"], "response_unverifiable")
        self.assertFalse(result["retry_safe"])
        self.assertTrue(result["possibly_paid"])
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED)

    def test_a_500_after_send_is_unverified_and_possibly_paid(self):
        """The line the whole distinction is drawn against.  Shopify's edge can
        answer 502 or 503 after the mutation has committed, so a 5xx proves
        nothing and stays a person's problem — and `idempotent=False` also means
        `execute()` no longer retries it.

        It is `response_unverifiable` rather than `transport_error_after_send`:
        Shopify answered, the answer was unusable.  The transport worked."""
        self.responses = [
            targets_response(),
            ShopifyAPIError("refundCreate returned HTTP 500.", 500),
        ]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_UNVERIFIED)
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(result["reason_code"], "response_unverifiable")
        self.assertTrue(result["possibly_paid"])
        self.assertFalse(result["retry_safe"])
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED)
        self.assertNoGid()

    def test_a_transport_error_after_send_is_unverified(self):
        """A timeout or a reset says the answer never came back, not that the
        document never ran."""
        self.responses = [targets_response(), ShopifyAPIError("connection reset")]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_UNVERIFIED)
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(result["reason_code"], "transport_error_after_send")
        self.assertTrue(result["possibly_paid"])
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED)

    def test_the_two_unknown_codes_split_on_whether_shopify_ANSWERED(self):
        """Both are `failed_unknown` and both mean "a person must look", but
        they are different facts and the contract's §6 table defines them
        narrowly.  No status_code on the exception means the transport itself
        failed and nothing was read back; a status_code means Shopify answered
        something this side could not use.  Filing an HTTP 400 or 404 as a
        transport error tells a reader the network broke, and sends them to
        look at the wrong thing."""
        answered = {
            400: "refundCreate returned HTTP 400: bad request",
            404: "refundCreate returned HTTP 404: no such order",
            500: "refundCreate returned HTTP 500.",
            # The bare 429 joins them in round 3: something answered, it just
            # said nothing about whether the mutation ran.
            429: "refundCreate rate limited (429).",
            200: "refundCreate returned GraphQL errors: something else",
        }
        for status, message in answered.items():
            self.seed()
            self.responses = [targets_response(),
                              ShopifyAPIError(message, status)]
            result = r.write_back_refund(REFUND)
            self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN, status)
            self.assertEqual(result["reason_code"], "response_unverifiable", status)

        # Only a genuine transport failure carries no status at all.  The
        # missing-mutation-key raise used to be listed here and is not one: it
        # now comes back with status_code 200, and the test below is where it
        # belongs.
        for message in ("connection reset by peer", "read timed out",
                        "refundCreate failed: HTTPSConnectionPool read timeout"):
            self.seed()
            self.responses = [targets_response(), ShopifyAPIError(message)]
            result = r.write_back_refund(REFUND)
            self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN, message)
            self.assertEqual(
                result["reason_code"], "transport_error_after_send", message
            )

    def test_a_missing_mutation_key_is_an_answer_not_a_broken_network(self):
        """`check_user_errors` is the REAL one here, so this pins the shape it
        raises rather than a fixture's guess at it.

        It only ever sees a body execute() already accepted and parsed, which
        execute() reaches only on a 2xx with a non-null `data` — Shopify
        ANSWERED, in full, and the answer simply did not contain the mutation.
        The raise therefore carries status_code 200, and this side splits
        "the transport failed" from "Shopify answered something unusable" on
        `status_code is None`: raising bare filed it as
        `transport_error_after_send` and sent the reader to look at the network
        on a request that had come back."""
        self.responses = [targets_response(), {"somethingElse": {}}]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(
            result["reason_code"], "response_unverifiable",
            "a response Shopify sent in full was reported as a transport error",
        )
        self.assertTrue(result["possibly_paid"])
        self.assertFalse(result["retry_safe"])
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED)
        self.assertNoGid()

    def test_rate_limited_belongs_to_the_unsent_outcome_and_nothing_else(self):
        """A new slug in a closed vocabulary payment_portals branches on, so it
        has to sit in exactly one outcome — the one that means nobody was
        paid."""
        self.assertIn("rate_limited", r.REASON_CODES[r.OUTCOME_FAILED_UNSENT])
        for outcome, codes in r.REASON_CODES.items():
            if outcome != r.OUTCOME_FAILED_UNSENT:
                self.assertNotIn("rate_limited", codes, outcome)


# ── The warning must survive truncation ───────────────────────────────────────

class TestUnverifiedMessage(WritebackTestCase):
    def test_a_huge_upstream_error_cannot_push_the_warning_out_of_the_field(self):
        """_release_claim stores only the first 1000 characters, so the warning
        goes first — appended, a verbose GraphQL error pushed it off the end of
        the one field a person reads to learn not to retry."""
        self.responses = [
            targets_response(),
            ShopifyAPIError("refundCreate returned GraphQL errors: " + "x" * 4000),
        ]
        r.write_back_refund(REFUND)

        recorded = self.stored(r.WRITEBACK_ERROR_FIELD)
        self.assertLessEqual(len(recorded), 1000)
        self.assertIn("POSSIBLY PAID", recorded)
        self.assertIn("not retry", recorded.lower())


# ── Unverified: blocked, and not a dead end ───────────────────────────────────

class TestUnverified(WritebackTestCase):
    """Unverified means the mutation went out and its fate is unknown.  Nothing
    automatic may touch it, and a person must be able to close it out."""

    def unverify(self):
        self.set_field(**{
            r.WRITEBACK_STATUS_FIELD: r.STATUS_UNVERIFIED,
            r.WRITEBACK_ERROR_FIELD: "POSSIBLY PAID — do not retry.",
        })

    def test_an_unverified_row_sends_nothing(self):
        """The whole point: retrying could pay the customer twice."""
        self.unverify()
        result = r.write_back_refund(REFUND)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], r.STATUS_UNVERIFIED)
        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
        self.assertFalse(result["retry_safe"])
        self.assertTrue(result["possibly_paid"])
        self.assertNothingSent()

    def test_an_unverified_row_is_not_downgraded_to_skipped(self):
        """Skipped reads as "nothing happened", which is exactly what is not
        known here."""
        self.unverify()
        r.write_back_refund(REFUND)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED)

    def test_the_claim_also_refuses_it(self):
        """Two callers can both clear eligibility before either writes; the claim
        is the layer that has to hold."""
        self.unverify()
        self.assertFalse(r._claim(REFUND))

    def test_resolving_as_paid_records_the_gid_and_marks_it_done(self):
        self.unverify()
        result = r.resolve_unverified_writeback(
            REFUND, "paid", shopify_refund_gid="1234567890", gateway="manual",
            note="Found in Shopify admin.",
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.stored(r.REFUND_GID_FIELD), REFUND_GID)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_DONE)
        self.assertEqual(self.stored(r.REFUND_GATEWAY_FIELD), "manual")

    def test_a_bare_numeric_id_is_stored_as_a_gid(self):
        """It has to match what the credit-note guard looks for."""
        self.unverify()
        r.resolve_unverified_writeback(REFUND, "paid", shopify_refund_gid="1234567890")
        self.assertEqual(
            r.refund_request_for_shopify_refund("1234567890"), REFUND
        )

    def test_a_well_formed_refund_gid_is_accepted_as_given(self):
        """The other accepted form: what a person copies out of the Shopify
        admin's URL or API response."""
        self.unverify()
        result = r.resolve_unverified_writeback(
            REFUND, "paid", shopify_refund_gid=REFUND_GID
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.stored(r.REFUND_GID_FIELD), REFUND_GID)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_DONE)

    def test_a_value_that_is_not_a_refund_id_is_refused(self):
        """The only exit from Unverified, and two loop guards now lean on what
        it writes: `refund_request_for_shopify_refund` matches the GID, and
        `unverified_writebacks_for_order` stops matching once the status moves
        to Done.  `gid()` passes anything already starting with `gid://`
        straight through, so a mis-paste — another resource type, another
        store's refund, a truncated id — used to be stored verbatim and then
        permanently satisfied BOTH guards for that order: the refunds/create
        webhook for the real refund would match nothing, be reported as an
        externally-made refund, and earn a second Payment Entry.

        Refusing costs a retype.  Accepting costs a refund that is never
        recorded, so the message has to say what a correct value looks like."""
        self.unverify()
        for value in (
            "gid://shopify/Product/1234567890",      # right store, wrong type
            "gid://shopify/Order/1234567890",
            "gid://shopify/Refund/",                 # truncated to nothing
            "gid://shopify/Refund",
            "gid://shopify/Refund/12345abc",
            "gid://shopify/Refund/1234567890/extra",
            "REF-00207",                             # the ERPNext name, not the id
            "#6518",                                 # the order name
            "https://admin.shopify.com/store/x/orders/6518",
            "12,345",
            "1234-5678",
            "yes it is there",
        ):
            result = r.resolve_unverified_writeback(
                REFUND, "paid", shopify_refund_gid=value
            )

            self.assertFalse(result["ok"], value)
            self.assertIn(
                "gid://shopify/Refund/", result["message"],
                f"the refusal for {value!r} does not say what a correct value "
                f"looks like",
            )
            self.assertEqual(
                self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED, value
            )
            self.assertFalse(
                (self.stored(r.REFUND_GID_FIELD) or "").strip(),
                f"{value!r} was stored as a refund id",
            )

    def test_a_padded_numeric_id_is_still_a_numeric_id(self):
        """Refusing has to cost a retype, not a puzzle: surrounding whitespace
        from a copy-paste is not a mis-paste."""
        self.unverify()
        result = r.resolve_unverified_writeback(
            REFUND, "paid", shopify_refund_gid="  1234567890  "
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.stored(r.REFUND_GID_FIELD), REFUND_GID)

    def test_a_refused_gid_leaves_the_row_resolvable(self):
        """A refusal must not consume the one exit: after the retype it works."""
        self.unverify()
        r.resolve_unverified_writeback(
            REFUND, "paid", shopify_refund_gid="gid://shopify/Product/1234567890"
        )
        result = r.resolve_unverified_writeback(
            REFUND, "paid", shopify_refund_gid="1234567890"
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.stored(r.REFUND_GID_FIELD), REFUND_GID)

    def test_resolving_as_paid_without_a_gid_is_refused(self):
        """A Done row with no GID would let the refunds/create webhook build a
        second Credit Note."""
        self.unverify()
        result = r.resolve_unverified_writeback(REFUND, "paid")

        self.assertFalse(result["ok"])
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED)
        self.assertFalse(self.stored(r.REFUND_GID_FIELD))

    def test_resolving_as_not_paid_clears_it_for_another_attempt(self):
        self.unverify()
        result = r.resolve_unverified_writeback(REFUND, "not_paid")

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), "")
        self.assertFalse(self.stored(r.REFUND_GID_FIELD))
        # And it really is sendable again.
        self.assertTrue(r.write_back_refund(REFUND)["ok"])

    def test_the_resolution_records_who_decided_and_which_way(self):
        """A call about whether a customer has been paid, made without the
        evidence in hand.  It should not be anonymous."""
        self.unverify()
        r.resolve_unverified_writeback(REFUND, "not_paid", note="Checked #6518.")
        recorded = self.stored(r.WRITEBACK_ERROR_FIELD)

        self.assertIn("NOT PAID", recorded)
        self.assertIn("Administrator", recorded)
        self.assertIn("Checked #6518.", recorded)

    def test_an_unknown_resolution_is_refused(self):
        self.unverify()
        for resolution in ("", None, "maybe", "done"):
            result = r.resolve_unverified_writeback(REFUND, resolution)
            self.assertFalse(result["ok"], resolution)
            self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_UNVERIFIED)

    def test_it_refuses_any_status_other_than_unverified(self):
        """Not a general status-fixing tool: pointed at a Done row it would
        overwrite a real GID with a hand-typed one."""
        for status in ("", r.STATUS_DONE, r.STATUS_FAILED, r.STATUS_PENDING,
                       r.STATUS_SKIPPED):
            self.seed()
            self.set_field(**{r.WRITEBACK_STATUS_FIELD: status,
                              r.REFUND_GID_FIELD: "gid://shopify/Refund/original"})
            result = r.resolve_unverified_writeback(
                REFUND, "paid", shopify_refund_gid="9999"
            )
            self.assertFalse(result["ok"], status)
            self.assertEqual(
                self.stored(r.REFUND_GID_FIELD), "gid://shopify/Refund/original", status
            )

    def test_it_reports_an_unmigrated_site_rather_than_raising(self):
        """Availability before permission: frappe.has_permission on a DocType
        that does not exist raises, and "you lack permission" is the wrong
        diagnosis for "this app is inert here"."""
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set()
        real = frappe.has_permission
        frappe.has_permission = lambda *a, **k: (_ for _ in ()).throw(
            Exception("DocType Refund Request not found")
        )
        try:
            result = r.resolve_unverified_writeback(REFUND, "not_paid")
        finally:
            frappe.has_permission = real

        self.assertFalse(result["ok"])
        self.assertIn("migrate", result["message"].lower())

    def test_it_needs_submit_permission(self):
        self.unverify()
        real = frappe.has_permission
        calls = []
        frappe.has_permission = lambda *a, **k: calls.append(k) or True
        try:
            r.resolve_unverified_writeback(REFUND, "not_paid")
        finally:
            frappe.has_permission = real
        self.assertTrue(any(k.get("throw") for k in calls), calls)


# ── The credit-note loop ──────────────────────────────────────────────────────

class TestCreditNoteLoopGuard(WritebackTestCase):
    """Our own write fires refunds/create, and the webhook handler would create a
    second Credit Note for a refund ERPNext already has."""

    def test_a_refund_we_wrote_is_recognised_by_its_gid(self):
        frappe_stub.DB[r.REFUND_REQUEST][REFUND][r.REFUND_GID_FIELD] = REFUND_GID
        self.assertEqual(
            r.refund_request_for_shopify_refund("1234567890"), REFUND
        )

    def test_the_gid_form_is_recognised_too(self):
        frappe_stub.DB[r.REFUND_REQUEST][REFUND][r.REFUND_GID_FIELD] = REFUND_GID
        self.assertEqual(r.refund_request_for_shopify_refund(REFUND_GID), REFUND)

    def test_a_refund_we_did_not_write_is_not_recognised(self):
        self.assertIsNone(r.refund_request_for_shopify_refund("9999999999"))

    def test_a_blank_refund_id_is_not_recognised(self):
        for value in ("", None, "   "):
            self.assertIsNone(r.refund_request_for_shopify_refund(value))

    def test_it_is_inert_without_the_refund_request_doctype(self):
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set()
        self.assertIsNone(r.refund_request_for_shopify_refund("1234567890"))

    def test_the_credit_note_path_returns_early_for_our_own_refund(self):
        from shopify_integration.utils import credit_note as cn

        frappe_stub.DB[r.REFUND_REQUEST][REFUND][r.REFUND_GID_FIELD] = REFUND_GID
        result = cn.create_credit_note_from_shopify_refund(
            {"id": "1234567890", "order_id": ORDER_ID}, self.settings
        )

        self.assertIsNone(result)
        self.assertEqual(
            [d for d in frappe_stub.INSERTS if d.get("doctype") == "Sales Invoice"], []
        )


class TestUnverifiedWritebacksForOrder(WritebackTestCase):
    """The other half of the loop guard, for the window the GID cannot cover.

    `refund_request_for_shopify_refund` matches on `shopify_refund_gid`, and
    that field is only written once a successful `refundCreate` response has
    been READ.  Everything committed before the post is the `Unverified` marker
    with the GID field still empty — so every `failed_unknown` outcome (worker
    killed after the post, a timeout, a 5xx, "Shopify accepted the request but
    returned no refund object") leaves a row that IS ours and carries no GID,
    while Shopify may genuinely hold the refund.

    This lookup answers "which unresolved write-backs of ours are on this
    order?", which is the only question available in that window.  Plural since
    round 3: the singular form returned one arbitrary row via
    `frappe.db.get_value`, so an order carrying two unconfirmed write-backs
    named only one of them — and the person sent to resolve it cleared one row
    while the other went on withholding reports with nothing to point at.
    """

    def unverify(self, sales_order="SO-0001"):
        self.set_field(**{
            "sales_order": sales_order,
            r.WRITEBACK_STATUS_FIELD: r.STATUS_UNVERIFIED,
            r.REFUND_GID_FIELD: "",
        })

    def second_unverified(self, name="REF-0008", sales_order="SO-0001",
                          docstatus=1):
        """A second unconfirmed write-back on the same Shopify order."""
        frappe_stub.set_doc(r.REFUND_REQUEST, name, {
            "name": name,
            "docstatus": docstatus,
            "status": "Approved",
            "refund_channel": r.CHANNEL_DISPATCH,
            "payment_entry": "",
            "sales_order": sales_order,
            "net_refund_amount": 500.0,
            "reason_note": "Second unconfirmed attempt",
            r.REFUND_GID_FIELD: "",
            r.WRITEBACK_STATUS_FIELD: r.STATUS_UNVERIFIED,
        })
        return name

    def test_an_unverified_row_on_the_order_is_found(self):
        install_sales_order_lookup(self)
        self.unverify()
        self.assertEqual(r.unverified_writebacks_for_order(ORDER_ID), [REFUND])

    def test_two_unconfirmed_writebacks_on_one_order_are_both_named(self):
        """The reason this is plural.  Two rows can be Unverified on the same
        order — a first attempt that lost its answer, then a second refund
        raised for the rest of the order that lost its answer too — and
        `frappe.db.get_value` returned whichever the database offered first.
        Naming one of two tells a person the order is clear once they have
        resolved it, while the refund the unnamed row may have paid is the one
        nothing ever records."""
        install_sales_order_lookup(self)
        self.unverify()
        second = self.second_unverified()

        self.assertEqual(
            r.unverified_writebacks_for_order(ORDER_ID),
            sorted([REFUND, second]),
            "an order with two unconfirmed write-backs named only one of them",
        )

    def test_the_order_of_the_result_is_deterministic(self):
        """Two callers reading the same order must be told the same thing, and
        a log line that reshuffles between runs cannot be diffed."""
        install_sales_order_lookup(self)
        self.unverify()
        self.second_unverified(name="REF-0002")
        self.second_unverified(name="REF-0009")

        first = r.unverified_writebacks_for_order(ORDER_ID)
        self.assertEqual(first, r.unverified_writebacks_for_order(ORDER_ID))
        self.assertEqual(first, sorted(first))

    def test_a_cancelled_refund_request_is_not_a_hit(self):
        """The docstatus filter, and it is not hypothetical: cancelling the
        stuck request is exactly what an operator reaches for when a row will
        not clear.  A cancelled Refund Request is no longer ERPNext's record of
        anything, so it must stop withholding refund reports for the order —
        otherwise the refund Shopify really made is never recorded anywhere,
        which is the omission nothing can recover."""
        install_sales_order_lookup(self)
        self.unverify()
        self.set_field(docstatus=2)
        self.assertEqual(r.unverified_writebacks_for_order(ORDER_ID), [])

    def test_a_draft_refund_request_is_not_a_hit_either(self):
        """Same filter, the other side of it.  A draft was never submitted, so
        nothing dispatched from it and no mutation was ever posted."""
        install_sales_order_lookup(self)
        self.unverify()
        self.set_field(docstatus=0)
        self.assertEqual(r.unverified_writebacks_for_order(ORDER_ID), [])

    def test_a_cancelled_row_does_not_hide_a_live_one(self):
        """The filter must narrow the answer, not replace it."""
        install_sales_order_lookup(self)
        self.unverify()
        live = self.second_unverified(name="REF-0009")
        self.set_field(docstatus=2)
        self.assertEqual(r.unverified_writebacks_for_order(ORDER_ID), [live])

    def test_a_row_in_any_other_state_is_not_a_hit(self):
        """Done, Failed, Skipped and Pending are all resolved as far as this
        question goes: Done has a GID for the other lookup to match, and the
        rest were never posted or were cleanly refused."""
        install_sales_order_lookup(self)
        for status in ("", r.STATUS_DONE, r.STATUS_FAILED, r.STATUS_SKIPPED,
                       r.STATUS_PENDING):
            self.set_field(**{r.WRITEBACK_STATUS_FIELD: status})
            self.assertEqual(
                r.unverified_writebacks_for_order(ORDER_ID), [], status
            )

    def test_an_unverified_row_on_a_different_order_is_not_a_hit(self):
        """The lookup is order-scoped, and has to be: an unresolved write-back
        on one order says nothing about a refund somebody made on another."""
        install_sales_order_lookup(self)
        self.unverify()
        self.assertEqual(r.unverified_writebacks_for_order("9999999999999"), [])

    def test_an_amended_sales_order_is_still_the_same_shopify_order(self):
        """A Sales Order amended after the Refund Request was raised leaves two
        documents carrying the same shopify_order_id, and the refund points at
        one of them.  Matching a single Sales Order would miss the row and
        report our own possibly-paid refund as an external one."""
        install_sales_order_lookup(self)
        frappe_stub.set_doc("Sales Order", "SO-0001-1", {
            "shopify_order_id": ORDER_ID,
            "shopify_store": "notdrones.myshopify.com",
        })
        self.unverify(sales_order="SO-0001-1")
        self.assertEqual(r.unverified_writebacks_for_order(ORDER_ID), [REFUND])

    def test_both_halves_of_an_amended_order_are_reported(self):
        """The amendment and the original can each carry an unconfirmed
        write-back, and those are two refunds, not one."""
        install_sales_order_lookup(self)
        frappe_stub.set_doc("Sales Order", "SO-0001-1", {
            "shopify_order_id": ORDER_ID,
            "shopify_store": "notdrones.myshopify.com",
        })
        self.unverify()
        amended = self.second_unverified(sales_order="SO-0001-1")

        self.assertEqual(
            r.unverified_writebacks_for_order(ORDER_ID),
            sorted([REFUND, amended]),
        )

    def test_it_is_inert_without_the_refund_request_doctype(self):
        """Same rule as every other entry point: this module stays installable
        on a site with no payment_portals, so the answer there is an empty list
        rather than an exception inside a webhook."""
        install_sales_order_lookup(self)
        self.unverify()
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set()
        self.assertEqual(r.unverified_writebacks_for_order(ORDER_ID), [])

    def test_a_blank_order_id_is_never_a_hit(self):
        """A refund whose order we could not read must not be claimed by a row
        whose Sales Order link is empty."""
        install_sales_order_lookup(self)
        self.unverify()
        for value in ("", None, "   "):
            self.assertEqual(
                r.unverified_writebacks_for_order(value), [], repr(value)
            )

    def test_an_order_with_no_sales_order_at_all_is_not_a_hit(self):
        install_sales_order_lookup(self)
        self.unverify()
        self.assertEqual(r.unverified_writebacks_for_order("404040404"), [])

    def test_it_never_raises_when_the_lookup_itself_fails(self):
        """It is consulted from inside a webhook that must return 200 about a
        refund that has already happened, so a guard that cannot decide
        answers "no hit" and says why in the Error Log."""
        self.unverify()
        real = frappe.get_all

        def _boom(*a, **k):
            raise RuntimeError("no such table")

        frappe.get_all = _boom
        try:
            self.assertEqual(r.unverified_writebacks_for_order(ORDER_ID), [])
        finally:
            frappe.get_all = real
        self.assertTrue(
            [t for _, t in frappe_stub.ERRORS if "Unverified" in t],
            [t for _, t in frappe_stub.ERRORS],
        )

    def test_the_state_written_before_the_post_is_exactly_what_this_finds(self):
        """End to end, and the reason this function exists: after a post-send
        failure the row is Unverified with NO GID, so the GID lookup misses and
        this one hits."""
        install_sales_order_lookup(self)
        self.responses = [targets_response(), ShopifyAPIError("connection reset")]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_UNVERIFIED)
        self.assertFalse(self.stored(r.REFUND_GID_FIELD))
        self.assertIsNone(r.refund_request_for_shopify_refund("1234567890"))
        self.assertEqual(r.unverified_writebacks_for_order(ORDER_ID), [REFUND])

    def test_resolving_the_row_ends_the_hit(self):
        """`resolve_unverified_writeback` is the way out of the window: as paid
        it writes the GID the other lookup matches, and as not_paid it clears
        the status so nothing is withheld any more."""
        install_sales_order_lookup(self)
        self.unverify()
        r.resolve_unverified_writeback(REFUND, "paid",
                                       shopify_refund_gid="1234567890")
        self.assertEqual(r.unverified_writebacks_for_order(ORDER_ID), [])
        self.assertEqual(r.refund_request_for_shopify_refund("1234567890"), REFUND)

        self.seed()
        install_sales_order_lookup(self)
        self.unverify()
        r.resolve_unverified_writeback(REFUND, "not_paid")
        self.assertEqual(r.unverified_writebacks_for_order(ORDER_ID), [])

    def test_resolving_one_of_two_leaves_the_other_named(self):
        """The consequence of the plural shape: a person who clears one row is
        still told the order is withheld, and by which document."""
        install_sales_order_lookup(self)
        self.unverify()
        second = self.second_unverified()
        r.resolve_unverified_writeback(REFUND, "not_paid")

        self.assertEqual(r.unverified_writebacks_for_order(ORDER_ID), [second])

    def test_the_singular_form_is_gone(self):
        """A tripwire, because the two shapes differ in the direction that
        loses a row: `unverified_writeback_for_order` returned one arbitrary
        match, so a caller left on it goes on naming one of two.  Its absence
        is what forces every caller to be updated rather than to keep
        working while answering less than it is asked."""
        self.assertFalse(
            hasattr(r, "unverified_writeback_for_order"),
            "the singular lookup is back; it names one of two unconfirmed "
            "write-backs and the other keeps withholding reports silently",
        )


class TestTheSafeProbeNeverReachesTheMutation(WritebackTestCase):
    """What the section 10 "safe probe" actually exercises.

    REFUND-WRITEBACK-BRIEF.md section 10 step 2 proposed a first live exercise
    against an already-fully-refunded order (#6518 or #6491), on the grounds
    that Shopify "must refuse with a userErrors about exceeding the refundable
    amount", and that this "exercises credentials, query, mutation and error
    handling".

    The first half is true and the second is not.  This app's own headroom guard
    refuses first, before refundCreate is posted at all -- so the mutation and
    the userErrors path are never entered, and neither is the Unverified
    pre-commit that makes a killed worker safe.

    That matters beyond documentation tidiness: the guard is strictly more
    conservative than Shopify's, so any order on which refundCreate actually
    posts is an order Shopify will accept -- and a real customer gets paid.
    There is therefore no safe live exercise of the mutation, which is what both
    open items in payment_portals' handoff assumed there was.
    """

    def fully_refunded_order(self):
        """#6518 after its refund: a SALE parent with zero headroom left."""
        return targets_response(transactions=[{
            "id": "gid://shopify/OrderTransaction/99",
            "kind": "SALE",
            "status": "SUCCESS",
            "gateway": "manual",
            "formattedGateway": "Manual",
            "amountSet": {"presentmentMoney": {"amount": "12999.00",
                                               "currencyCode": "INR"}},
            "maximumRefundableV2": {"amount": "0.00", "currencyCode": "INR"},
            "parentTransaction": None,
        }])

    def test_an_already_refunded_order_is_refused_before_anything_is_sent(self):
        self.responses = [self.fully_refunded_order()]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)
        self.assertEqual(result["reason_code"], "no_refundable_transactions")
        self.assertTrue(result["retry_safe"])
        self.assertFalse(result["possibly_paid"])

    def test_exactly_one_graphql_call_is_made_and_it_is_the_query(self):
        """The heart of the correction.  Two calls would mean the mutation went
        out; one means the guard refused first."""
        self.responses = [self.fully_refunded_order()]
        r.write_back_refund(REFUND)

        self.assertEqual(len(self.calls), 1,
                         "the mutation was posted after all")
        self.assertEqual(self.calls[0]["operation"], "RefundTargets")
        self.assertNotIn("refundCreate", self.calls[0]["query"])

    def test_the_row_never_enters_the_possibly_paid_state(self):
        """Unverified is committed immediately before the post.  If the probe
        reached the mutation this would be set, and clearing it needs a person."""
        self.assertNotIn(
            r.STATUS_UNVERIFIED,
            [w[2].get(r.WRITEBACK_STATUS_FIELD)
             for w in frappe_stub.WRITES if isinstance(w[2], dict)],
        )
        self.responses = [self.fully_refunded_order()]
        r.write_back_refund(REFUND)

        statuses = [w[2].get(r.WRITEBACK_STATUS_FIELD)
                    for w in frappe_stub.WRITES if isinstance(w[2], dict)]
        self.assertNotIn(r.STATUS_UNVERIFIED, statuses)
        self.assertIn(r.STATUS_FAILED, statuses)

    def test_a_partial_refund_short_of_headroom_is_also_refused_unsent(self):
        """So the conclusion is not specific to a fully refunded order: asking
        for more than remains never reaches the mutation either.  Every route to
        refundCreate is a route Shopify accepts."""
        self.responses = [targets_response(transactions=[{
            "id": "gid://shopify/OrderTransaction/99",
            "kind": "SALE", "status": "SUCCESS", "gateway": "manual",
            "formattedGateway": "Manual",
            "amountSet": {"presentmentMoney": {"amount": "12999.00",
                                               "currencyCode": "INR"}},
            "maximumRefundableV2": {"amount": "5000.00", "currencyCode": "INR"},
            "parentTransaction": None,
        }])]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)
        self.assertEqual(result["reason_code"], "insufficient_refundable")
        self.assertEqual(len(self.calls), 1)

    def test_what_the_probe_does_still_exercise(self):
        """Not a nihilistic finding.  The probe is still worth running: it
        proves the credentials, the RefundTargets query, and this app's reading
        of a real order's transactions -- which is where the genuinely unknown
        shapes are."""
        self.responses = [self.fully_refunded_order()]
        r.write_back_refund(REFUND)

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["operation"], "RefundTargets")
        self.assertIn("maximumRefundableV2", self.calls[0]["query"])
        self.assertEqual(self.calls[0]["variables"]["orderId"], ORDER_GID)


if __name__ == "__main__":
    unittest.main()
