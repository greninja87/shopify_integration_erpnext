"""
test_refund_report.py — tests for reporting a Shopify-side refund to ERPNext.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_refund_report -v

This direction moves no money, so the tests are not about payouts.  They are
about the two ways a report can be worse than no report at all:

  * it says something the data does not support.  "manual" gateway means OCC
    (a real Cashfree refund followed) on one order and Snapmint/NEFT (no
    Gateway Transaction will ever exist) on the next, and nothing in a Shopify
    payload separates them.  So the tests pin that no settlement conclusion is
    ever emitted, only the raw gateway and an explicit "undetermined".
  * it is believed to have been delivered when it was not.  frappe.call drops
    kwargs the target does not declare, silently, so an observer built against
    an older field set looks exactly like one that honoured every field.  The
    tests pin that a report is only "reported" when the observer enumerated
    what it consumed, and that a dropped field lands as "unacknowledged".

The second is the same failure class as the dispatch contract's expected_amount
(REFUND-DISPATCH-CONTRACT.md section 1), pointing the other way across the seam.
"""

import re
import unittest
from pathlib import Path

from shopify_integration.tests import frappe_stub

frappe_stub.install()

import frappe  # noqa: E402

from shopify_integration.utils import refund_report as rr  # noqa: E402
from shopify_integration.utils.shopify_api import ShopifyAPIError  # noqa: E402

ORDER_ID = "7843650535529"
REFUND_ID = "929361464"
STORE = "notdrones.myshopify.com"


def webhook_payload(transactions=None, refund_line_items=None, **over):
    """A refunds/create REST payload, the shape the webhook actually receives.

    Note what is NOT here: any total.  A refund's amount is the sum of its
    refund transactions, which is why the extractor has to add them up rather
    than read a field.
    """
    if transactions is None:
        transactions = [{
            "id": 1068278508,
            "order_id": int(ORDER_ID),
            "kind": "refund",
            "gateway": "manual",
            "status": "success",
            "amount": "12999.00",
            "currency": "INR",
        }]
    if refund_line_items is None:
        refund_line_items = [{
            "id": 209341123,
            "quantity": 1,
            "line_item_id": 128323456,
            "restock_type": "no_restock",
            "subtotal": 11015.42,
            "total_tax": 1983.58,
            "line_item": {"id": 128323456, "title": "Agri Drone", "sku": "AGRI-1"},
        }]
    payload = {
        "id": int(REFUND_ID),
        "order_id": int(ORDER_ID),
        "created_at": "2026-09-01T10:15:00+05:30",
        "processed_at": "2026-09-01T10:15:00+05:30",
        "note": "Customer cancelled, refunded by NEFT",
        "user_id": 799407056,
        "restock": False,
        "transactions": transactions,
        "refund_line_items": refund_line_items,
        "order_adjustments": [],
    }
    payload.update(over)
    return payload


# ── Observers, deliberately of differing vintages ────────────────────────────

def good_observer(**facts):
    """An observer that declares **facts, so nothing can be dropped, and
    enumerates what it used."""
    good_observer.seen = facts
    return {
        rr.ACK_KEY: True,
        rr.CONSUMED_KEY: sorted(facts.keys()),
        "recorded_as": "REF-00301",
    }


def stale_observer(shopify_refund_id=None, shopify_order_id=None, amount=None,
                   **rest):
    """An observer of an older vintage: it never declared settlement_channel or
    gateways, so frappe.call drops both before it is even entered.  With **rest
    it still SEES them; the point of the test is the ones it does not claim."""
    stale_observer.seen = dict(rest,
                               shopify_refund_id=shopify_refund_id,
                               shopify_order_id=shopify_order_id,
                               amount=amount)
    return {rr.ACK_KEY: True, rr.CONSUMED_KEY: ["shopify_refund_id",
                                                "shopify_order_id", "amount"]}


