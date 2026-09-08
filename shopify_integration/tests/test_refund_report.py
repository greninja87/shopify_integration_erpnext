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

  * it is delivered for a refund THIS app raised.  refund.py's own
    refundCreate makes Shopify send refunds/create back to us, and reporting
    that hands the recorder a refund it raised itself and is already booking --
    the second recorder for one refund, which is two Payment Entries.  The
    tests pin that the guard sits in report_refund(), the one seam both the
    webhook and the backfill cross, so neither path can be guarded while the
    other is forgotten.

The second is the same failure class as the dispatch contract's expected_amount
(REFUND-DISPATCH-CONTRACT.md section 1), pointing the other way across the seam.

What these tests deliberately do NOT pin is that a refund is reported at most
once.  Delivery is at-least-once by design -- see section 5a of
REFUND-REPORT-CONTRACT.md -- so the tests pin the opposite: a duplicate is
reported again, visibly, and the observer dedupes.
"""

import json
import re
import types
import unittest
from pathlib import Path

from shopify_integration.tests import frappe_stub

frappe_stub.install()

import frappe  # noqa: E402

from shopify_integration.tests.test_refund_writeback import (  # noqa: E402
    install_sales_order_lookup,
)
from shopify_integration.utils import refund as writeback  # noqa: E402
from shopify_integration.utils import refund_report as rr  # noqa: E402
from shopify_integration.utils.shopify_api import ShopifyAPIError  # noqa: E402

# The outcome slug round 2 introduced and round 3 withdrew.  Spelled in two
# halves deliberately: one of the tests below asserts the slug appears in no
# source file, and this file is one of the files it reads.
WITHDRAWN_OUTCOME = "own_writeback" + "_unverified"

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


def must_consume_observer(shopify_refund_id=None, shopify_order_id=None,
                          amount=None, gateways=None, settlement_channel=None):
    """An observer that declares exactly the MUST_CONSUME fields and nothing
    else.

    This is the frappe.call drop path for an OPTIONAL fact, and the reason
    `unconfirmed_writeback_on_order` is deliberately not in MUST_CONSUME: this
    observer never receives it, cannot know it existed, and must still
    acknowledge and still count as delivered — behaving exactly as it does
    today, which is to deliver.  A mandatory fact would instead make every
    observer of this vintage unacknowledged, which is a version bump.
    """
    must_consume_observer.seen = {
        "shopify_refund_id": shopify_refund_id,
        "shopify_order_id": shopify_order_id,
        "amount": amount,
        "gateways": gateways,
        "settlement_channel": settlement_channel,
    }
    return {rr.ACK_KEY: True, rr.CONSUMED_KEY: sorted(rr.MUST_CONSUME)}


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


# -- A refund this app raised itself -----------------------------------------

def seed_own_writeback(refund_gid, refund_name="REF-00207"):
    """A Refund Request row this app wrote back, carrying that Shopify refund.

    The state on disk once `refund.py` has READ a successful refundCreate
    response: the row names the Shopify refund, and the GID and the Done status
    were committed together — deliberately, so the refunds/create webhook our
    own write fires can be recognised as ours by the time it arrives.

    Note what this is NOT: the state *before* the post.  There the GID field is
    still empty and only the Unverified marker exists — see
    `seed_unverified_writeback`, and the window it opens.  The round-1 fixture
    for this file claimed the GID was committed before the post, which is why
    the suite could not catch that window.

    Which id form is stored is the caller's choice here, because a payload may
    hand over either.
    """
    frappe_stub.META_FIELDS[writeback.REFUND_REQUEST] = {
        writeback.REFUND_GID_FIELD,
        writeback.WRITEBACK_STATUS_FIELD,
    }
    frappe_stub.set_doc(writeback.REFUND_REQUEST, refund_name, {
        "name": refund_name,
        writeback.REFUND_GID_FIELD: refund_gid,
        writeback.WRITEBACK_STATUS_FIELD: writeback.STATUS_DONE,
    })
    return refund_name


def seed_unverified_writeback(refund_name="REF-00207", sales_order="SO-0001",
                              shopify_order_id=ORDER_ID):
    """A Refund Request of ours mid-flight: Unverified, and NO GID.

    This is the state `refund.py` commits *before* it posts refundCreate, and
    the state every `failed_unknown` outcome leaves behind — a worker killed
    after the post, a socket timeout, a 5xx, a 200 that answered with no refund
    object.  The row is ours and Shopify may genuinely hold the refund, and
    there is no GID for the exact lookup to match, so the only handle on it is
    the order.

    `docstatus: 1` is not decoration.  `unverified_writebacks_for_order`
    filters to submitted rows, because cancelling the stuck request is what an
    operator reaches for when a row will not clear, and a cancelled Refund
    Request is no longer ERPNext's record of anything.  A fixture without it
    seeds a row production would never see, so every assertion built on it
    passes for the wrong reason — which is what this fixture did until round 3.
    """
    frappe_stub.META_FIELDS[writeback.REFUND_REQUEST] = {
        writeback.REFUND_GID_FIELD,
        writeback.WRITEBACK_STATUS_FIELD,
    }
    frappe_stub.set_doc("Sales Order", sales_order, {
        "shopify_order_id": shopify_order_id,
        "shopify_store": STORE,
    })
    frappe_stub.set_doc(writeback.REFUND_REQUEST, refund_name, {
        "name": refund_name,
        "sales_order": sales_order,
        "docstatus": 1,
        writeback.REFUND_GID_FIELD: "",
        writeback.WRITEBACK_STATUS_FIELD: writeback.STATUS_UNVERIFIED,
    })
    return refund_name


def capture_logger(testcase):
    """Route frappe.logger().info into a list, restored on cleanup.

    The delivery log line is not decoration here: delivery is at-least-once, so
    what makes a repeat delivery liveable is that a person can see it happened
    afterwards.  A test that never read the line would let it be deleted.
    """
    lines = []
    real = frappe.logger
    frappe.logger = lambda *a, **k: types.SimpleNamespace(
        info=lambda message, *ar, **kw: lines.append(str(message)),
        warning=lambda *ar, **kw: None,
        error=lambda *ar, **kw: None,
        debug=lambda *ar, **kw: None,
    )
    testcase.addCleanup(lambda: setattr(frappe, "logger", real))
    return lines


class RefundReportTestCase(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()
        for fn in (good_observer, stale_observer, narrow_observer,
                   must_consume_observer):
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
        write-back fields on Refund Request, and a report is not one of them.

        Asserted on the guard paths too, not only the delivering one: both read
        that document, and the unconfirmed-write-back one is about a row a
        person has to clear — clearing it from here would decide, without
        evidence, that a customer had or had not been paid."""
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.report_refund(self.facts())

        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311")
        rr.report_refund_from_webhook(webhook_payload(), shop_domain=STORE)

        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}", "REF-00312")
        rr.report_refund_from_webhook(webhook_payload(), shop_domain=STORE)

        for doctype, _name, _values, _kwargs in frappe_stub.WRITES:
            self.assertNotEqual(doctype, "Refund Request")
        self.assertEqual(
            frappe_stub.get_doc_values(writeback.REFUND_REQUEST,
                                       "REF-00311").get(
                writeback.WRITEBACK_STATUS_FIELD),
            writeback.STATUS_UNVERIFIED,
            "reporting resolved the row it was only supposed to report",
        )


