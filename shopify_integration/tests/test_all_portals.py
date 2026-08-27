"""
test_all_portals.py — one reference field, every payment portal.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_all_portals -v

The fixtures below are trimmed copies of REAL payloads and settlement rows from
the live store, so these tests pin actual observed shapes rather than invented
ones:

  PayU      transaction.authorization  = rkdkuLhOZPiHLp9XVygf0ASij
            Gateway Transaction.gateway_order_ref = the same string
  Cashfree  payment_gateway_names ["manual"], tags "CASHFREE - UPI",
            note_attributes.pg_order_id = notdrones.myshopify.com_xdgddtxyga
            Gateway Transaction.gateway_order_ref = the same string
  Snapmint  gateway_order_ref 9650408503 with no Shopify counterpart, and
            settlement rows that already match on order name
"""

import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.utils import gateway_reference as gr  # noqa: E402

PAYU_TXNID = "rkdkuLhOZPiHLp9XVygf0ASij"
CASHFREE_REF = "notdrones.myshopify.com_xdgddtxyga"


# ── Fixtures (trimmed from live payloads) ─────────────────────────────────────

def payu_txn():
    return {
        "id": 8811, "kind": "sale", "status": "success",
        "created_at": "2026-08-21T22:57:37+05:30",
        "gateway": "Cards, UPI, NB by PayU India",
        "authorization": PAYU_TXNID,
    }


def payu_order():
    return {
        "id": 7840691126377, "name": "#6428",
        "payment_gateway_names": ["Cards, UPI, NB by PayU India"],
        "tags": "",
        "note_attributes": [{"name": "cart_token", "value": "abc"}],
    }


def cashfree_txn():
    """A Cashfree-via-custom-app payment: Shopify saw nothing."""
    return {
        "id": 10167847354473, "kind": "sale", "status": "success",
        "created_at": "2026-08-27T14:14:57+05:30",
        "gateway": "manual",
        "authorization": None,
        "receipt": {},
    }


def cashfree_order():
    return {
        "id": 7840770392169, "name": "#6498",
        "payment_gateway_names": ["manual"],
        "tags": "CASHFREE - UPI",
        "note_attributes": [
            {"name": "pg_order_id", "value": CASHFREE_REF},
            {"name": "cart_token", "value": "hWNG1OqGup0hOrIVkXb8bqse"},
            {"name": "customer_ip", "value": "49.14.127.220"},
        ],
    }


def snapmint_order():
    return {
        "id": 7840691126000, "name": "#6497",
        "payment_gateway_names": ["snapmint"],
        "tags": "snapmint, snapmint_80746688, snapmint_exp_chk",
        "note_attributes": [{"name": "cart_token", "value": "xyz"}],
    }


# ── PayU: the case the field exists for ───────────────────────────────────────

class TestPayU(unittest.TestCase):
    """
    PayU settlement rows carry no platform_order_id or platform_order_name — of
    681 rows on live, zero matched automatically. The authorization is the only
    join key it offers.
    """

    def setUp(self):
        frappe_stub.reset()

    def test_reference_is_the_txnid(self):
        self.assertEqual(
            gr.extract_gateway_reference(payu_txn(), payu_order()), PAYU_TXNID
        )

    def test_matches_the_settlement_gateway_order_ref(self):
        settlement_gateway_order_ref = PAYU_TXNID   # observed on live
        self.assertEqual(
            gr.extract_gateway_reference(payu_txn(), payu_order()),
            settlement_gateway_order_ref,
        )

    def test_works_without_the_order(self):
        """The transaction alone is enough for PayU."""
        self.assertEqual(gr.extract_gateway_reference(payu_txn()), PAYU_TXNID)

    def test_gateway_name_is_the_real_gateway(self):
        self.assertEqual(
            gr.extract_gateway_name(payu_txn(), payu_order()),
            "Cards, UPI, NB by PayU India",
        )


# ── Cashfree via a custom app: transaction empty, order carries it ────────────

