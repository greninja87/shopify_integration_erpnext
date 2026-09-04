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
    ORDER_ID,
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


# ── The routing invariant ─────────────────────────────────────────────────────

# Every reason_code, and who owes the customer the money when it is returned.
# Enumerated rather than derived, so adding a code without deciding its side
# fails the test below instead of quietly defaulting.
#
# The rule the contract states, and the one payment_portals branches on:
#
#     payout_owner == OWNER_CALLER   <=>   reason_code == REASON_NOT_OURS
#
# It is a biconditional, and it has already been got wrong once in the direction
# that pays a customer twice: channel_is_manual_portal_refund — a refund Shopify
# has ALREADY made — reported as the caller's to pay, because the channel guard
# returned before the Sales Order was ever looked up.
EXPECTED_OWNER = {
    # Shopify's payout. Several of these are cases where it cannot perform it
    # right now; that does NOT make it somebody else's.
    "already_paid": r.OWNER_SHOPIFY,
    "channel_is_manual_portal_refund": r.OWNER_SHOPIFY,
    "wrong_refund_status": r.OWNER_SHOPIFY,
    "not_submitted": r.OWNER_SHOPIFY,
    "nothing_to_refund": r.OWNER_SHOPIFY,
    "writeback_unavailable_for_store": r.OWNER_SHOPIFY,
    "no_api_credentials": r.OWNER_SHOPIFY,
    "amount_mismatch": r.OWNER_SHOPIFY,
    "query_failed": r.OWNER_SHOPIFY,
    "shopify_order_not_found": r.OWNER_SHOPIFY,
    "insufficient_refundable": r.OWNER_SHOPIFY,
    "no_refundable_transactions": r.OWNER_SHOPIFY,
    "rejected_by_shopify": r.OWNER_SHOPIFY,
    "not_authorised": r.OWNER_SHOPIFY,
    "setup_failed": r.OWNER_SHOPIFY,
    "transport_error_after_send": r.OWNER_SHOPIFY,
    "response_unverifiable": r.OWNER_SHOPIFY,
    "unverified_previous_attempt": r.OWNER_SHOPIFY,
    "claimed_elsewhere": r.OWNER_SHOPIFY,

    # The caller's payout. Exactly one code, by construction.
    "not_a_shopify_order": r.OWNER_CALLER,

    # Undeterminable: we refused before we could read what deciding it needs.
    "not_installed": r.OWNER_UNKNOWN,
    "refund_request_missing": r.OWNER_UNKNOWN,
    "no_permission": r.OWNER_UNKNOWN,

    # Success carries no code.
    "": r.OWNER_SHOPIFY,
}


class TestRoutingInvariant(unittest.TestCase):
    """Pins the biconditional over the whole vocabulary, statically."""

    def test_every_reason_code_has_a_declared_owner(self):
        """A new code without a decided side is the drift this catches."""
        for outcome, codes in r.REASON_CODES.items():
            for code in codes:
                self.assertIn(
                    code, EXPECTED_OWNER,
                    f"{code} ({outcome}) has no declared payout owner",
                )

    def test_exactly_one_code_puts_the_payout_on_the_caller(self):
        callers = [c for c, o in EXPECTED_OWNER.items() if o == r.OWNER_CALLER]
        self.assertEqual(callers, [r.REASON_NOT_OURS])

    def test_the_biconditional_holds_over_the_whole_vocabulary(self):
        for code, owner in EXPECTED_OWNER.items():
            self.assertEqual(
                owner == r.OWNER_CALLER, code == r.REASON_NOT_OURS,
                f"{code}: owner {owner} breaks "
                f"payout_owner == caller <=> reason_code == {r.REASON_NOT_OURS}",
            )

    def test_caller_must_pay_is_true_for_exactly_the_caller_owner(self):
        for owner in (r.OWNER_SHOPIFY, r.OWNER_CALLER, r.OWNER_UNKNOWN):
            flags = r._ownership({"payout_owner": owner})
            self.assertIs(flags["caller_must_pay"], owner == r.OWNER_CALLER, owner)

    def test_owns_payout_is_none_when_undeterminable_not_false(self):
        """False would invite the caller to pay it; None cannot be mistaken for
        a determination."""
        self.assertIsNone(r._ownership({"payout_owner": r.OWNER_UNKNOWN})["owns_payout"])
        self.assertIs(r._ownership({"payout_owner": r.OWNER_SHOPIFY})["owns_payout"], True)
        self.assertIs(r._ownership({"payout_owner": r.OWNER_CALLER})["owns_payout"], False)

    def test_a_missing_owner_degrades_to_unknown(self):
        self.assertEqual(r._ownership({})["payout_owner"], r.OWNER_UNKNOWN)
        self.assertFalse(r._ownership({})["caller_must_pay"])


