"""
test_refund_contract.py — guards on the dispatch contract itself.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_refund_contract -v

REFUND-DISPATCH-CONTRACT.md is what the payment_portals side builds against, and
it decides whether that side thinks a customer might already have been paid.  A
document that has drifted from the code is worse than no document, because it is
believed.  So:

  * every reason_code the code can emit is listed in the contract
  * nothing is emitted from outside the closed vocabulary
  * the version in the document matches the one in the code
  * the retry_safe / possibly_paid flags agree with the outcome they came with —
    the two keys a caller is invited to trust instead of computing its own
"""

import io
import unittest
from pathlib import Path

from shopify_integration.tests import frappe_stub

frappe_stub.install()

import frappe  # noqa: E402

from shopify_integration.tests.test_refund_writeback import (  # noqa: E402
    REFUND,
    REFUND_GID,
    WritebackTestCase,
    refund_created,
    targets_response,
)
from shopify_integration.utils import refund as r  # noqa: E402
from shopify_integration.utils.shopify_api import ShopifyAPIError  # noqa: E402

CONTRACT = Path(__file__).resolve().parents[2] / "REFUND-DISPATCH-CONTRACT.md"


def contract_text() -> str:
    return io.open(CONTRACT, encoding="utf-8").read()


# ── The document and the code agree ───────────────────────────────────────────

class TestContractDocument(unittest.TestCase):
    def setUp(self):
        self.text = contract_text()

    def test_the_contract_exists_where_the_other_side_was_told_to_look(self):
        self.assertTrue(CONTRACT.is_file(), CONTRACT)

    def test_every_outcome_is_documented(self):
        for outcome in r.REASON_CODES:
            self.assertIn(f"`{outcome}`", self.text, outcome)

    def test_every_reason_code_is_documented(self):
        for outcome, codes in r.REASON_CODES.items():
            for code in codes:
                if not code:
                    continue
                self.assertIn(
                    f"`{code}`", self.text,
                    f"{code} ({outcome}) is emitted but not in the contract",
                )

    def test_the_documented_version_matches_the_code(self):
        self.assertIn(
            f"**Version {r.CONTRACT_VERSION}.**", self.text,
            f"the code says contract version {r.CONTRACT_VERSION}",
        )

    def test_the_contract_names_the_hook_functions_it_promises(self):
        for name in ("write_back_refund", "get_refund_writeback_status",
                     "resolve_unverified_writeback", "refund_payout_dispatchers"):
            self.assertIn(name, self.text, name)

    def test_the_contract_says_the_call_is_synchronous(self):
        """An enqueued payout cannot be reported as sent, and that reasoning is
        the whole answer to the question — it has to be in the document."""
        self.assertIn("synchronous", self.text.lower())

    def test_the_unimplemented_parts_are_marked_as_such(self):
        """The other side is building against this; a promised function that does
        not exist has to say so, not be discovered."""
        self.assertIn("not implemented yet", self.text.lower())


# ── Nothing escapes the vocabulary ────────────────────────────────────────────