class TestCashfreeAsManual(unittest.TestCase):
    """
    payment_gateway_names reads ["manual"] and the transaction is empty, but
    note_attributes.pg_order_id holds the value that appears as
    gateway_order_ref on the settlement row.
    """

    def setUp(self):
        frappe_stub.reset()

    def test_transaction_alone_yields_nothing(self):
        """This is what produced the blank field before the fix."""
        self.assertEqual(gr.extract_gateway_reference(cashfree_txn()), "")

    def test_order_supplies_the_reference(self):
        self.assertEqual(
            gr.extract_gateway_reference(cashfree_txn(), cashfree_order()),
            CASHFREE_REF,
        )

    def test_matches_the_settlement_gateway_order_ref(self):
        settlement_gateway_order_ref = CASHFREE_REF   # observed on live
        self.assertEqual(
            gr.extract_gateway_reference(cashfree_txn(), cashfree_order()),
            settlement_gateway_order_ref,
        )

    def test_gateway_name_is_not_manual(self):
        """
        "manual" on a reconciliation field is worse than useless — it hides that
        the payment was Cashfree. The tags carry the real gateway.
        """
        name = gr.extract_gateway_name(cashfree_txn(), cashfree_order())
        self.assertEqual(name, "CASHFREE - UPI")
        self.assertNotEqual(name.lower(), "manual")

    def test_works_with_no_transaction_at_all(self):
        """A Cashfree order can be resolved from the order payload alone."""
        self.assertEqual(
            gr.extract_gateway_reference(None, cashfree_order()), CASHFREE_REF
        )


# ── Snapmint: deliberately unhandled ─────────────────────────────────────────

class TestSnapmint(unittest.TestCase):
    """
    Snapmint's gateway_order_ref is a 10-digit id (9650408503) with no
    counterpart in the Shopify payload — the tag id (80746688) is a different
    number. Its settlement rows already carry platform_order_name, so 151 of 154
    match on order name without help. Capturing the tag id would put a value in
    the field that joins to nothing.
    """

    def setUp(self):
        frappe_stub.reset()

    def test_no_reference_captured(self):
        self.assertEqual(gr.extract_gateway_reference(None, snapmint_order()), "")

    def test_tag_id_is_not_mistaken_for_a_reference(self):
        ref = gr.extract_gateway_reference(None, snapmint_order())
        self.assertNotIn("80746688", ref)

    def test_gateway_name_still_identifies_the_portal(self):
        """Blank reference, but we still record who took the money."""
        name = gr.extract_gateway_name({"gateway": "snapmint"}, snapmint_order())
        self.assertEqual(name, "snapmint")


# ── The resolution chain ─────────────────────────────────────────────────────

