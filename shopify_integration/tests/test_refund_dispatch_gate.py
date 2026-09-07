"""
test_refund_dispatch_gate.py — which channel and which status may pay a customer.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_refund_dispatch_gate -v

A successful refundCreate PAYS the customer — the Cashfree-OCC app bridges it
into a real Cashfree refund — so the state this gate accepts has to be the state
*before* ERPNext books the refund, not after it.

The gate replaced in CONTRACT_VERSION 4 demanded `Completed`, on the reasoning
"Shopify is told once ERPNext has booked and paid the refund, not before".  That
is write-back ordering, and it contradicts the contract's own §0.
payment_portals sets `Completed` in exactly one place — `_record_on_refund`, when
the Payment Entry is posted — so book-then-call means the customer is paid after
ERPNext recorded paying them.  Dispatch has to run at `Approved`, mirroring the
Cashfree send path in the same app.

The gate is now keyed on `refund_channel` as a positive allow-list, which closes
a hazard the old one left open: see
`test_a_bank_transfer_on_a_shopify_order_is_never_written_back`.
"""

import ast
import unittest
from pathlib import Path

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.tests.test_refund_writeback import (  # noqa: E402
    REFUND,
    WritebackTestCase,
)
from shopify_integration.utils import refund as r  # noqa: E402

HOOKS = Path(__file__).resolve().parents[1] / "hooks.py"