class TestOurOwnWriteBackIsNotReported(RefundReportTestCase):
    """The loop `refund.py` closes on itself.

    A successful refundCreate PAYS the customer, and Shopify then sends us
    refunds/create for our own write -- which is why refund.py commits the GID
    before the post.  The credit-note path already consults
    refund_request_for_shopify_refund for exactly this reason
    (utils/credit_note.py), but the report path crossed the same webhook
    without the same check and handed the observer a refund ERPNext raised and
    payment_portals is already booking.  That is the second recorder for one
    refund, which this module's own docstring calls two Payment Entries.

    The guard lives in report_refund(), not in api.py: it is the single seam
    both the webhook and the backfill cross, so it cannot be fixed on one path
    and forgotten on the other.
    """

    def facts(self, **over):
        f = rr.facts_from_webhook(webhook_payload(), shop_domain=STORE)
        f.update(over)
        return f

    def test_a_refund_this_app_wrote_back_is_not_reported(self):
        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}")
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_OWN_WRITEBACK)
        self.assertFalse(result["delivered"])

    def test_the_observer_is_never_called_for_our_own_refund(self):
        """Not "called and ignored" -- never called.  The observer raised this
        refund; telling it about it is the duplicate."""
        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}")
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.report_refund(self.facts())

        self.assertEqual(frappe_stub.CALLS, [], "the observer was called anyway")
        self.assertFalse(hasattr(good_observer, "seen"))

    def test_the_bare_numeric_id_form_is_recognised(self):
        """A REST payload gives the bare id, and refund.py may have stored
        either form.  refund_request_for_shopify_refund tries both, so the
        guard hands it the id rather than pre-deciding a form."""
        seed_own_writeback(REFUND_ID)
        frappe_stub.register_observer("app.obs.good", good_observer)
        self.assertEqual(rr.report_refund(self.facts())["outcome"],
                         rr.OUTCOME_OWN_WRITEBACK)

    def test_the_gid_form_is_recognised_too(self):
        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}")
        frappe_stub.register_observer("app.obs.good", good_observer)
        self.assertEqual(rr.report_refund(self.facts())["outcome"],
                         rr.OUTCOME_OWN_WRITEBACK)

    def test_the_refund_request_is_named_so_the_line_can_be_followed_up(self):
        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}", "REF-00311")
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund(self.facts())
        self.assertIn("REF-00311", result["message"])

    def test_a_deliberate_skip_is_not_written_to_the_error_log(self):
        """Same reasoning as no_observer: this is a correct outcome, not a
        fault.  Logging it would put a line in the Error Log for every refund
        this app raises, and train people to ignore the ones that matter."""
        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}")
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund_from_webhook(webhook_payload(),
                                               shop_domain=STORE)

        self.assertEqual(result["outcome"], rr.OUTCOME_OWN_WRITEBACK)
        self.assertEqual(frappe_stub.ERRORS, [])

    def test_the_skip_is_visible_in_the_log_naming_the_refund_request(self):
        lines = capture_logger(self)
        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}", "REF-00311")
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.report_refund_from_webhook(webhook_payload(), shop_domain=STORE)

        self.assertTrue(any(REFUND_ID in line and "REF-00311" in line
                            for line in lines), lines)

    def test_an_external_refund_is_still_reported(self):
        """The no-regression case, and the one that matters more than the
        guard: a refund somebody made in Shopify is what this whole module
        exists for.  A guard that suppressed it would be the permanent
        omission -- #6518's NEFT recorded by nothing at all."""
        seed_own_writeback("gid://shopify/Refund/111111111", "REF-00099")
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_REPORTED)
        self.assertTrue(result["delivered"])
        self.assertEqual(good_observer.seen["shopify_refund_id"], REFUND_ID)

    def test_reporting_proceeds_where_the_writeback_fields_do_not_exist(self):
        """On a site with no payment_portals, or one where `bench migrate` has
        not run the patch, refund_request_for_shopify_refund returns None
        because the fields are absent.  That must read as "not ours", never as
        "cannot tell, so stay quiet"."""
        frappe_stub.set_doc(writeback.REFUND_REQUEST, "REF-00207", {
            "name": "REF-00207",
            writeback.REFUND_GID_FIELD: f"gid://shopify/Refund/{REFUND_ID}",
        })
        frappe_stub.META_FIELDS[writeback.REFUND_REQUEST] = set()
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_REPORTED)
        self.assertTrue(result["delivered"])

    def test_a_blank_refund_id_does_not_match_a_blank_field(self):
        """A refund whose id we could not read must not be claimed as ours by a
        Refund Request whose gid is still empty -- the state every row is in
        before its write-back runs."""
        frappe_stub.META_FIELDS[writeback.REFUND_REQUEST] = {
            writeback.REFUND_GID_FIELD, writeback.WRITEBACK_STATUS_FIELD}
        frappe_stub.set_doc(writeback.REFUND_REQUEST, "REF-00500", {
            "name": "REF-00500", writeback.REFUND_GID_FIELD: "",
            writeback.WRITEBACK_STATUS_FIELD: ""})
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund(self.facts(shopify_refund_id=""))
        self.assertNotEqual(result["outcome"], rr.OUTCOME_OWN_WRITEBACK)