class TestResolutionOrder(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()

    def test_transaction_wins_over_note_attributes(self):
        """
        When both exist the transaction is authoritative: it is the gateway's own
        normalised reference, while note_attributes is written by a third-party
        app.
        """
        order = cashfree_order()
        self.assertEqual(gr.extract_gateway_reference(payu_txn(), order), PAYU_TXNID)

    def test_receipt_beats_note_attributes(self):
        txn = {"authorization": "", "receipt": {"txnid": "from_receipt"}}
        self.assertEqual(
            gr.extract_gateway_reference(txn, cashfree_order()), "from_receipt"
        )

    def test_only_verified_note_attribute_keys_are_read(self):
        """An unverified key must not be guessed at."""
        self.assertEqual(gr._NOTE_ATTRIBUTE_KEYS, ("pg_order_id",))

    def test_unknown_note_attribute_is_ignored(self):
        order = {"note_attributes": [{"name": "some_other_ref", "value": "XYZ"}]}
        self.assertEqual(gr.extract_gateway_reference(cashfree_txn(), order), "")

    def test_blank_note_attribute_value_is_not_a_reference(self):
        order = {"note_attributes": [{"name": "pg_order_id", "value": "   "}]}
        self.assertEqual(gr.extract_gateway_reference(cashfree_txn(), order), "")

    def test_null_string_note_attribute_is_rejected(self):
        order = {"note_attributes": [{"name": "pg_order_id", "value": "null"}]}
        self.assertEqual(gr.extract_gateway_reference(cashfree_txn(), order), "")

    def test_garbage_note_attributes_do_not_crash(self):
        for bad in ({"note_attributes": "nope"}, {"note_attributes": [None, 5]},
                    {"note_attributes": None}, {}, None):
            self.assertEqual(gr.extract_gateway_reference(cashfree_txn(), bad), "")

    def test_reference_is_truncated_to_the_field_length(self):
        order = {"note_attributes": [{"name": "pg_order_id", "value": "x" * 300}]}
        self.assertEqual(
            len(gr.extract_gateway_reference(cashfree_txn(), order)),
            gr._MAX_REFERENCE_LEN,
        )


# ── Writing it: still one field, still no placeholder ────────────────────────

class TestCaptureAcrossPortals(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()
        frappe_stub.set_doc("Payment Entry", "PE-0001", {
            "name": "PE-0001", "reference_no": "#6498",
            "custom_gateway_reference": "", "custom_gateway_name": "",
        })

    def test_cashfree_order_now_captures(self):
        """The #6498 case that came back blank on live."""
        ref = gr.capture_gateway_reference(
            "PE-0001", 7840770392169,
            settings=frappe_stub.FakeSettings(),
            transactions=[cashfree_txn()],
            order=cashfree_order(),
        )
        self.assertEqual(ref, CASHFREE_REF)
        stored = frappe_stub.get_doc_values("Payment Entry", "PE-0001")
        self.assertEqual(stored["custom_gateway_reference"], CASHFREE_REF)
        self.assertEqual(stored["custom_gateway_name"], "CASHFREE - UPI")

    def test_payu_order_captures(self):
        ref = gr.capture_gateway_reference(
            "PE-0001", 7840691126377,
            settings=frappe_stub.FakeSettings(),
            transactions=[payu_txn()],
            order=payu_order(),
        )
        self.assertEqual(ref, PAYU_TXNID)

    def test_snapmint_stays_blank_and_writes_nothing(self):
        ref = gr.capture_gateway_reference(
            "PE-0001", 7840691126000,
            settings=frappe_stub.FakeSettings(),
            transactions=[{"kind": "sale", "status": "success", "gateway": "snapmint"}],
            order=snapmint_order(),
        )
        self.assertEqual(ref, "")
        self.assertEqual(frappe_stub.WRITES, [], "no placeholder may be written")

    def test_still_only_the_two_gateway_fields(self):
        gr.capture_gateway_reference(
            "PE-0001", 7840770392169,
            settings=frappe_stub.FakeSettings(),
            transactions=[cashfree_txn()],
            order=cashfree_order(),
        )
        _dt, _n, values, _kw = frappe_stub.WRITES[0]
        self.assertEqual(set(values), {"custom_gateway_reference", "custom_gateway_name"})

    def test_reference_no_still_untouched(self):
        gr.capture_gateway_reference(
            "PE-0001", 7840770392169,
            settings=frappe_stub.FakeSettings(),
            transactions=[cashfree_txn()],
            order=cashfree_order(),
        )
        self.assertEqual(
            frappe_stub.get_doc_values("Payment Entry", "PE-0001")["reference_no"], "#6498"
        )
        for _dt, _n, values, _kw in frappe_stub.WRITES:
            self.assertNotIn("reference_no", values)


class TestBackfillFetchesTheOrder(unittest.TestCase):
    """
    The order-sync path is handed the payload for free. The backfill is not, so
    when the transaction yields nothing it must fetch the order before giving up
    — otherwise every Cashfree Payment Entry would backfill as blank.
    """

    def setUp(self):
        frappe_stub.reset()
        frappe_stub.set_doc("Payment Entry", "PE-0002", {
            "name": "PE-0002", "custom_gateway_reference": "", "custom_gateway_name": "",
        })

    def test_order_is_fetched_when_the_transaction_is_empty(self):
        calls = []
        original = gr.get_order
        gr.get_order = lambda settings, oid: (calls.append(oid), cashfree_order())[1]
        try:
            ref = gr.capture_gateway_reference(
                "PE-0002", 7840770392169,
                settings=frappe_stub.FakeSettings(),
                transactions=[cashfree_txn()],
            )
        finally:
            gr.get_order = original

        self.assertEqual(ref, CASHFREE_REF)
        self.assertEqual(len(calls), 1, "should fetch the order exactly once")

    def test_order_is_not_fetched_when_the_transaction_suffices(self):
        calls = []
        original = gr.get_order
        gr.get_order = lambda settings, oid: (calls.append(oid), payu_order())[1]
        try:
            ref = gr.capture_gateway_reference(
                "PE-0002", 7840691126377,
                settings=frappe_stub.FakeSettings(),
                transactions=[payu_txn()],
            )
        finally:
            gr.get_order = original

        self.assertEqual(ref, PAYU_TXNID)
        self.assertEqual(calls, [], "no wasted API call when authorization is present")

    def test_order_fetch_failure_is_survivable(self):
        original = gr.get_order

        def boom(settings, oid):
            raise gr.ShopifyAPIError("500 from Shopify", 500)

        gr.get_order = boom
        try:
            ref = gr.capture_gateway_reference(
                "PE-0002", 7840770392169,
                settings=frappe_stub.FakeSettings(),
                transactions=[cashfree_txn()],
            )
        finally:
            gr.get_order = original

        self.assertEqual(ref, "")
        self.assertEqual(frappe_stub.WRITES, [])
        self.assertTrue(any("Order Fetch Failed" in t for _m, t in frappe_stub.ERRORS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
