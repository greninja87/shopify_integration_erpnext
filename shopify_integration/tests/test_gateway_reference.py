"""
test_gateway_reference.py — unit tests for the pure gateway-reference logic.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_gateway_reference -v

The repo has no bench test harness, so `frappe` is faked (see frappe_stub.py)
before importing the module under test.  Only the pure functions
(select_gateway_transaction / extract_gateway_reference) are exercised here —
they hold all the decision logic and none of the I/O.
"""

import unittest


from shopify_integration.tests.frappe_stub import install as _install_frappe_stub

_install_frappe_stub()

from shopify_integration.utils.gateway_reference import (  # noqa: E402
    extract_gateway_name,
    extract_gateway_reference,
    select_gateway_transaction,
)


# ── Transaction selection ─────────────────────────────────────────────────────

class TestSelectGatewayTransaction(unittest.TestCase):
    def test_empty_and_none(self):
        self.assertIsNone(select_gateway_transaction([]))
        self.assertIsNone(select_gateway_transaction(None))

    def test_picks_successful_sale_over_authorization(self):
        txns = [
            {"id": 1, "kind": "authorization", "status": "success",
             "created_at": "2026-08-20T10:00:00+05:30"},
            {"id": 2, "kind": "sale", "status": "success",
             "created_at": "2026-08-20T10:00:05+05:30"},
        ]
        self.assertEqual(select_gateway_transaction(txns)["id"], 2)

    def test_picks_capture(self):
        txns = [{"id": 7, "kind": "capture", "status": "success",
                 "created_at": "2026-08-20T10:00:00+05:30"}]
        self.assertEqual(select_gateway_transaction(txns)["id"], 7)

    def test_ignores_failed_and_pending(self):
        txns = [
            {"id": 1, "kind": "sale", "status": "failure",
             "created_at": "2026-08-20T10:00:00+05:30"},
            {"id": 2, "kind": "sale", "status": "pending",
             "created_at": "2026-08-20T10:00:01+05:30"},
        ]
        self.assertIsNone(select_gateway_transaction(txns))

    def test_ignores_refund_and_void(self):
        txns = [
            {"id": 1, "kind": "refund", "status": "success",
             "created_at": "2026-08-20T10:00:00+05:30"},
            {"id": 2, "kind": "void", "status": "success",
             "created_at": "2026-08-20T10:00:01+05:30"},
        ]
        self.assertIsNone(select_gateway_transaction(txns))

    def test_earliest_wins_when_several_match(self):
        txns = [
            {"id": 3, "kind": "capture", "status": "success",
             "created_at": "2026-08-20T12:00:00+05:30"},
            {"id": 1, "kind": "sale", "status": "success",
             "created_at": "2026-08-20T09:00:00+05:30"},
            {"id": 2, "kind": "sale", "status": "success",
             "created_at": "2026-08-20T11:00:00+05:30"},
        ]
        self.assertEqual(select_gateway_transaction(txns)["id"], 1)

    def test_earliest_across_timezone_offsets(self):
        # 09:00+05:30 is 03:30 UTC — LATER than 03:00 UTC.  A naive string
        # sort would wrongly pick the +05:30 row first.
        txns = [
            {"id": 1, "kind": "sale", "status": "success",
             "created_at": "2026-08-20T09:00:00+05:30"},
            {"id": 2, "kind": "sale", "status": "success",
             "created_at": "2026-08-20T03:00:00+00:00"},
        ]
        self.assertEqual(select_gateway_transaction(txns)["id"], 2)

    def test_created_at_with_z_suffix(self):
        txns = [
            {"id": 1, "kind": "sale", "status": "success",
             "created_at": "2026-08-20T04:00:00Z"},
            {"id": 2, "kind": "sale", "status": "success",
             "created_at": "2026-08-20T03:00:00Z"},
        ]
        self.assertEqual(select_gateway_transaction(txns)["id"], 2)

    def test_status_and_kind_case_insensitive(self):
        txns = [{"id": 9, "kind": "SALE", "status": "SUCCESS",
                 "created_at": "2026-08-20T10:00:00+05:30"}]
        self.assertEqual(select_gateway_transaction(txns)["id"], 9)

    def test_unparseable_created_at_sorts_last(self):
        txns = [
            {"id": 1, "kind": "sale", "status": "success", "created_at": "not-a-date"},
            {"id": 2, "kind": "sale", "status": "success",
             "created_at": "2026-08-20T09:00:00+05:30"},
        ]
        self.assertEqual(select_gateway_transaction(txns)["id"], 2)

    def test_missing_created_at_still_selected(self):
        txns = [{"id": 5, "kind": "sale", "status": "success"}]
        self.assertEqual(select_gateway_transaction(txns)["id"], 5)

    def test_non_dict_rows_ignored(self):
        txns = ["garbage", None, {"id": 4, "kind": "sale", "status": "success"}]
        self.assertEqual(select_gateway_transaction(txns)["id"], 4)