def narrow_observer(shopify_refund_id=None, shopify_order_id=None, amount=None):
    """No **kwargs at all — the real silent drop.  frappe.call will not pass
    gateways or settlement_channel, and this function cannot know they existed."""
    narrow_observer.seen = {"shopify_refund_id": shopify_refund_id,
                            "shopify_order_id": shopify_order_id,
                            "amount": amount}
    return {rr.ACK_KEY: True,
            rr.CONSUMED_KEY: ["shopify_refund_id", "shopify_order_id", "amount"]}


def version_only_observer(**facts):
    """Echoes a version number and nothing else.  Section 1 of the dispatch
    contract says that is not enough, and this pins it."""
    return {rr.ACK_KEY: True, "report_contract_version": rr.REPORT_CONTRACT_VERSION}


def silent_observer(**facts):
    """Returns nothing, as a function that merely enqueues would."""
    return None


def refusing_observer(**facts):
    """Declines the report outright, which is a legitimate answer."""
    return {rr.ACK_KEY: False, "message": "no Sales Order for that Shopify order"}


def exploding_observer(**facts):
    raise RuntimeError("observer blew up")


class RefundReportTestCase(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()
        for fn in (good_observer, stale_observer, narrow_observer):
            if hasattr(fn, "seen"):
                del fn.seen


# ── Fact extraction ──────────────────────────────────────────────────────────

class TestFactsFromWebhook(RefundReportTestCase):

    def test_amount_is_summed_from_the_refund_transactions(self):
        """There is no total on a refund payload.  #6518's 12,999 is one
        transaction; a split refund is several, and must add up."""
        facts = rr.facts_from_webhook(webhook_payload(), shop_domain=STORE)
        self.assertEqual(facts["amount"], 12999.00)

        split = webhook_payload(transactions=[
            {"kind": "refund", "status": "success", "gateway": "manual",
             "amount": "10000.00", "currency": "INR", "id": 1},
            {"kind": "refund", "status": "success", "gateway": "manual",
             "amount": "2999.00", "currency": "INR", "id": 2},
        ])
        self.assertEqual(rr.facts_from_webhook(split)["amount"], 12999.00)

    def test_only_successful_refund_transactions_count_towards_the_amount(self):
        """A failed refund transaction moved no money.  Counting it would report
        a larger refund than happened."""
        mixed = webhook_payload(transactions=[
            {"kind": "refund", "status": "success", "gateway": "manual",
             "amount": "12999.00", "id": 1},
            {"kind": "refund", "status": "failure", "gateway": "manual",
             "amount": "500.00", "id": 2},
            {"kind": "sale", "status": "success", "gateway": "manual",
             "amount": "999.00", "id": 3},
        ])
        facts = rr.facts_from_webhook(mixed)
        self.assertEqual(facts["amount"], 12999.00)

    def test_gateways_are_copied_verbatim_and_deduplicated(self):
        facts = rr.facts_from_webhook(webhook_payload())
        self.assertEqual(facts["gateways"], ["manual"])

    def test_the_refund_and_order_ids_are_kept_apart(self):
        """The payload's own "id" is the REFUND id; "order_id" is the order.
        Conflating them is how a refund gets filed against the wrong order --
        api.py already carries a comment about this trap for the log row."""
        facts = rr.facts_from_webhook(webhook_payload())
        self.assertEqual(facts["shopify_refund_id"], REFUND_ID)
        self.assertEqual(facts["shopify_order_id"], ORDER_ID)
        self.assertNotEqual(facts["shopify_refund_id"], facts["shopify_order_id"])

    def test_the_refund_gid_is_built_from_the_refund_id(self):
        facts = rr.facts_from_webhook(webhook_payload())
        self.assertEqual(facts["shopify_refund_gid"],
                         f"gid://shopify/Refund/{REFUND_ID}")

    def test_note_reason_staff_user_and_timestamps_are_carried(self):
        facts = rr.facts_from_webhook(webhook_payload(), shop_domain=STORE)
        self.assertEqual(facts["note"], "Customer cancelled, refunded by NEFT")
        self.assertEqual(facts["shopify_user_id"], "799407056")
        self.assertEqual(facts["processed_at"], "2026-09-01T10:15:00+05:30")
        self.assertEqual(facts["shopify_store"], STORE)

    def test_line_items_are_reported_with_quantity_and_restock(self):
        facts = rr.facts_from_webhook(webhook_payload())
        self.assertEqual(len(facts["line_items"]), 1)
        line = facts["line_items"][0]
        self.assertEqual(line["sku"], "AGRI-1")
        self.assertEqual(line["quantity"], 1)
        self.assertEqual(line["restock_type"], "no_restock")
        self.assertEqual(line["line_item_id"], "128323456")

    def test_restocked_is_derived_from_the_line_items_not_guessed(self):
        self.assertFalse(rr.facts_from_webhook(webhook_payload())["restocked"])

        restocked = webhook_payload(refund_line_items=[
            {"id": 1, "quantity": 2, "line_item_id": 9, "restock_type": "return",
             "line_item": {"sku": "AGRI-1"}},
        ])
        self.assertTrue(rr.facts_from_webhook(restocked)["restocked"])

    def test_notify_is_unknown_rather_than_false(self):
        """Shopify's refund resource does not persist `notify` -- it is
        write-only on the input.  Reporting False would be inventing a fact,
        and a reader would take it as "the customer was not emailed"."""
        facts = rr.facts_from_webhook(webhook_payload())
        self.assertIsNone(facts["notify"])

    def test_a_refund_with_no_transactions_reports_zero_not_a_crash(self):
        """Shopify allows a refund that only restocks.  No money moved, and the
        report must still go out saying exactly that."""
        facts = rr.facts_from_webhook(webhook_payload(transactions=[]))
        self.assertEqual(facts["amount"], 0.0)
        self.assertEqual(facts["gateways"], [])
        self.assertEqual(facts["transaction_count"], 0)

    def test_every_fact_the_seam_requires_is_present(self):
        facts = rr.facts_from_webhook(webhook_payload(), shop_domain=STORE)
        for field in rr.MUST_CONSUME:
            self.assertIn(field, facts, f"{field} is required but not extracted")


# ── The conclusion this app must refuse to draw ──────────────────────────────

class TestNoSettlementConclusion(RefundReportTestCase):

    def test_settlement_channel_is_always_undetermined(self):
        """The whole point.  OCC and Snapmint both read "manual", so no Shopify
        payload can tell "a Cashfree refund followed" from "an NEFT went out and
        no Gateway Transaction will ever exist".  #6491's manual-gateway refund
        became Cashfree 144073385 in the same minute; #6518's went by NEFT.
        Same gateway string, opposite settlement."""
        for gateway in ("manual", "cashfree", "razorpay", "", "bogus"):
            facts = rr.facts_from_webhook(webhook_payload(transactions=[
                {"kind": "refund", "status": "success", "gateway": gateway,
                 "amount": "100.00", "id": 1},
            ]))
            self.assertEqual(facts["settlement_channel"],
                             rr.SETTLEMENT_UNDETERMINED,
                             f"a conclusion was drawn from gateway {gateway!r}")

    def test_the_raw_gateway_is_still_handed_over(self):
        """Refusing to conclude is not refusing to report.  payment_portals can
        decide from portal_account -> provider, which it can see and we cannot."""
        facts = rr.facts_from_webhook(webhook_payload())
        self.assertEqual(facts["gateways"], ["manual"])

    def test_no_fact_claims_money_did_or_did_not_move(self):
        """A belt-and-braces guard on the vocabulary: no key may carry a
        cashfree/settled/booked verdict, however tempting."""
        facts = rr.facts_from_webhook(webhook_payload())
        for key in facts:
            self.assertNotIn("cashfree", key.lower())
            self.assertNotIn("settled", key.lower())


# ── The seam ─────────────────────────────────────────────────────────────────

class TestReportRefund(RefundReportTestCase):

    def facts(self, **over):
        f = rr.facts_from_webhook(webhook_payload(), shop_domain=STORE)
        f.update(over)
        return f

    def test_no_observer_registered_is_reported_as_such_not_as_success(self):
        result = rr.report_refund(self.facts())
        self.assertEqual(result["outcome"], rr.OUTCOME_NO_OBSERVER)
        self.assertFalse(result["delivered"])

    def test_a_full_observer_gets_every_fact_and_the_report_is_delivered(self):
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_REPORTED)
        self.assertTrue(result["delivered"])
        self.assertEqual(result["recorded_as"], "REF-00301")
        self.assertEqual(good_observer.seen["shopify_refund_id"], REFUND_ID)
        self.assertEqual(good_observer.seen["settlement_channel"],
                         rr.SETTLEMENT_UNDETERMINED)

    def test_an_observer_that_cannot_receive_a_field_is_unacknowledged(self):
        """The trap, reproduced end to end.  narrow_observer declares three
        parameters, so frappe.call drops gateways and settlement_channel before
        the function is entered -- no TypeError, no warning.  It answers with a
        cheerful True, and that must NOT read as delivered."""
        frappe_stub.register_observer("app.obs.narrow", narrow_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_UNACKNOWLEDGED)
        self.assertFalse(result["delivered"])
        self.assertIn("gateways", result["unconsumed"])
        self.assertIn("settlement_channel", result["unconsumed"])

        # And the drop really happened, rather than the guard firing on a
        # technicality: the observer never saw the fields at all.
        _path, passed, received = frappe_stub.CALLS[-1]
        self.assertIn("gateways", passed)
        self.assertNotIn("gateways", received)

    def test_an_observer_that_sees_a_field_but_does_not_claim_it_is_unacknowledged(self):
        """stale_observer takes **rest, so nothing is dropped -- but it only
        enumerates three fields.  Silence about a field is not consumption."""
        frappe_stub.register_observer("app.obs.stale", stale_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_UNACKNOWLEDGED)
        self.assertIn("settlement_channel", result["unconsumed"])

    def test_a_version_number_is_not_an_acknowledgement(self):
        frappe_stub.register_observer("app.obs.version", version_only_observer)
        result = rr.report_refund(self.facts())
        self.assertEqual(result["outcome"], rr.OUTCOME_UNACKNOWLEDGED)

    def test_an_observer_returning_nothing_is_unacknowledged(self):
        frappe_stub.register_observer("app.obs.silent", silent_observer)
        result = rr.report_refund(self.facts())
        self.assertEqual(result["outcome"], rr.OUTCOME_UNACKNOWLEDGED)
        self.assertFalse(result["delivered"])

    def test_an_observer_may_refuse_and_that_is_not_a_failure(self):
        frappe_stub.register_observer("app.obs.refuse", refusing_observer)
        result = rr.report_refund(self.facts())
        self.assertEqual(result["outcome"], rr.OUTCOME_REFUSED)
        self.assertFalse(result["delivered"])
        self.assertIn("no Sales Order", result["message"])

    def test_an_observer_that_raises_does_not_take_the_webhook_down(self):
        """This runs inside a webhook that must return 200, and inside a refund
        that has already happened in Shopify.  Failing loudly here would make
        Shopify retry an event that is not the problem."""
        frappe_stub.register_observer("app.obs.boom", exploding_observer)
        result = rr.report_refund(self.facts())
        self.assertEqual(result["outcome"], rr.OUTCOME_FAILED)
        self.assertFalse(result["delivered"])
        self.assertTrue(frappe_stub.ERRORS, "the failure was not logged")

    def test_report_refund_never_raises_whatever_the_observer_does(self):
        for fn in (exploding_observer, silent_observer, refusing_observer,
                   narrow_observer, good_observer):
            frappe_stub.reset()
            frappe_stub.register_observer("app.obs.x", fn)
            try:
                rr.report_refund(self.facts())
            except Exception as exc:  # pragma: no cover
                self.fail(f"report_refund raised for {fn.__name__}: {exc}")

    def test_two_observers_are_a_misconfiguration_and_nothing_is_sent(self):
        """Unlike the payout dispatcher, a second reader is harmless in itself --
        but it means two apps may each record the refund, which is the two
        Payment Entries this whole design exists to avoid.  Refuse rather than
        pick."""
        frappe_stub.register_observer("app.obs.a", good_observer)
        frappe_stub.register_observer("app.obs.b", good_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_REFUSED)
        self.assertFalse(result["delivered"])
        self.assertIn("2", result["message"])
        self.assertEqual(frappe_stub.CALLS, [], "an observer was called anyway")

    def test_the_result_carries_the_report_contract_version_and_provider(self):
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund(self.facts())
        self.assertEqual(result["provider"], "shopify")
        self.assertEqual(result["report_contract_version"],
                         rr.REPORT_CONTRACT_VERSION)

    def test_a_report_never_writes_a_ledger_document(self):
        """The boundary this build was asked for.  payment_portals is the only
        app permitted to build a ledger document, and two recorders for one
        refund is two Payment Entries."""
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.report_refund(self.facts())

        for doc in frappe_stub.INSERTS:
            self.assertNotIn(doc.get("doctype"),
                             {"Payment Entry", "Journal Entry", "Sales Invoice"})
        for doctype, _name, _values, _kwargs in frappe_stub.WRITES:
            self.assertNotIn(doctype, {"Payment Entry", "Journal Entry"})

    def test_the_module_writes_nothing_to_refund_request(self):
        """Section 8 of the dispatch contract: this app touches only its own five
        write-back fields on Refund Request, and a report is not one of them."""
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.report_refund(self.facts())
        for doctype, _name, _values, _kwargs in frappe_stub.WRITES:
            self.assertNotEqual(doctype, "Refund Request")