class TestAnUnconfirmedWriteBackIsReportedNotWithheld(RefundReportTestCase):
    """The window the GID guard cannot see, and the direction this module takes
    in it.

    `refund.py` writes `shopify_refund_gid` only after it has READ a successful
    refundCreate response.  What it commits *before* the post is the Unverified
    marker, with that field still empty.  So every `failed_unknown` outcome —
    worker killed after the post, a timeout, a 5xx, "Shopify accepted the
    request but returned no refund object" — leaves a row that IS ours, with no
    GID, while Shopify may genuinely hold the refund.

    Round 2 made that suspicion WITHHOLD the report.  Reversed here, because it
    contradicted this module's own doctrine.  A genuinely external refund on an
    order that happens to carry an unresolved row was withheld indefinitely,
    with one Error Log line and nothing that re-drives it — a permanent
    omission, which the docstring and `_surface`'s own text call the failure
    nothing can recover — while the harm it avoided was a duplicate the
    observer is contractually obliged to dedupe anyway.

    So the report is DELIVERED, and the observer is handed the fact it needs to
    reconcile instead of duplicating.  The credit-note path chooses the
    opposite direction on the same evidence, deliberately: see
    TestTheCreditNotePathWithholdsInstead for why the two disagree.
    """

    def facts(self, **over):
        f = rr.facts_from_webhook(webhook_payload(), shop_domain=STORE)
        f.update(over)
        return f

    def test_an_external_refund_beside_an_unresolved_row_is_still_reported(self):
        """The reversal, in one assertion.  This refund may be ours and may be
        somebody else's; delivering it risks a duplicate the observer dedupes,
        and withholding it risks the omission nothing recovers."""
        install_sales_order_lookup(self)
        seed_unverified_writeback()
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_REPORTED)
        self.assertTrue(result["delivered"])

    def test_the_facts_name_the_unconfirmed_row_so_the_observer_can_reconcile(self):
        """What replaces the withholding: the observer is told the one thing it
        cannot see, and can reconcile against its own Refund Request rather
        than book a second Payment Entry."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311")
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.report_refund(self.facts())

        self.assertEqual(good_observer.seen["unconfirmed_writeback_on_order"],
                         ["REF-00311"])

    def test_every_unconfirmed_row_on_the_order_is_named_not_just_one(self):
        """An order can carry two: a first attempt that lost its answer, then a
        second refund raised for the rest of the order that lost its answer
        too.  Naming one of two says the order is clear once that one is
        cleared."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311")
        seed_unverified_writeback(refund_name="REF-00312")
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.report_refund(self.facts())

        self.assertEqual(good_observer.seen["unconfirmed_writeback_on_order"],
                         ["REF-00311", "REF-00312"])

    def test_the_delivery_is_logged_because_a_person_may_have_to_reconcile(self):
        """Delivering is right and still not silent: this is the one delivery
        that may need reconciling by hand, and the log line has to say which
        row to resolve and how."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311")
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund_from_webhook(webhook_payload(),
                                               shop_domain=STORE)

        self.assertTrue(result["delivered"])
        self.assertTrue(frappe_stub.ERRORS,
                        "a delivery that may need reconciling was silent")
        message, title = frappe_stub.ERRORS[-1]
        self.assertIn(REFUND_ID, title)
        self.assertIn("REF-00311", message)
        self.assertIn("resolve_unverified_writeback", message)
        # It must not read like the other module's payout alarm, or it is
        # triaged as a double refund at 2am.
        self.assertIn("No money is at risk", message)

    def test_the_log_line_says_the_report_went_out(self):
        """A reader who takes this for a withheld report goes looking for the
        refund in payment_portals and does not find it, or re-drives a report
        that already landed."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311")
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.report_refund_from_webhook(webhook_payload(), shop_domain=STORE)

        message, _title = frappe_stub.ERRORS[-1]
        self.assertIn("was reported", message)
        self.assertNotIn("not reported", message.lower())
        self.assertNotIn("was withheld", message.lower())

    def test_no_version_of_the_conditional_recovery_sentence_survives(self):
        """Round 2's instruction read "if the refund turns out NOT to be ours,
        run backfill_now to report it" — which was "not applicable" precisely
        when the second refund needed reporting, and is meaningless now that
        the report goes out on its own."""
        import inspect

        sources = [inspect.getsource(rr)]
        contract = (Path(__file__).resolve().parents[2]
                    / "REFUND-REPORT-CONTRACT.md").read_text(encoding="utf-8")
        sources.append(contract)
        for source in sources:
            self.assertNotRegex(
                " ".join(source.split()),
                r"(?i)turns? out (?:\*\*)?not(?:\*\*)? to be ours",
            )

    def test_resolving_the_row_empties_the_fact_and_silences_the_log(self):
        """`resolve_unverified_writeback(..., "not_paid")` says no refund of
        ours exists, so there is nothing left to reconcile against."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311")
        writeback.resolve_unverified_writeback("REF-00311", "not_paid")
        frappe_stub.ERRORS.clear()
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund_from_webhook(webhook_payload(),
                                               shop_domain=STORE)

        self.assertEqual(result["outcome"], rr.OUTCOME_REPORTED)
        self.assertEqual(good_observer.seen["unconfirmed_writeback_on_order"],
                         [])
        self.assertEqual(frappe_stub.ERRORS, [])

    def test_resolving_the_row_to_a_gid_makes_it_the_certain_skip(self):
        """`resolve_unverified_writeback(..., "paid")` writes the GID, and from
        then on this is the ordinary own_writeback case: ERPNext has the refund
        and there is nothing for anyone to do."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311")
        writeback.resolve_unverified_writeback(
            "REF-00311", "paid", shopify_refund_gid=REFUND_ID)
        frappe_stub.ERRORS.clear()
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund_from_webhook(webhook_payload(),
                                               shop_domain=STORE)

        self.assertEqual(result["outcome"], rr.OUTCOME_OWN_WRITEBACK)
        self.assertEqual(frappe_stub.ERRORS, [])
        self.assertEqual(frappe_stub.CALLS, [])

    def test_a_gid_matched_own_refund_is_still_skipped_and_still_silent(self):
        """The one certain case, untouched.  The GID means refund.py read
        Shopify's answer for this very refund, so it is already recorded and
        there is nobody to tell — and an unresolved row elsewhere on the same
        order must not turn that certainty into a suspicion."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00400")
        frappe_stub.set_doc(writeback.REFUND_REQUEST, "REF-00311", {
            "name": "REF-00311", "sales_order": "SO-0001",
            writeback.REFUND_GID_FIELD: f"gid://shopify/Refund/{REFUND_ID}",
            writeback.WRITEBACK_STATUS_FIELD: writeback.STATUS_DONE})
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund_from_webhook(webhook_payload(),
                                               shop_domain=STORE)

        self.assertEqual(result["outcome"], rr.OUTCOME_OWN_WRITEBACK)
        self.assertIn("REF-00311", result["message"])
        self.assertEqual(frappe_stub.ERRORS, [])
        self.assertEqual(frappe_stub.CALLS, [])
        self.assertFalse(hasattr(good_observer, "seen"))

    def test_an_unresolved_row_on_another_order_leaves_the_fact_empty(self):
        """The lookup is order-scoped, and the fact has to be too: an
        unconfirmed write-back on one order is nothing for the observer to
        reconcile a refund on a different one against."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311",
                                  sales_order="SO-0002",
                                  shopify_order_id="9999999999999")
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_REPORTED)
        self.assertEqual(good_observer.seen["unconfirmed_writeback_on_order"],
                         [])

    def test_an_observer_that_does_not_declare_the_fact_still_delivers(self):
        """The frappe.call drop path, pinned.  An observer declaring only the
        MUST_CONSUME fields never receives the new one — no TypeError, no
        warning — and must behave exactly as it does today, which is to
        deliver.  That is what makes an OPTIONAL fact safe to add without a
        version bump, and a mandatory one not."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311")
        frappe_stub.register_observer("app.obs.mc", must_consume_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_REPORTED)
        self.assertTrue(result["delivered"])
        _path, passed, received = frappe_stub.CALLS[-1]
        self.assertIn("unconfirmed_writeback_on_order", passed)
        self.assertNotIn("unconfirmed_writeback_on_order", received)

    def test_the_fact_is_not_in_must_consume(self):
        """Adding a fact to MUST_CONSUME makes every existing observer
        unacknowledged until it claims the new field, which this contract's own
        rule reserves for a version bump."""
        self.assertNotIn("unconfirmed_writeback_on_order", rr.MUST_CONSUME)

    def test_the_fact_is_always_present_and_always_a_list(self):
        """An observer branching on it must not have to tell "no unconfirmed
        write-back" from "this build does not report them"."""
        install_sales_order_lookup(self)
        facts = rr.facts_from_webhook(webhook_payload(), shop_domain=STORE)
        self.assertEqual(facts["unconfirmed_writeback_on_order"], [])

        seed_unverified_writeback(refund_name="REF-00311")
        facts = rr.facts_from_webhook(webhook_payload(), shop_domain=STORE)
        self.assertEqual(facts["unconfirmed_writeback_on_order"], ["REF-00311"])

    def test_it_is_inert_on_a_site_with_no_payment_portals(self):
        """Same rule as every other entry point.  With no write-back fields the
        lookup answers nothing, which reads as "no unconfirmed write-back" —
        never as "cannot tell, so stay quiet"."""
        install_sales_order_lookup(self)
        seed_unverified_writeback()
        frappe_stub.META_FIELDS[writeback.REFUND_REQUEST] = set()
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_REPORTED)
        self.assertEqual(good_observer.seen["unconfirmed_writeback_on_order"],
                         [])

    def test_a_broken_lookup_reports_with_an_empty_fact_rather_than_raising(self):
        """This runs inside a webhook that must return 200 about a refund that
        has already happened.  A lookup that cannot answer must not take the
        report down with it."""
        install_sales_order_lookup(self)
        seed_unverified_writeback()

        def _boom(*a, **k):
            raise RuntimeError("no such table")

        frappe.get_all = _boom
        frappe_stub.register_observer("app.obs.good", good_observer)
        result = rr.report_refund(self.facts())

        self.assertEqual(result["outcome"], rr.OUTCOME_REPORTED)
        self.assertEqual(good_observer.seen["unconfirmed_writeback_on_order"],
                         [])
        self.assertTrue(frappe_stub.ERRORS,
                        "a lookup that could not decide was silent")

    def test_the_withheld_outcome_is_gone_from_the_vocabulary(self):
        """It described a report nothing would ever re-drive.  A caller that
        branches on outcomes must not find it listed."""
        self.assertNotIn(WITHDRAWN_OUTCOME, rr.OUTCOMES)
        self.assertFalse(hasattr(rr, "OUTCOME_" + WITHDRAWN_OUTCOME.upper()))
        self.assertEqual(rr.DELIBERATE_SKIPS,
                         frozenset({rr.OUTCOME_OWN_WRITEBACK}))

    def test_surface_reads_the_skip_set_rather_than_a_literal(self):
        """DELIBERATE_SKIPS' own comment promises that `needs_report_note` and
        `_surface` both consult it, so that a future skip has to be added in
        one place.  `_surface` used to name its outcomes in a literal tuple
        instead, which made that promise false in the direction that matters:
        a skip added to the set would be honoured by the note and still
        written to the Error Log by this, and an Error Log that carries a line
        for every refund this app writes back stops being read.

        Pinned by adding a synthetic slug to the set, because a test that only
        checked the two real outcomes would pass against the literal."""
        synthetic = "a_future_deliberate_skip"
        original = rr.DELIBERATE_SKIPS
        rr.DELIBERATE_SKIPS = frozenset(original | {synthetic})
        try:
            rr._surface(
                {"outcome": synthetic, "delivered": False, "message": "x"},
                self.facts(),
            )
            self.assertFalse(
                frappe_stub.ERRORS,
                "_surface logged an outcome listed in DELIBERATE_SKIPS, so it "
                "is not reading the set",
            )
            self.assertFalse(rr.needs_report_note(synthetic))
        finally:
            rr.DELIBERATE_SKIPS = original

    def test_nothing_anywhere_still_references_the_withheld_outcome(self):
        """A dangling slug in api.py or the contract is a reader believing a
        branch exists that cannot fire.  Matched case-insensitively, so the
        constant name is caught as well as the slug."""
        root = Path(__file__).resolve().parents[2]
        for relative in ("shopify_integration/utils/refund_report.py",
                         "shopify_integration/utils/credit_note.py",
                         "shopify_integration/api.py",
                         "REFUND-REPORT-CONTRACT.md",
                         "REFUND-WRITEBACK-BRIEF.md",
                         "shopify_integration/tests/test_refund_report.py"):
            text = (root / relative).read_text(encoding="utf-8").lower()
            self.assertNotIn(WITHDRAWN_OUTCOME, text, relative)

    def test_the_module_docstring_no_longer_claims_the_gid_precedes_the_post(self):
        """The published rationale was factually false: refund.py commits only
        the Unverified marker before the post, and the GID lands after the
        response.  That sentence asserted a guarantee the guard did not have
        and concealed this whole window."""
        text = " ".join((rr.__doc__ or "").split())
        self.assertNotRegex(
            text, r"(?i)commits the refund GID \*?before\*? the post",
            "the docstring still claims the GID is committed before the post",
        )
        self.assertIn("Unverified", text)


