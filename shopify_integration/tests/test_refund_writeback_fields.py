"""
test_refund_writeback_fields.py — structural guards on the Refund Request
custom fields this app installs.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_refund_writeback_fields -v

Refund Request belongs to payment_portals; these are Custom Fields, so they
live in their own table and a `bench migrate` there cannot undo them.

The no_copy tests mirror payment_portals' own
test_the_portal_s_own_answer_never_carries_over and
test_the_bank_utr_never_carries_over, on the same grounds: Refund Request is
amendable, and an amended refund is a *new* refund that has not been written to
Shopify.  A shopify_refund_gid inherited from the original would make the
idempotency guard skip a write-back that never happened — and skip it silently,
which is the worst direction for this failure to go.  Those tests guard fields
payment_portals owns; these guard ours, next to the code that defines them.
"""

import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

import frappe  # noqa: E402

from shopify_integration import install as inst  # noqa: E402
from shopify_integration.utils import refund as r  # noqa: E402

# The fields that hold a value, as opposed to the section and column breaks.
VALUE_FIELDS = (
    r.REFUND_GID_FIELD,
    r.WRITEBACK_STATUS_FIELD,
    r.REFUND_GATEWAY_FIELD,
    r.WRITEBACK_ERROR_FIELD,
    r.WRITEBACK_AT_FIELD,
)


def created_fields(doctype_exists=True, refund_request_fields=("amended_from",)):
    """Run the creator with the helper stubbed out, and return what it asked for
    as {fieldname: field_def}, plus the doctypes it touched."""
    calls = []
    real_helper = inst.create_or_update_custom_field
    real_exists = frappe.db.exists
    real_get_meta = frappe.get_meta

    frappe_stub.META_FIELDS["Refund Request"] = set(refund_request_fields)
    frappe.get_meta = real_get_meta
    frappe.db.exists = lambda *a, **k: doctype_exists
    inst.create_or_update_custom_field = lambda dt, field_def: calls.append((dt, field_def))
    try:
        inst.create_refund_request_writeback_custom_fields()
    finally:
        inst.create_or_update_custom_field = real_helper
        frappe.db.exists = real_exists

    return calls


class TestRefundWritebackFields(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()
        self.calls = created_fields()
        self.fields = {f["fieldname"]: f for f in (d for _, d in self.calls)}

    # ── The fields exist, on the right doctype ────────────────────────────────

    def test_every_field_lands_on_refund_request(self):
        self.assertTrue(self.calls)
        for doctype, field_def in self.calls:
            self.assertEqual(doctype, "Refund Request", field_def["fieldname"])

    def test_all_five_state_fields_are_created(self):
        for fieldname in VALUE_FIELDS:
            self.assertIn(fieldname, self.fields)

    def test_the_fields_are_wrapped_in_a_collapsible_section(self):
        """Five read-only machine fields appended bare to somebody else's form is
        noise; the rest of this app puts them in a collapsed section."""
        section = self.fields["shopify_refund_writeback_section"]
        self.assertEqual(section["fieldtype"], "Section Break")
        self.assertEqual(section["collapsible"], 1)

    def test_the_section_anchors_on_a_field_refund_request_actually_has(self):
        self.assertEqual(
            self.fields["shopify_refund_writeback_section"]["insert_after"],
            "amended_from",
        )

    def test_a_missing_anchor_falls_back_rather_than_pointing_at_nothing(self):
        calls = created_fields(refund_request_fields=("status",))
        fields = {f["fieldname"]: f for f in (d for _, d in calls)}
        self.assertEqual(
            fields["shopify_refund_writeback_section"]["insert_after"], "status"
        )

    # ── Nothing carries over onto an amendment ────────────────────────────────

    def test_the_shopify_evidence_never_carries_over(self):
        """The mirror of payment_portals' own no_copy guards.  A carried-over GID
        makes the §6 idempotency guard skip a refund that was never written."""
        for fieldname in VALUE_FIELDS:
            self.assertEqual(
                self.fields[fieldname].get("no_copy"), 1,
                f"{fieldname} would be copied onto an amendment",
            )

    # ── Written after submit, by machine ──────────────────────────────────────

    def test_every_field_is_writable_after_submit(self):
        """All five are written once the Refund Request is submitted; without
        allow_on_submit the write-back cannot record its own outcome."""
        for fieldname in VALUE_FIELDS:
            self.assertEqual(self.fields[fieldname].get("allow_on_submit"), 1, fieldname)

    def test_every_field_is_read_only_to_people(self):
        for fieldname in VALUE_FIELDS:
            self.assertEqual(self.fields[fieldname].get("read_only"), 1, fieldname)

    # ── Shapes ────────────────────────────────────────────────────────────────

    def test_the_status_field_offers_exactly_the_four_states_and_blank(self):
        options = self.fields[r.WRITEBACK_STATUS_FIELD]["options"]
        self.assertEqual(
            options.split("\n"), ["", "Pending", "Done", "Failed", "Skipped"]
        )

    def test_the_status_options_match_the_constants_the_code_writes(self):
        options = set(self.fields[r.WRITEBACK_STATUS_FIELD]["options"].split("\n"))
        for status in (r.STATUS_PENDING, r.STATUS_DONE, r.STATUS_FAILED, r.STATUS_SKIPPED):
            self.assertIn(status, options)

    def test_field_types(self):
        self.assertEqual(self.fields[r.REFUND_GID_FIELD]["fieldtype"], "Data")
        self.assertEqual(self.fields[r.REFUND_GATEWAY_FIELD]["fieldtype"], "Data")
        self.assertEqual(self.fields[r.WRITEBACK_ERROR_FIELD]["fieldtype"], "Small Text")
        self.assertEqual(self.fields[r.WRITEBACK_AT_FIELD]["fieldtype"], "Datetime")

    def test_the_gateway_field_says_in_plain_words_what_manual_means(self):
        """"manual" is the normal value on these orders *and* the customer gets
        paid, via the Cashfree-OCC app.  The description has to say that, because
        the word invites the opposite reading — which it got, twice."""
        description = self.fields[r.REFUND_GATEWAY_FIELD].get("description", "").lower()
        self.assertIn("manual", description)
        self.assertIn("parent transaction", description)

    def test_the_gateway_field_never_claims_that_manual_means_no_payout(self):
        """The one sentence that must not appear here.  An earlier draft of the
        brief said this write-back could not move money; production Gateway
        Transaction data disproved it."""
        description = self.fields[r.REFUND_GATEWAY_FIELD].get("description", "").lower()
        for claim in ("nothing was charged", "no money moved", "does not move money"):
            self.assertNotIn(claim, description)

    # ── Inert without payment_portals ─────────────────────────────────────────

    def test_nothing_is_created_when_refund_request_does_not_exist(self):
        """shopify_integration must stay installable on a site with no
        payment_portals."""
        self.assertEqual(created_fields(doctype_exists=False), [])


if __name__ == "__main__":
    unittest.main()