class TestTheDispatchGate(WritebackTestCase):
    """The accepted states, and every refusal around them."""

    def test_the_shopify_channel_dispatches_from_approved(self):
        """`Approved` is what a person sees on the form, and what a refusal
        returns the document to — payment_portals' SENDABLE_STATUSES is
        `Approved` alone."""
        self.set_field(refund_channel=r.CHANNEL_DISPATCH, status="Approved")
        result = r.write_back_refund(REFUND)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outcome"], r.OUTCOME_PAID)

    def test_the_shopify_channel_dispatches_from_queued(self):
        """`send_refund_to_portal` commits `Queued` and then enqueues the job, so
        a dispatched payout reaches here on `Queued`, never on `Approved`.
        Missing this would refuse every dispatch while the form button worked —
        and the refusal would read as a status problem, not a missing state."""
        self.set_field(refund_channel=r.CHANNEL_DISPATCH, status="Queued")
        result = r.write_back_refund(REFUND)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["outcome"], r.OUTCOME_PAID)

    def test_a_booked_refund_is_refused_by_its_own_code(self):
        """`Completed` with a Payment Entry means ERPNext already booked this
        refund.  Paying it now is paying after the record — and booking without a
        GID means something paid it outside this flow.  Either way a person has
        to look, so it gets its own reason_code rather than being folded into a
        complaint about the status."""
        self.set_field(refund_channel=r.CHANNEL_DISPATCH,
                       status="Completed", payment_entry="ACC-PAY-0001")
        result = r.write_back_refund(REFUND)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason_code"], "already_booked")
        self.assertNothingSent()
        self.assertNoGid()

    def test_a_completed_refund_with_no_payment_entry_is_the_wrong_status(self):
        """Distinct from `already_booked`, which asserts a booking exists.  This
        one must not claim one that does not."""
        self.set_field(refund_channel=r.CHANNEL_DISPATCH, status="Completed")
        result = r.write_back_refund(REFUND)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason_code"], "wrong_refund_status")
        self.assertNothingSent()

    def test_no_other_status_dispatches(self):
        """Including `Failed` and `Processing`.  `Failed` is not re-sendable in
        payment_portals either, and `Processing` means a call has already gone
        out — accepting either would be a second payout on a document that may
        already have had one."""
        for status in ("Draft", "Processing", "Failed", "Cancelled", "", None):
            self.seed()
            self.set_field(refund_channel=r.CHANNEL_DISPATCH, status=status)
            result = r.write_back_refund(REFUND)
            self.assertFalse(result["ok"], f"{status!r} dispatched")
            self.assertEqual(result["reason_code"], "wrong_refund_status", repr(status))
            self.assertNothingSent()

    def test_only_the_shopify_channel_dispatches(self):
        """A positive allow-list, for the same reason `caller_must_pay` is a
        positive flag: an unrecognised channel — including one a later
        payment_portals adds — must refuse rather than pay."""
        for channel in ("Payment Portal", "Bank Transfer", "", None, "Something New"):
            self.seed()
            self.set_field(refund_channel=channel, status="Approved")
            result = r.write_back_refund(REFUND)
            self.assertFalse(result["ok"], f"{channel!r} dispatched")
            self.assertEqual(result["reason_code"], "channel_does_not_dispatch",
                             repr(channel))
            self.assertNothingSent()
            self.assertNoGid()

    def test_a_bank_transfer_on_a_shopify_order_is_never_written_back(self):
        """The hazard the old gate left open, and the reason this is an
        allow-list rather than one more exclusion.

        `Bank Transfer` + `Completed` passed the old channel test — which only
        excluded `Manual Portal Refund` — and satisfied the old status test, so
        the form button would have written it back.  A bank refund is money
        already sent by NEFT, so the OCC bridge would pay the same customer a
        second time.  #6518's Rs 12,999 went out by NEFT and is exactly this
        shape.
        """
        self.set_field(refund_channel="Bank Transfer", status="Completed",
                       payment_entry="ACC-PAY-0002")
        result = r.write_back_refund(REFUND)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason_code"], "channel_does_not_dispatch")
        self.assertNothingSent()
        self.assertNoGid()

    def test_the_manual_portal_channel_keeps_its_own_refusal(self):
        """Distinct from `channel_does_not_dispatch`: that one means "this refund
        is paid some other way", this one means "Shopify has already paid it".
        payment_portals branches on the difference, and the two messages send a
        reader to different places."""
        self.set_field(refund_channel=r.CHANNEL_FROM_SHOPIFY, status="Approved")
        result = r.write_back_refund(REFUND)
        self.assertEqual(result["reason_code"], "channel_is_manual_portal_refund")
        self.assertNothingSent()

    def test_the_channel_is_judged_before_the_status(self):
        """A Bank Transfer refund is not dispatchable in any status, so
        complaining about the status would name the wrong problem and send
        somebody off to change a field that would not help."""
        self.set_field(refund_channel="Bank Transfer", status="Draft")
        self.assertEqual(
            r.write_back_refund(REFUND)["reason_code"], "channel_does_not_dispatch"
        )

    def test_ownership_still_precedes_both(self):
        """The biconditional is `payout_owner == caller <=> not_a_shopify_order`,
        and it holds only while ownership is settled before any guard that can
        return.  Both new refusals sit after it, so a non-Shopify order on an
        undispatchable channel must still come back as the caller's."""
        frappe_stub.DB["Sales Order"]["SO-0001"]["shopify_order_id"] = ""
        self.set_field(refund_channel="Bank Transfer", status="Completed")
        result = r.write_back_refund(REFUND)
        self.assertEqual(result["reason_code"], r.REASON_NOT_OURS)
        self.assertTrue(result["caller_must_pay"])

    def test_an_existing_gid_still_wins_over_the_gate(self):
        """Idempotency is the one guard that must hold even on a document since
        edited into a state the others reject."""
        self.set_field(refund_channel="Bank Transfer", status="Completed",
                       **{r.REFUND_GID_FIELD: "gid://shopify/Refund/1"})
        self.assertEqual(r.write_back_refund(REFUND)["reason_code"], "already_paid")

    def test_the_gate_never_raises(self):
        for case in ({"refund_channel": None, "status": None},
                     {"refund_channel": r.CHANNEL_DISPATCH, "status": None},
                     {"refund_channel": r.CHANNEL_DISPATCH, "status": "Completed",
                      "payment_entry": None}):
            self.seed()
            self.set_field(**case)
            try:
                r.write_back_refund(REFUND)
            except Exception as exc:  # noqa: BLE001 — that is the assertion
                self.fail(f"{case} raised {exc!r}")

    def test_the_two_shopify_channels_are_not_the_same_string(self):
        """Conflating them is the mistake the build prompt for this change warned
        about: one dispatches a payout, the other records one Shopify made."""
        self.assertNotEqual(r.CHANNEL_DISPATCH, r.CHANNEL_FROM_SHOPIFY)

    def test_the_gate_reports_the_refusal_on_the_document(self):
        """Every guard writes its reason where the form can show it; a silent
        refusal is a payout somebody will retry by hand."""
        self.set_field(refund_channel="Bank Transfer", status="Completed")
        r.write_back_refund(REFUND)
        self.assertEqual(self.stored(r.WRITEBACK_STATUS_FIELD), r.STATUS_SKIPPED)
        self.assertTrue(self.stored(r.WRITEBACK_ERROR_FIELD))