class TestOutcomeVocabulary(RefundReportTestCase):

    def test_delivered_is_true_for_exactly_one_outcome(self):
        """The same discipline as retry_safe in the dispatch contract: one
        boolean a caller can read instead of remembering a set of strings."""
        self.assertEqual(
            {o for o in rr.OUTCOMES if rr.delivered(o)},
            {rr.OUTCOME_REPORTED},
        )

    def test_every_outcome_the_module_can_emit_is_in_the_vocabulary(self):
        observers = [None, good_observer, narrow_observer, silent_observer,
                     refusing_observer, exploding_observer]
        seen = set()
        for fn in observers:
            frappe_stub.reset()
            if fn:
                frappe_stub.register_observer("app.obs.x", fn)
            facts = rr.facts_from_webhook(webhook_payload(), shop_domain=STORE)
            seen.add(rr.report_refund(facts)["outcome"])
        self.assertTrue(seen <= set(rr.OUTCOMES), f"outside vocabulary: {seen}")


# ── The frappe-bound webhook path ────────────────────────────────────────────

class TestReportFromWebhook(RefundReportTestCase):

    def test_an_unacknowledged_report_reaches_the_error_log(self):
        """The refund happened and ERPNext was not told.  No retry fixes that on
        its own, so it has to reach a person."""
        frappe_stub.register_observer("app.obs.narrow", narrow_observer)
        result = rr.report_refund_from_webhook(webhook_payload(), shop_domain=STORE)

        self.assertEqual(result["outcome"], rr.OUTCOME_UNACKNOWLEDGED)
        self.assertTrue(frappe_stub.ERRORS)
        message, title = frappe_stub.ERRORS[-1]
        self.assertIn(REFUND_ID, title)
        self.assertIn("12999", message)
        self.assertIn("manual", message)

    def test_the_error_log_says_no_money_is_at_risk(self):
        """The other module's error text means "a customer may have been paid".
        This one must not read like that one, or it will be triaged as a payout
        incident at 2am."""
        frappe_stub.register_observer("app.obs.narrow", narrow_observer)
        rr.report_refund_from_webhook(webhook_payload(), shop_domain=STORE)
        message, _title = frappe_stub.ERRORS[-1]
        self.assertIn("No money is at risk", message)

    def test_no_observer_is_not_logged_as_an_error(self):
        """A site with no payment_portals is a normal configuration.  Logging it
        would fill the Error Log on every refund and train people to ignore it."""
        result = rr.report_refund_from_webhook(webhook_payload(), shop_domain=STORE)
        self.assertEqual(result["outcome"], rr.OUTCOME_NO_OBSERVER)
        self.assertEqual(frappe_stub.ERRORS, [])

    def test_a_delivered_report_is_not_logged_as_an_error(self):
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund_from_webhook(webhook_payload(), shop_domain=STORE)
        self.assertTrue(result["delivered"])
        self.assertEqual(frappe_stub.ERRORS, [])

    def test_a_malformed_payload_does_not_raise(self):
        """The webhook must return 200.  A payload this app cannot read is a log
        line, not a 500 that makes Shopify retry."""
        for bad in (None, {}, {"id": None}, {"transactions": "not a list"},
                    {"refund_line_items": [None, 7]}):
            frappe_stub.reset()
            try:
                result = rr.report_refund_from_webhook(bad, shop_domain=STORE)
            except Exception as exc:  # pragma: no cover
                self.fail(f"raised on {bad!r}: {exc}")
            self.assertIn(result["outcome"], rr.OUTCOMES)