class TestTheCreditNotePathWithholdsInstead(RefundReportTestCase):
    """The same evidence, the opposite decision — and why that is not a bug.

    Tested beside the report path on purpose.  The two paths read the same
    `Unverified` row and go different ways, and a reader who meets only one of
    them "fixes" the asymmetry.  What differs is the cost of being wrong:

      * report path — a duplicate report is one the observer must dedupe on
        `shopify_refund_id`, and its own text says so.  Withholding risks a
        refund nothing ever records.
      * credit-note path — a duplicate Credit Note is a real accounting
        document somebody has to cancel by hand, and the skip is already
        visible: `_create_credit_note_background` writes a "Skipped" row with a
        reason to the Shopify Log, which nothing on the report path does.

    Before this, `create_credit_note_from_shopify_refund` guarded on the GID
    alone — the field `refund.py` writes only AFTER a successful response — so
    the blind window round 2 found for the report path was wide open here.
    """

    def setUp(self):
        super().setUp()
        install_sales_order_lookup(self)
        self.settings = frappe_stub.FakeSettings()
        # `make_return_doc` is ERPNext's, and there is no bench here.  Only the
        # no-regression case reaches it; both guard cases must return before
        # this import is even attempted, which is itself worth pinning.
        self._install_erpnext_stub()

    def _install_erpnext_stub(self):
        import sys

        made = []

        def make_return_doc(doctype, name):
            made.append((doctype, name))
            return frappe.get_doc({
                "doctype": doctype, "is_return": 1, "return_against": name,
                "items": [],
            })

        modules = {}
        for path in ("erpnext", "erpnext.controllers",
                     "erpnext.controllers.accounts_controller"):
            module = types.ModuleType(path)
            module.__path__ = []
            modules[path] = module
        modules["erpnext.controllers.accounts_controller"].make_return_doc = (
            make_return_doc)
        saved = {path: sys.modules.get(path) for path in modules}

        def restore():
            for path, previous in saved.items():
                if previous is None:
                    sys.modules.pop(path, None)
                else:
                    sys.modules[path] = previous

        sys.modules.update(modules)
        self.addCleanup(restore)
        self.made = made

    def seed_invoice(self):
        """A submitted Sales Order and Sales Invoice for the order, so the
        no-regression case has something to return against."""
        frappe_stub.set_doc("Sales Order", "SO-0001", {
            "name": "SO-0001", "shopify_order_id": ORDER_ID, "docstatus": 1,
            "shopify_store": STORE,
        })
        frappe_stub.SQL_RESULTS["FROM `tabSales Invoice Item`"] = [["SINV-0001"]]

    def refund_data(self):
        return {"id": REFUND_ID, "order_id": ORDER_ID}

    def test_an_unconfirmed_writeback_on_the_order_creates_no_credit_note(self):
        """The window that was open: no GID exists yet, so the old guard saw
        nothing and built a second Credit Note for a refund this app may have
        posted itself."""
        from shopify_integration.utils import credit_note as cn

        self.seed_invoice()
        seed_unverified_writeback(refund_name="REF-00311")

        result = cn.create_credit_note_from_shopify_refund(
            self.refund_data(), self.settings)

        self.assertIsNone(result)
        self.assertEqual(
            [d for d in frappe_stub.INSERTS
             if d.get("doctype") == "Sales Invoice"], [])
        self.assertEqual(self.made, [], "make_return_doc was reached anyway")

    def test_the_shopify_log_reason_names_the_refund_request_and_the_way_out(self):
        """The skip has to be actionable on the row a person opens.  "Skipped"
        with the own-refund sentence would say ERPNext already has the Credit
        Note, which is exactly what nobody knows here."""
        from shopify_integration.utils import credit_note as cn

        self.seed_invoice()
        seed_unverified_writeback(refund_name="REF-00311")

        cn._create_credit_note_background(self.refund_data(), "Test Store",
                                          log_name="SL-0001")

        rows = [(name, values) for doctype, name, values, _kw
                in frappe_stub.WRITES if doctype == "Shopify Log"]
        self.assertTrue(rows, "no Shopify Log row was written")
        _name, values = rows[-1]
        self.assertEqual(values["status"], "Skipped")
        reason = values["error_message"]
        self.assertIn("REF-00311", reason)
        self.assertIn("may", reason.lower())
        self.assertIn("resolve_unverified_writeback", reason)
        # And it must not claim the Credit Note already exists, which is the
        # own-refund reason and the one thing that is not known here.
        self.assertNotIn("already exists", reason)

    def test_the_two_reasons_are_distinct(self):
        """A single reason for both skips reads as "ERPNext already has it" on
        the case where nobody knows whether it does."""
        from shopify_integration.utils import credit_note as cn

        self.seed_invoice()
        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}", "REF-00312")
        cn._create_credit_note_background(self.refund_data(), "Test Store",
                                          log_name="SL-0001")
        own = [values for doctype, _n, values, _k in frappe_stub.WRITES
               if doctype == "Shopify Log"][-1]["error_message"]

        frappe_stub.reset()
        install_sales_order_lookup(self)
        self.seed_invoice()
        seed_unverified_writeback(refund_name="REF-00311")
        cn._create_credit_note_background(self.refund_data(), "Test Store",
                                          log_name="SL-0002")
        unconfirmed = [values for doctype, _n, values, _k in frappe_stub.WRITES
                       if doctype == "Shopify Log"][-1]["error_message"]

        self.assertNotEqual(own, unconfirmed)

    def test_a_gid_matched_own_refund_still_returns_none(self):
        """The guard that was already here, unchanged."""
        from shopify_integration.utils import credit_note as cn

        self.seed_invoice()
        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}", "REF-00312")

        self.assertIsNone(cn.create_credit_note_from_shopify_refund(
            self.refund_data(), self.settings))
        self.assertEqual(self.made, [])

    def test_no_unconfirmed_row_and_no_gid_match_still_creates_the_credit_note(self):
        """The no-regression case, and the one that matters most: an ordinary
        external refund must still produce its Credit Note."""
        from shopify_integration.utils import credit_note as cn

        self.seed_invoice()
        frappe_stub.META_FIELDS[writeback.REFUND_REQUEST] = {
            writeback.REFUND_GID_FIELD, writeback.WRITEBACK_STATUS_FIELD}

        name = cn.create_credit_note_from_shopify_refund(
            self.refund_data(), self.settings)

        self.assertTrue(name)
        self.assertEqual(self.made, [("Sales Invoice", "SINV-0001")])
        self.assertEqual(
            [d["return_against"] for d in frappe_stub.INSERTS
             if d.get("doctype") == "Sales Invoice"], ["SINV-0001"])

    def test_a_row_on_another_order_does_not_block_the_credit_note(self):
        """Order-scoped here too.  An unconfirmed write-back on one order is no
        reason to withhold the Credit Note for a refund on another."""
        from shopify_integration.utils import credit_note as cn

        self.seed_invoice()
        seed_unverified_writeback(refund_name="REF-00311",
                                  sales_order="SO-0002",
                                  shopify_order_id="9999999999999")

        self.assertTrue(cn.create_credit_note_from_shopify_refund(
            self.refund_data(), self.settings))

    def test_the_comment_records_why_the_two_paths_disagree(self):
        """The asymmetry is deliberate, and a reader who cannot see why will
        "fix" one path to match the other — in whichever direction is wrong."""
        import inspect

        from shopify_integration.utils import credit_note as cn

        source = " ".join(inspect.getsource(cn).split())
        self.assertRegex(source, r"(?i)report")
        self.assertRegex(source, r"(?i)by hand|manually")
        self.assertIn("unverified_writebacks_for_order", source)