class TestOutcomeVocabulary(WritebackTestCase):
    """Sweeps every reachable exit of write_back_refund and checks the result
    against the closed vocabulary."""

    def assertContractual(self, result, label=""):
        outcome = result.get("outcome")
        self.assertIn(outcome, r.REASON_CODES, f"{label}: unknown outcome {outcome!r}")
        self.assertIn(
            result.get("reason_code", ""), r.REASON_CODES[outcome],
            f"{label}: {result.get('reason_code')!r} is not in the vocabulary for "
            f"{outcome}",
        )
        # The two derived flags a caller is told to trust rather than compute.
        self.assertIs(
            result["retry_safe"], outcome == r.OUTCOME_FAILED_UNSENT, label
        )
        self.assertIs(
            result["possibly_paid"],
            outcome in (r.OUTCOME_PAID, r.OUTCOME_FAILED_UNKNOWN),
            label,
        )
        self.assertEqual(result["provider"], "shopify", label)
        self.assertEqual(result["contract_version"], r.CONTRACT_VERSION, label)
        self.assertTrue(result["message"], f"{label}: every outcome must say why")
        # A GID is returned only where the contract says one exists.
        if result.get("refund_gid"):
            self.assertIn(
                outcome, (r.OUTCOME_PAID, r.OUTCOME_REFUSED),
                f"{label}: a refund id came back with outcome {outcome}",
            )

    def test_success(self):
        self.assertContractual(r.write_back_refund(REFUND), "paid")

    def test_every_guard(self):
        cases = {
            "already_paid": {r.REFUND_GID_FIELD: REFUND_GID},
            "channel_is_manual_portal_refund": {"refund_channel": r.CHANNEL_FROM_SHOPIFY},
            "wrong_refund_status": {"status": "Approved"},
            "not_submitted": {"docstatus": 0},
            "nothing_to_refund": {"net_refund_amount": 0},
            "not_a_shopify_order": {"sales_order": ""},
            "unverified_previous_attempt": {
                r.WRITEBACK_STATUS_FIELD: r.STATUS_UNVERIFIED
            },
        }
        for expected_code, fields in cases.items():
            self.seed()
            self.set_field(**fields)
            result = r.write_back_refund(REFUND)
            self.assertContractual(result, expected_code)
            self.assertEqual(result["reason_code"], expected_code)

    def test_store_and_installation_guards(self):
        self.seed()
        frappe_stub.DB["Shopify Settings"]["Test Store"]["enable_refund_writeback"] = 0
        self.assertContractual(
            r.write_back_refund(REFUND), "writeback_unavailable_for_store"
        )

        self.seed()
        r.has_admin_api_credentials = lambda settings: False
        self.assertContractual(r.write_back_refund(REFUND), "no_api_credentials")

        self.seed()
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set()
        self.assertContractual(r.write_back_refund(REFUND), "not_installed")

    def test_no_permission(self):
        real = frappe.has_permission
        frappe.has_permission = lambda *a, **k: False
        try:
            self.assertContractual(r.write_back_refund(REFUND), "no_permission")
        finally:
            frappe.has_permission = real

    def test_in_progress(self):
        self.set_field(**{
            r.WRITEBACK_STATUS_FIELD: r.STATUS_PENDING,
            r.WRITEBACK_AT_FIELD: frappe.utils.now_datetime(),
        })
        result = r.write_back_refund(REFUND)
        self.assertContractual(result, "claimed_elsewhere")
        self.assertEqual(result["outcome"], r.OUTCOME_IN_PROGRESS)

    def test_every_unsent_failure(self):
        cases = {
            "query_failed": [ShopifyAPIError("boom")],
            "shopify_order_not_found": [targets_response(order=False)],
            "not_authorised": [targets_response(), ShopifyAPIError("nope", 403)],
            "rejected_by_shopify": [
                targets_response(),
                refund_created(user_errors=[{"field": None, "message": "no"}]),
            ],
        }
        for expected_code, responses in cases.items():
            self.seed()
            self.responses = list(responses)
            result = r.write_back_refund(REFUND)
            self.assertContractual(result, expected_code)
            self.assertEqual(result["reason_code"], expected_code)
            self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNSENT)

    def test_every_unknown_failure(self):
        cases = {
            "transport_error_after_send": [
                targets_response(), ShopifyAPIError("connection reset")
            ],
            "response_unverifiable": [targets_response(), refund_created(refund=False)],
        }
        for expected_code, responses in cases.items():
            self.seed()
            self.responses = list(responses)
            result = r.write_back_refund(REFUND)
            self.assertContractual(result, expected_code)
            self.assertEqual(result["reason_code"], expected_code)
            self.assertEqual(result["outcome"], r.OUTCOME_FAILED_UNKNOWN)
            self.assertTrue(result["possibly_paid"])
            self.assertFalse(result["retry_safe"])

    def test_only_one_outcome_is_ever_retry_safe(self):
        """The contract's central promise, asserted as a property rather than
        case by case."""
        self.assertEqual(r._RETRY_SAFE_OUTCOMES, (r.OUTCOME_FAILED_UNSENT,))

    def test_the_amount_is_reported_back_on_every_outcome_that_knows_it(self):
        """payment_portals reconciles on this figure, so a silent 0.0 where a
        real amount was in play would be a reconciliation bug."""
        result = r.write_back_refund(REFUND)
        self.assertEqual(result["amount"], 12999.0)

        self.seed()
        self.responses = [targets_response(), ShopifyAPIError("connection reset")]
        self.assertEqual(r.write_back_refund(REFUND)["amount"], 12999.0)


if __name__ == "__main__":
    unittest.main()