class TestReportingIsNotGatedOnThePayoutToggle(RefundReportTestCase):
    """The toggle that must stay off guards the payout, not the report."""

    def test_the_store_lookup_ignores_enable_refund_writeback(self):
        """enable_refund_writeback is 0 on every store and must stay 0.  If the
        report were gated on it, the one thing that is safe to run would only
        run while the dangerous thing was armed."""
        frappe_stub.set_doc("Shopify Settings", "Test Store", {
            "name": "Test Store",
            "shop_domain": STORE,
            "enable_sync": 1,
            "enable_refund_writeback": 0,
        })
        self.assertIsNotNone(rr._settings_for_store(STORE))

    def test_a_store_with_sync_off_is_not_reported_for(self):
        frappe_stub.set_doc("Shopify Settings", "Off Store", {
            "name": "Off Store",
            "shop_domain": "off.myshopify.com",
            "enable_sync": 0,
            "enable_refund_writeback": 0,
        })
        self.assertIsNone(rr._settings_for_store("off.myshopify.com"))


# ── The backfill ─────────────────────────────────────────────────────────────

def refunds_response(refunds=None, order=True, shape="list"):
    """An OrderRefunds response.  `order.refunds` in each of the three shapes
    the field has been documented as."""
    if not order:
        return {"order": None}
    if refunds is None:
        refunds = [{
            "id": "gid://shopify/Refund/929361464",
            "note": "Refunded by NEFT",
            "createdAt": "2026-09-01T10:15:00Z",
            "totalRefundedSet": {"presentmentMoney":
                                 {"amount": "12999.00", "currencyCode": "INR"}},
            "transactions": {"edges": [{"node": {
                "id": "gid://shopify/OrderTransaction/1",
                "kind": "REFUND", "status": "SUCCESS", "gateway": "manual",
                "amountSet": {"presentmentMoney":
                              {"amount": "12999.00", "currencyCode": "INR"}},
            }}]},
        }]
    if shape == "list":
        container = refunds
    elif shape == "nodes":
        container = {"nodes": refunds}
    else:
        container = {"edges": [{"node": r} for r in refunds]}
    return {"order": {"id": "gid://shopify/Order/" + ORDER_ID,
                      "name": "#6518", "refunds": container}}