class TestEveryDeliveryIsVisibleAfterwards(RefundReportTestCase):
    """Delivery is at-least-once, so a repeat delivery is expected rather than
    prevented.  What makes that liveable is that a person can see it happened:
    one log line per delivery, naming the refund and how it was discovered."""

    def test_a_delivery_names_the_refund_and_the_source(self):
        lines = capture_logger(self)
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.report_refund_from_webhook(webhook_payload(), shop_domain=STORE)

        self.assertTrue(any(REFUND_ID in line and "webhook" in line
                            for line in lines), lines)

    def test_the_line_says_a_repeat_is_a_redelivery_not_a_second_refund(self):
        """The line is read by a person asking "was this refunded twice?". It
        has to answer that, not merely record a delivery."""
        lines = capture_logger(self)
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.report_refund_from_webhook(webhook_payload(), shop_domain=STORE)
        self.assertTrue(any("at-least-once" in line for line in lines), lines)


class TestOutcomeVocabulary(RefundReportTestCase):

    def test_delivered_is_true_for_exactly_one_outcome(self):
        """The same discipline as retry_safe in the dispatch contract: one
        boolean a caller can read instead of remembering a set of strings."""
        self.assertEqual(
            {o for o in rr.OUTCOMES if rr.delivered(o)},
            {rr.OUTCOME_REPORTED},
        )

    def test_own_writeback_is_in_the_vocabulary_and_never_delivered(self):
        """It is a correct, deliberate skip -- but "delivered" means the
        observer was told, and for our own refund nobody was told by us."""
        self.assertIn(rr.OUTCOME_OWN_WRITEBACK, rr.OUTCOMES)
        self.assertFalse(rr.delivered(rr.OUTCOME_OWN_WRITEBACK))

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

    def test_the_guard_applies_on_the_backfill_path_too(self):
        """The reason the guard is in report_refund and not in api.py.  A
        backfill of an order this app refunded reads our own refund straight
        back out of Shopify, and an api.py-side check would not be on this
        path at all."""
        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}", "REF-00311")
        frappe_stub.register_observer("app.obs.good", good_observer)
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertEqual(out["read"], 1)
        self.assertEqual(out["reported"], 0)
        self.assertEqual(out["reports"][0]["outcome"],
                         rr.OUTCOME_OWN_WRITEBACK)
        self.assertEqual(frappe_stub.CALLS, [])
        self.assertEqual(frappe_stub.ERRORS, [])

    def test_the_same_refund_twice_in_one_read_is_reported_once(self):
        """Within one run the repeat is visible, so it is dropped.  Across runs
        it is not -- see the at-least-once tests below."""
        node = refunds_response()["order"]["refunds"][0]
        self.responses = [refunds_response(refunds=[node, dict(node)])]
        frappe_stub.register_observer("app.obs.good", good_observer)
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertEqual(out["read"], 2)
        self.assertEqual(out["reported"], 1)
        self.assertEqual(out["duplicates"], 1)
        self.assertEqual(len(frappe_stub.CALLS), 1)

    def test_the_dropped_duplicate_is_named_rather_than_silently_capped(self):
        """This codebase does not drop rows quietly: on REF-00207 the rows were
        examined and discarded, and the verdict that survived did not fit
        them.  Whatever was dropped has to be readable afterwards."""
        node = refunds_response()["order"]["refunds"][0]
        self.responses = [refunds_response(refunds=[node, dict(node)])]
        frappe_stub.register_observer("app.obs.good", good_observer)
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertIn("929361464", out["message"])
        self.assertIn("duplicate", out["message"].lower())

    def test_two_different_refunds_on_one_order_are_both_reported(self):
        """A partial refund followed by another is two refunds, not a
        duplicate.  Which is why the dedupe key is the refund id."""
        first = refunds_response()["order"]["refunds"][0]
        second = dict(first, id="gid://shopify/Refund/929361465")
        self.responses = [refunds_response(refunds=[first, second])]
        frappe_stub.register_observer("app.obs.good", good_observer)
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertEqual(out["reported"], 2)
        self.assertEqual(out["duplicates"], 0)
        self.assertEqual(len(frappe_stub.CALLS), 2)

    def test_nodes_with_no_id_are_reported_rather_than_deduped(self):
        """Two refunds we cannot name are not evidence of one refund.  A
        duplicate the observer absorbs beats an omission nothing can."""
        blank = {"id": "", "note": "", "createdAt": "2026-09-01T10:15:00Z",
                 "totalRefundedSet": {"presentmentMoney":
                                      {"amount": "100.00",
                                       "currencyCode": "INR"}},
                 "transactions": {"edges": []}}
        self.responses = [refunds_response(refunds=[blank, dict(blank)])]
        frappe_stub.register_observer("app.obs.good", good_observer)
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertEqual(out["duplicates"], 0)
        self.assertEqual(len(frappe_stub.CALLS), 2)

    def test_a_skipped_own_refund_is_counted_and_named_in_the_message(self):
        """"Read 1 refund(s); 0 reported" with no reason is unreadable, and it
        is what an operator got for the EXPECTED outcome of every refund this
        app writes back.  Same rule as the dropped duplicates: whatever was not
        reported is named, because a bare count is the REF-00207 failure."""
        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}", "REF-00311")
        frappe_stub.register_observer("app.obs.good", good_observer)
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertEqual(out["read"], 1)
        self.assertEqual(out["reported"], 0)
        self.assertEqual(out["own_writeback"], 1)
        self.assertIn(REFUND_ID, out["message"])
        self.assertIn("own_writeback", out["message"])
        self.assertIn("REF-00311", out["message"])

    def test_a_refund_beside_an_unconfirmed_writeback_is_reported_and_named(self):
        """Reported — not withheld — and the backfill says which row a person
        may have to reconcile it against.  A count with no ids is the REF-00207
        failure, and "1 reported" alone would hide that this is the one
        delivery somebody may need to check by hand."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311")
        frappe_stub.register_observer("app.obs.good", good_observer)
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertEqual(out["reported"], 1)
        self.assertEqual(out["own_writeback"], 0)
        self.assertEqual(out["unconfirmed_writeback"], 1)
        self.assertIn("REF-00311", out["message"])
        self.assertIn("unconfirmed", out["message"].lower())
        self.assertTrue(frappe_stub.ERRORS)

    def test_the_fact_reaches_the_observer_on_the_backfill_path_too(self):
        """Same shape from both fact builders, so an observer cannot tell a
        backfilled refund from a live one except by `source`."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311")
        frappe_stub.register_observer("app.obs.good", good_observer)
        rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertEqual(good_observer.seen["source"], "backfill")
        self.assertEqual(good_observer.seen["unconfirmed_writeback_on_order"],
                         ["REF-00311"])

    def test_nothing_is_said_about_skips_when_there_were_none(self):
        """The message stays readable in the ordinary case."""
        frappe_stub.register_observer("app.obs.good", good_observer)
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertEqual(out["own_writeback"], 0)
        self.assertEqual(out["unconfirmed_writeback"], 0)
        self.assertNotIn("own_writeback", out["message"])
        self.assertNotIn("unconfirmed", out["message"].lower())

    def test_the_return_message_says_delivery_is_at_least_once(self):
        """"3 reported" must not read as three distinct refunds.  It is three
        reports handed over, and a webhook retry or a second backfill will hand
        the same one over again."""
        frappe_stub.register_observer("app.obs.good", good_observer)
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)
        self.assertIn("at-least-once", out["message"])

    def test_a_second_backfill_reports_the_same_refund_again(self):
        """Pinned deliberately, as documented behaviour rather than a bug
        awaiting a ledger.  Suppressing a report that was needed is the worse
        failure here: a refund with no Cashfree row would then be recorded by
        nothing at all.  The observer dedupes on shopify_refund_id."""
        frappe_stub.register_observer("app.obs.good", good_observer)
        self.responses = [refunds_response(), refunds_response()]
        rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)
        out = rr.backfill_order_refunds(ORDER_ID, shop_domain=STORE)

        self.assertEqual(out["reported"], 1)
        self.assertEqual(len(frappe_stub.CALLS), 2)

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


