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
  * HTTP 200 with empty userErrors and no refund object → Failed, never Done
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
            "status": "Completed",
            "refund_channel": "Payment Portal",
            "sales_order": "SO-0001",
            "net_refund_amount": 12999.0,
            "reason_note": "Damaged in transit",
            r.REFUND_GID_FIELD: "",
            r.WRITEBACK_STATUS_FIELD: "",
        })

        # Capture GraphQL calls and serve canned responses in order.
        self.calls = []
        self.responses = [targets_response(), refund_created()]

        def fake_execute(settings, query, variables=None, operation=""):
            self.calls.append({"query": query, "variables": variables or {},
                               "operation": operation})
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

    def test_http_200_with_no_user_errors_and_no_refund_is_failed_not_done(self):
        """The quiet failure this codebase already guards elsewhere: the request
        was accepted, nothing happened, and nothing said so."""
        self.responses = [targets_response(), refund_created(refund=False)]
        result = r.write_back_refund(REFUND)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], r.STATUS_FAILED)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_FAILED)
        self.assertNoGid()

    def test_a_refund_object_with_no_id_is_also_failed(self):
        self.responses = [targets_response(), {"refundCreate": {
            "refund": {"id": "", "transactions": {"edges": []}}, "userErrors": [],
        }}]
        result = r.write_back_refund(REFUND)
        self.assertEqual(result["status"], r.STATUS_FAILED)
        self.assertNoGid()

    def test_a_missing_mutation_key_is_failed(self):
        self.responses = [targets_response(), {}]
        result = r.write_back_refund(REFUND)
        self.assertEqual(result["status"], r.STATUS_FAILED)
        self.assertNoGid()

    def test_a_transport_error_is_failed_and_recorded(self):
        self.responses = [targets_response(), ShopifyAPIError("connection reset")]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_FAILED)
        self.assertIn("connection reset", self.stored(r.WRITEBACK_ERROR_FIELD))
        self.assertNoGid()

    def test_a_failed_query_never_reaches_the_mutation(self):
        self.responses = [ShopifyAPIError("order query failed")]
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["status"], r.STATUS_FAILED)
        self.assertNothingSent()

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

    def test_a_refund_that_is_not_completed_is_skipped(self):
        for status in ("Draft", "Approved", "Queued", "Processing", "Failed", "Cancelled"):
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

    def test_an_unmigrated_site_says_migrate_and_not_no_permission(self):
        """On a site with no payment_portals, frappe.has_permission on the
        missing DocType fails too — so the availability check has to come first
        or the diagnosis is wrong."""
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set()
        real = frappe.has_permission
        frappe.has_permission = lambda *a, **k: (_ for _ in ()).throw(
            Exception("DocType Refund Request not found")
        )
        try:
            result = r.write_back_refund(REFUND)
        finally:
            frappe.has_permission = real

        self.assertIn("migrate", result["message"].lower())
        self.assertNotIn("permission", result["message"].lower())

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

    def test_write_back_refund_refuses_a_caller_without_submit_permission(self):
        """It is whitelisted and it pays a customer, so it cannot rely on the
        caller having checked."""
        real = frappe.has_permission
        frappe.has_permission = lambda *a, **k: False
        try:
            result = r.write_back_refund(REFUND)
        finally:
            frappe.has_permission = real

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], r.STATUS_SKIPPED)
        self.assertIn("permission", result["message"].lower())
        self.assertNothingSent()

    def test_a_refused_caller_leaves_no_mark_on_the_document(self):
        real = frappe.has_permission
        frappe.has_permission = lambda *a, **k: False
        try:
            r.write_back_refund(REFUND)
        finally:
            frappe.has_permission = real

        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), "")
        self.assertEqual(frappe_stub.WRITES, [])

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

    def test_the_status_endpoint_survives_an_unmigrated_site(self):
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set()
        info = r.get_refund_writeback_status(REFUND)
        self.assertFalse(info["is_shopify"])
        self.assertFalse(info["migrated"])


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


if __name__ == "__main__":
    unittest.main()