class TestBackfill(RefundReportTestCase):

    def setUp(self):
        super().setUp()
        self._real_execute = rr.execute
        frappe_stub.set_doc("Shopify Settings", "Test Store", {
            "name": "Test Store",
            "shop_domain": STORE,
            "enable_sync": 1,
            "enable_refund_writeback": 0,
        })
        self.calls = []
        self.responses = [refunds_response()]

        def fake_execute(settings, query, variables=None, operation=""):
            self.calls.append({"query": query, "variables": variables or {},
                               "operation": operation})
            if not self.responses:
                raise AssertionError(f"unexpected GraphQL call: {operation}")
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        rr.execute = fake_execute

    def tearDown(self):
        rr.execute = self._real_execute

    def test_the_backfill_posts_no_mutation(self):
        """The whole safety case for this path in one assertion.  There is no
        refundCreate here, so unlike the write-back it cannot pay anybody."""
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertEqual(len(self.calls), 1)
        query = self.calls[0]["query"]
        self.assertNotIn("mutation", query.lower())
        self.assertNotIn("refundCreate", query)

    def test_each_refund_on_the_order_is_reported(self):
        frappe_stub.register_observer("app.obs.good", good_observer)
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertEqual(out["read"], 1)
        self.assertEqual(out["reported"], 1)
        self.assertEqual(out["shopify_order_name"], "#6518")
        self.assertEqual(good_observer.seen["amount"], 12999.00)
        self.assertEqual(good_observer.seen["source"], "backfill")

    def test_all_three_refunds_container_shapes_are_read(self):
        """Order.refunds is documented as a list and appears elsewhere as a
        connection.  Guessing wrong reads as "this order has no refunds" and
        reports nothing at all -- silently."""
        for shape in ("list", "nodes", "edges"):
            frappe_stub.reset()
            frappe_stub.set_doc("Shopify Settings", "Test Store", {
                "name": "Test Store", "shop_domain": STORE,
                "enable_sync": 1, "enable_refund_writeback": 0})
            frappe_stub.register_observer("app.obs.good", good_observer)
            self.responses = [refunds_response(shape=shape)]
            out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)
            self.assertEqual(out["read"], 1, f"shape {shape} was not read")

    def test_the_gid_from_shopify_is_kept_not_rebuilt(self):
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)
        self.assertEqual(good_observer.seen["shopify_refund_gid"],
                         "gid://shopify/Refund/929361464")
        self.assertEqual(good_observer.seen["shopify_refund_id"], "929361464")

    def test_the_total_is_used_when_the_transactions_come_back_empty(self):
        """A zero would read as "no money moved", which for a refund is the one
        wrong answer that looks like a fact."""
        frappe_stub.register_observer("app.obs.good", good_observer)
        self.responses = [refunds_response(refunds=[{
            "id": "gid://shopify/Refund/7",
            "note": "",
            "createdAt": "2026-09-01T10:15:00Z",
            "totalRefundedSet": {"presentmentMoney":
                                 {"amount": "46952.16", "currencyCode": "INR"}},
            "transactions": {"edges": []},
        }])]
        rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)
        self.assertEqual(good_observer.seen["amount"], 46952.16)

    def test_the_backfill_draws_no_settlement_conclusion_either(self):
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)
        self.assertEqual(good_observer.seen["settlement_channel"],
                         rr.SETTLEMENT_UNDETERMINED)
        self.assertEqual(good_observer.seen["gateways"], ["manual"])

    def test_an_order_shopify_cannot_show_is_reported_not_raised(self):
        self.responses = [refunds_response(order=False)]
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)
        self.assertEqual(out["read"], 0)
        self.assertIn("not found", out["message"])

    def test_a_read_failure_is_logged_and_returned(self):
        self.responses = [ShopifyAPIError("token rejected")]
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)
        self.assertEqual(out["read"], 0)
        self.assertTrue(frappe_stub.ERRORS)

    def test_an_unconfigured_store_reads_nothing(self):
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain="nope.myshopify.com")
        self.assertEqual(out["read"], 0)
        self.assertEqual(self.calls, [], "Shopify was called for an unknown store")

    def test_no_order_id_reads_nothing(self):
        out = rr.backfill_order_refunds("", shop_domain=STORE)
        self.assertEqual(out["read"], 0)
        self.assertEqual(self.calls, [])