# ── Reference extraction ──────────────────────────────────────────────────────

class TestExtractGatewayReference(unittest.TestCase):
    # Verified real value: Shopify order #6428 → PayU txnid.
    PAYU_TXNID = "rkdkuLhOZPiHLp9XVygf0ASij"

    def test_none_and_empty(self):
        self.assertEqual(extract_gateway_reference(None), "")
        self.assertEqual(extract_gateway_reference({}), "")

    def test_authorization_preferred(self):
        txn = {
            "authorization": self.PAYU_TXNID,
            "receipt": {"txnid": "should-not-win", "payment_id": "nor-this"},
        }
        self.assertEqual(extract_gateway_reference(txn), self.PAYU_TXNID)

    def test_payu_txnid_is_25_chars(self):
        self.assertEqual(len(extract_gateway_reference({"authorization": self.PAYU_TXNID})), 25)

    def test_falls_back_to_receipt_txnid(self):
        txn = {"authorization": "", "receipt": {"txnid": self.PAYU_TXNID}}
        self.assertEqual(extract_gateway_reference(txn), self.PAYU_TXNID)

    def test_falls_back_to_receipt_payment_id(self):
        txn = {"authorization": None,
               "receipt": {"txnid": "   ", "payment_id": "pay_XyZ123"}}
        self.assertEqual(extract_gateway_reference(txn), "pay_XyZ123")

    def test_whitespace_only_is_not_a_reference(self):
        txn = {"authorization": "   ", "receipt": {"txnid": "\t\n"}}
        self.assertEqual(extract_gateway_reference(txn), "")

    def test_all_empty_returns_blank_never_placeholder(self):
        self.assertEqual(extract_gateway_reference({"authorization": None, "receipt": {}}), "")

    def test_receipt_missing_entirely(self):
        self.assertEqual(extract_gateway_reference({"authorization": ""}), "")

    def test_receipt_as_json_string(self):
        # Some gateways serialise `receipt` as a JSON string rather than an object.
        txn = {"authorization": "", "receipt": '{"txnid": "' + self.PAYU_TXNID + '"}'}
        self.assertEqual(extract_gateway_reference(txn), self.PAYU_TXNID)

    def test_receipt_as_unparseable_string(self):
        self.assertEqual(
            extract_gateway_reference({"authorization": "", "receipt": "not json at all"}),
            "",
        )

    def test_numeric_reference_coerced_to_string(self):
        txn = {"authorization": "", "receipt": {"payment_id": 987654321}}
        self.assertEqual(extract_gateway_reference(txn), "987654321")

    def test_reference_is_stripped(self):
        txn = {"authorization": "  " + self.PAYU_TXNID + "  "}
        self.assertEqual(extract_gateway_reference(txn), self.PAYU_TXNID)

    def test_receipt_as_list_is_ignored(self):
        self.assertEqual(
            extract_gateway_reference({"authorization": "", "receipt": [1, 2, 3]}),
            "",
        )

    def test_null_string_is_not_a_reference(self):
        # Shopify sometimes returns the literal string "null" in receipt blobs.
        txn = {"authorization": "null", "receipt": {"txnid": "None"}}
        self.assertEqual(extract_gateway_reference(txn), "")


class TestGatewayNameNeverSaysManual(unittest.TestCase):
    """
    "manual" is what Shopify reports for a gateway wired in through a custom
    app.  It names no portal, so it must never reach a field described as
    "identifies which settlement portal the reference belongs to" — there it
    would be a placeholder wearing the shape of real data.
    """

    def test_manual_with_no_order_is_blank(self):
        self.assertEqual(extract_gateway_name({"gateway": "manual"}), "")

    def test_manual_with_an_orderless_backfill_is_blank(self):
        self.assertEqual(extract_gateway_name({"gateway": "MANUAL"}, None), "")

    def test_manual_falls_back_to_the_tags_when_the_order_is_there(self):
        order = {"tags": "CASHFREE - UPI"}
        self.assertEqual(
            extract_gateway_name({"gateway": "manual"}, order), "CASHFREE - UPI"
        )

    def test_a_real_gateway_is_still_returned(self):
        self.assertEqual(
            extract_gateway_name({"gateway": "Cards, UPI, NB by PayU India"}),
            "Cards, UPI, NB by PayU India",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