class TestTheGateIsPreflightedTheSameWay(WritebackTestCase):
    """`get_refund_writeback_status` is what the form and payment_portals call
    before committing.  A pre-flight that disagreed with the payout would tell a
    person the opposite of what pressing the button does."""

    def test_the_preflight_accepts_what_the_payout_accepts(self):
        self.set_field(refund_channel=r.CHANNEL_DISPATCH, status="Approved")
        info = r.get_refund_writeback_status(REFUND)
        self.assertTrue(info["can_write_back"], info)
        self.assertNothingSent()

    def test_the_preflight_refuses_what_the_payout_refuses(self):
        for fields, code in (
            ({"refund_channel": "Bank Transfer", "status": "Approved"},
             "channel_does_not_dispatch"),
            ({"refund_channel": r.CHANNEL_DISPATCH, "status": "Completed",
              "payment_entry": "ACC-PAY-0003"}, "already_booked"),
            ({"refund_channel": r.CHANNEL_DISPATCH, "status": "Failed"},
             "wrong_refund_status"),
        ):
            self.seed()
            self.set_field(**fields)
            info = r.get_refund_writeback_status(REFUND)
            self.assertFalse(info["can_write_back"], fields)
            self.assertEqual(info["reason_code"], code, fields)


class TestTheDispatcherHookIsRegistered(unittest.TestCase):
    """payment_portals refuses to pay anything while nothing is registered, and
    that refusal is indistinguishable from a broken deploy."""

    @classmethod
    def setUpClass(cls):
        cls.text = HOOKS.read_text(encoding="utf-8")

    def test_the_hook_names_write_back_refund(self):
        self.assertIn("refund_payout_dispatchers", self.text)
        self.assertIn("shopify_integration.utils.refund.write_back_refund", self.text)

    def test_exactly_one_dispatcher_is_registered(self):
        """Two dispatchers for one payout is two payouts.  The caller refuses on
        a list longer than one, so a duplicate here pays nobody rather than
        paying twice — but a deploy that pays nobody is still a bad deploy."""
        registered = None
        for node in ast.walk(ast.parse(self.text)):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if getattr(target, "id", None) == "refund_payout_dispatchers":
                        registered = ast.literal_eval(node.value)
        self.assertIsInstance(registered, list, "the hook must be a list")
        self.assertEqual(len(registered), 1, registered)

    def test_the_dispatcher_is_not_whitelisted(self):
        """Registering it does not make it an HTTP door.  `writeback_now` is the
        door and it checks submit permission; `write_back_refund` pays a
        customer and must stay unreachable over HTTP."""
        self.assertFalse(
            getattr(r.write_back_refund, "__is_whitelisted__", False),
            "write_back_refund is whitelisted — a payout is one HTTP call away",
        )


if __name__ == "__main__":
    unittest.main()