class TestRoutingLive(WritebackTestCase):
    """The same invariant, against real write_back_refund results — the static
    table above cannot catch a code whose runtime owner disagrees with it."""

    def assertRouting(self, result, label):
        code = result["reason_code"]
        self.assertIn(code, EXPECTED_OWNER, label)
        self.assertEqual(result["payout_owner"], EXPECTED_OWNER[code], label)
        self.assertIs(
            result["caller_must_pay"], code == r.REASON_NOT_OURS, label
        )

    def test_the_three_live_cases_from_the_deploy(self):
        """REF-00218-2, REF-00214 and REF-00219 on electrobotictest all came back
        with the same flag and opposite meanings.  This is that report, as a
        test: two Shopify-backed refunds (one a Manual Portal Refund Shopify had
        already paid) and one genuinely not ours."""
        # REF-00214 / REF-00219 — Shopify order, refunded in Shopify already.
        self.set_field(refund_channel=r.CHANNEL_FROM_SHOPIFY)
        booked = r.write_back_refund(REFUND)
        self.assertEqual(booked["reason_code"], "channel_is_manual_portal_refund")
        self.assertEqual(booked["payout_owner"], r.OWNER_SHOPIFY)
        self.assertFalse(
            booked["caller_must_pay"],
            "this refund was already paid by Shopify; paying it again is the "
            "double payout the contract exists to prevent",
        )

        # REF-00218-2 — no Shopify order at all.
        self.seed()
        frappe_stub.DB["Sales Order"]["SO-0001"]["shopify_order_id"] = ""
        not_ours = r.write_back_refund(REFUND)
        self.assertEqual(not_ours["reason_code"], r.REASON_NOT_OURS)
        self.assertEqual(not_ours["payout_owner"], r.OWNER_CALLER)
        self.assertTrue(not_ours["caller_must_pay"])

    def test_is_shopify_is_true_for_a_shopify_order_whatever_the_guard(self):
        """It read false for NG-SO2627-1022 and NG-SO2627-2160, which are both
        Shopify orders — the channel guard short-circuited the lookup."""
        self.set_field(refund_channel=r.CHANNEL_FROM_SHOPIFY)
        info = r.get_refund_writeback_status(REFUND)

        self.assertTrue(info["is_shopify"])
        self.assertEqual(info["shopify_order_id"], ORDER_ID)
        self.assertEqual(info["payout_owner"], r.OWNER_SHOPIFY)
        self.assertFalse(info["caller_must_pay"])
        self.assertFalse(info["can_write_back"])

    def test_the_order_id_is_populated_on_every_guard_that_can_see_it(self):
        for fields in ({"refund_channel": r.CHANNEL_FROM_SHOPIFY},
                       {"status": "Approved"},
                       {"docstatus": 0},
                       {"net_refund_amount": 0},
                       {r.WRITEBACK_STATUS_FIELD: r.STATUS_UNVERIFIED}):
            self.seed()
            self.set_field(**fields)
            info = r.get_refund_writeback_status(REFUND)
            self.assertEqual(info["shopify_order_id"], ORDER_ID, fields)
            self.assertEqual(info["shopify_store"], "notdrones.myshopify.com", fields)
            self.assertEqual(info["payout_owner"], r.OWNER_SHOPIFY, fields)

    def test_an_unmigrated_site_reports_unknown_not_caller(self):
        frappe_stub.META_FIELDS[r.REFUND_REQUEST] = set()
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["reason_code"], "not_installed")
        self.assertEqual(result["payout_owner"], r.OWNER_UNKNOWN)
        self.assertFalse(result["caller_must_pay"])
        self.assertIsNone(result["owns_payout"])

    def test_a_missing_document_reports_unknown_not_caller(self):
        result = r.write_back_refund("REF-NOPE")
        self.assertEqual(result["reason_code"], "refund_request_missing")
        self.assertEqual(result["payout_owner"], r.OWNER_UNKNOWN)
        self.assertFalse(result["caller_must_pay"])

    def test_no_permission_reports_unknown_not_caller(self):
        real = frappe.has_permission
        frappe.has_permission = lambda *a, **k: False
        try:
            result = r.write_back_refund(REFUND)
        finally:
            frappe.has_permission = real
        self.assertEqual(result["payout_owner"], r.OWNER_UNKNOWN)
        self.assertFalse(result["caller_must_pay"])

    def test_a_gid_settles_ownership_even_if_the_order_link_is_gone(self):
        """An amended Sales Order can lose its shopify_order_id; a refund Shopify
        demonstrably paid must not become the caller's."""
        self.set_field(**{r.REFUND_GID_FIELD: REFUND_GID})
        frappe_stub.DB["Sales Order"]["SO-0001"]["shopify_order_id"] = ""
        result = r.write_back_refund(REFUND)

        self.assertEqual(result["reason_code"], "already_paid")
        self.assertEqual(result["payout_owner"], r.OWNER_SHOPIFY)
        self.assertFalse(result["caller_must_pay"])

    def test_success_is_shopify_owned(self):
        self.assertRouting(r.write_back_refund(REFUND), "paid")

    def test_routing_on_every_guard(self):
        cases = (
            {"refund_channel": r.CHANNEL_FROM_SHOPIFY},
            {"status": "Approved"},
            {"docstatus": 0},
            {"net_refund_amount": 0},
            {"sales_order": ""},
            {r.REFUND_GID_FIELD: REFUND_GID},
            {r.WRITEBACK_STATUS_FIELD: r.STATUS_UNVERIFIED},
        )
        for fields in cases:
            self.seed()
            self.set_field(**fields)
            self.assertRouting(r.write_back_refund(REFUND), str(fields))


if __name__ == "__main__":
    unittest.main()