class TestTheWebhookLogNote(RefundReportTestCase):
    """What `api.py` pastes into the Shopify Log's error_message.

    The note exists so a report that did NOT land is visible on the log row a
    person opens.  Branching it on `delivered` alone made it fire for the two
    deliberate skips as well — and `own_writeback` is the EXPECTED outcome for
    every refund this app writes back, so its explanatory sentence landed in
    the error_message of every one of those webhooks.  An error field that
    carries a routine sentence is an error field nobody reads.
    """

    def drive(self, payload=None):
        """Run api.py's refunds/create branch and return its update_log_status
        calls.  The webhook itself is the only place the note is built, so the
        alternative to driving it is asserting on a copy of the expression."""
        import shopify_integration.api as api

        calls = []
        saved = {name: getattr(api, name) for name in
                 ("log_webhook", "update_log_status", "get_settings_for_store")}
        had_flags = hasattr(frappe, "flags")
        saved_flags = getattr(frappe, "flags", None)
        saved_request = getattr(frappe, "request", None)

        def restore():
            for name, fn in saved.items():
                setattr(api, name, fn)
            if had_flags:
                frappe.flags = saved_flags
            else:
                del frappe.flags
            if saved_request is None:
                if hasattr(frappe, "request"):
                    del frappe.request
            else:
                frappe.request = saved_request

        self.addCleanup(restore)

        body = json.dumps(payload if payload is not None else webhook_payload())
        frappe.flags = types.SimpleNamespace(ignore_permissions=False)
        frappe.request = types.SimpleNamespace(
            get_data=lambda cache=True: body.encode("utf-8"),
            headers={"X-Shopify-Topic": "refunds/create",
                     "X-Shopify-Shop-Domain": STORE},
        )
        # A store with sync on, no webhook_secret (so the HMAC step is skipped,
        # as it is on a store that has not set one) and credit notes off, which
        # is the shortest route to the branch that appends the note.
        api.get_settings_for_store = lambda domain: types.SimpleNamespace(
            name="Test Store",
            get=lambda key, default=None: {"shop_domain": STORE}.get(key, default),
        )
        api.log_webhook = lambda *a, **k: "SL-0001"
        api.update_log_status = lambda **k: calls.append(k)

        api.shopify_webhook()
        return calls

    def error_text(self, calls):
        self.assertTrue(calls, "api.py wrote no Shopify Log status at all")
        return calls[-1].get("error") or ""

    def test_a_deliberate_own_writeback_skip_writes_no_note(self):
        seed_own_writeback(f"gid://shopify/Refund/{REFUND_ID}", "REF-00311")
        frappe_stub.register_observer("app.obs.good", good_observer)
        text = self.error_text(self.drive())

        self.assertNotIn("Refund report:", text)
        self.assertNotIn("REF-00311", text)

    def test_a_delivered_report_beside_an_unconfirmed_writeback_writes_no_note(self):
        """It landed, so the note has nothing to say: the note exists for a
        report that did NOT land.  The reconciliation line is already in the
        Error Log with what to do, and the Shopify Log row's error field is not
        a second copy of that."""
        install_sales_order_lookup(self)
        seed_unverified_writeback(refund_name="REF-00311")
        frappe_stub.register_observer("app.obs.good", good_observer)
        text = self.error_text(self.drive())

        self.assertNotIn("Refund report:", text)
        self.assertNotIn("REF-00311", text)
        self.assertTrue(frappe_stub.ERRORS, "the reconciliation line is gone")

    def test_a_genuine_non_delivery_still_writes_the_note(self):
        """The case the note was added for: nobody has been told, and the log
        row a person opens has to say so."""
        frappe_stub.register_observer("app.obs.narrow", narrow_observer)
        text = self.error_text(self.drive())

        self.assertIn("Refund report:", text)
        self.assertIn("settlement_channel", text)

    def test_a_delivered_report_writes_no_note(self):
        frappe_stub.register_observer("app.obs.good", good_observer)
        self.assertNotIn("Refund report:", self.error_text(self.drive()))

    def test_the_predicate_is_the_one_place_that_decides(self):
        """`needs_report_note` lives beside the outcome vocabulary so api.py
        cannot drift from it, and a structural check keeps api.py consulting it
        rather than re-deriving the answer from `delivered`."""
        import inspect

        self.assertFalse(rr.needs_report_note(rr.OUTCOME_REPORTED))
        self.assertFalse(rr.needs_report_note(rr.OUTCOME_OWN_WRITEBACK))
        for outcome in (rr.OUTCOME_UNACKNOWLEDGED, rr.OUTCOME_NO_OBSERVER,
                        rr.OUTCOME_REFUSED, rr.OUTCOME_FAILED):
            self.assertTrue(rr.needs_report_note(outcome), outcome)

        import shopify_integration.api as api
        source = inspect.getsource(api.shopify_webhook)
        self.assertIn("needs_report_note", source)


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

    def test_the_at_least_once_delivery_guarantee_is_stated(self):
        """It was previously true and unsaid, which is the worst combination:
        an observer author reading this document had no way to know a second
        webhook or a second backfill hands them the same refund again."""
        self.assertIn("at-least-once", self.text)

    def test_the_observers_dedupe_obligation_is_stated_as_a_rule(self):
        """`shopify_refund_id` is in MUST_CONSUME because of this, and the
        comment there already says so -- but an obligation the other side has
        to infer from a comment on our side is not an obligation."""
        self.assertRegex(
            self.text,
            r"(?is)must dedupe[^.]{0,240}shopify_refund_id",
        )

    def test_the_absence_of_an_already_reported_ledger_is_explained(self):
        """The architectural limit is deliberate and has to read that way, or
        the next person adds the ledger that turns a duplicate the observer
        absorbs into an omission nothing can."""
        self.assertIn("permanent omission", self.text)

    def test_the_own_writeback_guard_is_documented(self):
        self.assertIn("`own_writeback`", self.text)
        self.assertIn("refund_request_for_shopify_refund", self.text)

    def test_the_document_does_not_claim_the_gid_precedes_the_post(self):
        """It said `refund.py` "commits the refund GID *before* the post, so
        the returning webhook can be recognised as ours".  It does not — only
        the Unverified marker goes in before the post — so that sentence
        promised a guarantee the guard did not have and hid the window the
        second lookup covers."""
        prose = " ".join(self.text.split())
        self.assertNotRegex(
            prose, r"(?i)commits the refund GID \*?before\*? the post",
            "the contract still claims the GID is committed before the post",
        )
        # And the retraction is present, not merely the deletion: an observer
        # author who integrated against version 1 has to be told the sentence
        # they read was wrong, or they keep believing it.
        self.assertRegex(prose, r"(?i)was\s+\*\*false\*\*")
        self.assertRegex(prose, r"(?i)only the Unverified marker is committed")

    def test_both_windows_are_named_and_so_is_what_covers_each(self):
        """An observer author has to be able to tell the two windows apart: the
        GID covers the refund whose response we read, and it is the only skip;
        the Unverified lookup covers the one we posted and never heard about,
        and it produces a delivered report carrying a fact."""
        self.assertIn("unverified_writebacks_for_order", self.text)
        self.assertIn("resolve_unverified_writeback", self.text)
        self.assertIn("`unconfirmed_writeback_on_order`", self.text)

    def test_the_optional_fact_is_documented_as_optional(self):
        """An observer author has to be told two things about it: that it is not
        in MUST_CONSUME, and that not declaring the parameter costs nothing —
        frappe.call drops it and the report still lands.  A reader who takes it
        for a required field either re-integrates for nothing or believes their
        reports have stopped being delivered."""
        prose = " ".join(self.text.split())
        self.assertRegex(prose, r"(?i)optional")
        self.assertRegex(prose, r"(?i)not in `?MUST_CONSUME`?")
        self.assertRegex(prose, r"(?is)frappe\.call[^.]{0,200}drop")

    def test_the_document_does_not_promise_a_withheld_report(self):
        """Round 2 documented an outcome that withheld the report and named
        `backfill_now` as the way to re-drive it.  Nothing re-drives it on its
        own, and an observer author reading that would wait for a report that
        never comes."""
        self.assertNotIn(WITHDRAWN_OUTCOME, self.text.lower())
        prose = " ".join(self.text.split())
        self.assertNotRegex(prose, r"(?i)withheld, and \*\*logged\*\*")

    def test_the_change_that_bumped_the_version_is_recorded(self):
        """A version bump nobody can read the reason for is a number that goes
        stale the next time somebody asks whether they must re-integrate."""
        prose = " ".join(self.text.split())
        self.assertRegex(prose, r"(?i)version 2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