class TestWhatIsReachableOverHttp(RefundReportTestCase):
    """Which functions are whitelisted is a real property of this seam, not a
    detail: the backfill hands facts to another app that may record a refund
    from them."""

    def test_backfill_now_is_the_only_http_door(self):
        self.assertTrue(getattr(rr.backfill_now, "__is_whitelisted__", False))

    def test_the_working_functions_are_not_whitelisted(self):
        for fn in (rr.report_refund, rr.report_refund_from_webhook,
                   rr.backfill_order_refunds, rr.facts_from_webhook,
                   rr.facts_from_graphql):
            self.assertFalse(getattr(fn, "__is_whitelisted__", False),
                             f"{fn.__name__} is reachable over HTTP")


class TestTheContractDocumentMatchesTheCode(RefundReportTestCase):
    """REFUND-REPORT-CONTRACT.md is what the payment_portals side builds an
    observer against.  A document that has drifted from the code is worse than
    no document, because it is believed -- the same reasoning as
    test_refund_contract.py for the dispatch direction.
    """

    @classmethod
    def setUpClass(cls):
        cls.path = (Path(__file__).resolve().parents[2]
                    / "REFUND-REPORT-CONTRACT.md")
        cls.text = cls.path.read_text(encoding="utf-8")

    def test_the_document_exists_where_the_code_points_at_it(self):
        self.assertTrue(self.path.is_file(), f"{self.path} is missing")

    def test_every_report_contract_version_literal_matches_the_code(self):
        """The dispatch contract shipped `contract_version: 1` through two bumps
        because only its header was updated.  Same guard, same reason."""
        literals = re.findall(r"report_contract_version\"?\s*:\s*(\d+)", self.text)
        self.assertTrue(literals, "no report_contract_version literal to check")
        for found in literals:
            self.assertEqual(
                int(found), rr.REPORT_CONTRACT_VERSION,
                f"the document shows report_contract_version {found}, the code "
                f"says {rr.REPORT_CONTRACT_VERSION}",
            )

    def test_the_header_version_matches_the_code(self):
        header = re.search(r"^\*\*Version (\d+)\.\*\*", self.text, re.M)
        self.assertIsNotNone(header, "no **Version N.** header found")
        self.assertEqual(int(header.group(1)), rr.REPORT_CONTRACT_VERSION)

    def test_every_outcome_the_code_can_emit_is_documented(self):
        for outcome in rr.OUTCOMES:
            self.assertIn(f"`{outcome}`", self.text,
                          f"outcome {outcome} is not in the contract")

    def test_the_document_invents_no_outcome_the_code_cannot_emit(self):
        """The other direction, which is the one that misleads a caller into
        branching on a string nothing ever returns."""
        documented = set(re.findall(r'"outcome":\s*(.+)', self.text))
        for line in documented:
            for slug in re.findall(r'"([a-z_]+)"', line):
                self.assertIn(slug, rr.OUTCOMES,
                              f"the document lists an outcome {slug!r} the code "
                              f"never emits")

    def test_every_must_consume_field_is_documented(self):
        """An observer author reads this table to know what to enumerate.  A
        field missing from it is a field they will not claim, which makes every
        report unacknowledged."""
        for field in rr.MUST_CONSUME:
            self.assertIn(f"`{field}`", self.text,
                          f"{field} is in MUST_CONSUME but not in the contract")

    def test_the_acknowledgement_keys_are_documented_verbatim(self):
        self.assertIn(rr.ACK_KEY, self.text)
        self.assertIn(f'"{rr.CONSUMED_KEY}"', self.text)

    def test_the_hook_name_is_documented_verbatim(self):
        self.assertIn(rr.OBSERVER_HOOK, self.text)

    def test_the_undetermined_settlement_value_is_documented_verbatim(self):
        self.assertIn(f'"{rr.SETTLEMENT_UNDETERMINED}"', self.text)

    def test_delivered_is_documented_as_true_for_one_outcome(self):
        self.assertIn("true for exactly one outcome", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
