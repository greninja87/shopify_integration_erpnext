"""`Unverified` must have an exit on the form, and the exit must not be a payout.

`Unverified` means `refundCreate` went out and this app never read the answer, so
the customer may already have been paid. Nothing automatic touches it, on
purpose: `check_eligibility` refuses it, `_claim` refuses it, `shopify_refund_button`
refuses it by name, and payment_portals' send path refuses it too. A retry there
is a second real payout.

So the only way out is a person reading the Shopify order and recording what is
on it, which `resolve_unverified_writeback` exists to store. That endpoint has
been whitelisted since the write-back was built and **nothing called it**. The
headline this state renders told the reader to "record what you find against this
write-back" and offered them nothing to do it with.

That became worse, not better, when the other app closed its own hole:
`payment_portals`' `cancellation_allowed` now reads the write-back state as well
as the refund gid -- it has to, because `Unverified` carries no gid by
construction, the state being committed in the instant *before* the mutation is
posted -- so cancelling the document is refused as well. Two refusals, an
instruction, and no control: that is how a stuck payout becomes a forgotten one.

What this file holds:

* the control exists and is offered in this state and only this state;
* it calls the endpoint that records a finding, and **not** `writeback_now`,
  which pays;
* it makes the person state which fact they found, with no default;
* it collects the refund gid when they say "paid", because the server refuses
  without one -- that gid is what the credit-note guard matches on, and a `Done`
  row without it lets the refunds/create webhook raise a second Credit Note;
* it reports a refusal, because the endpoint refuses by *returning* `ok: false`
  rather than by throwing, and a caller that only reloaded would show a form
  looking resolved when nothing had been written.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

FORM = Path(__file__).resolve().parents[1] / "public" / "js" / "refund_request.js"

#: The control, and the state it belongs to.
BUTTON = "shopify_resolve_unverified_button"
UNVERIFIED = "'Unverified'"

#: The endpoint that records a finding, and the one that pays. The whole point of
#: the control is that it calls the first and never the second.
RECORDS = "shopify_integration.utils.refund.resolve_unverified_writeback"
PAYS = "shopify_integration.utils.refund.write_back_refund"
PAYS_ENDPOINT = "shopify_integration.utils.refund.writeback_now"

#: The two findings the endpoint accepts, spelled as it spells them.
FINDINGS = ("'paid'", "'not_paid'")


def source() -> str:
    return FORM.read_text(encoding="utf-8")


def _body(name: str) -> str:
    """One top-level function's body out of the form script."""
    text = source()
    match = re.search(rf"\nfunction {name}\(frm(?:, info)?\) \{{(.*?)\n\}}", text, re.S)
    assert match, f"{name} not found in refund_request.js"
    return match.group(1)


class TestTheControlExists(unittest.TestCase):
    def test_the_button_is_defined(self):
        self.assertIn(f"function {BUTTON}(frm, info)", source())

    def test_the_refresh_handler_offers_it(self):
        """Defined and never called is the state this file was written about."""
        text = source()
        handler = re.search(r"refresh: function\(frm\) \{(.*?)\n    \}", text, re.S)
        self.assertTrue(handler, "the refresh handler was not found")
        self.assertIn(f"{BUTTON}(frm, info)", handler.group(1))

    def test_it_is_offered_in_this_state_and_only_this_state(self):
        """Guarded on the status, first, and by name.

        Offering it anywhere else would invite somebody to record a finding about
        an attempt that was never made.
        """
        body = _body(BUTTON)
        guard = f"if (info.status !== {UNVERIFIED}) return;"
        self.assertIn(guard, body)
        self.assertLess(
            body.index(guard),
            body.index("add_custom_button"),
            "the state is checked after the button is added, so it is added anyway",
        )


class TestItRecordsRatherThanPays(unittest.TestCase):
    def test_it_calls_the_endpoint_that_records_a_finding(self):
        self.assertIn(RECORDS, _body(BUTTON))

    def test_it_calls_neither_endpoint_that_pays(self):
        """The one mistake here that spends money.

        Both are named: `writeback_now` is the whitelisted door and
        `write_back_refund` the function behind it, and a refactor that reached
        for either from this dialog would turn a bookkeeping control into a second
        payout on the one document where a second payout is most likely.
        """
        body = _body(BUTTON)
        for endpoint in (PAYS, PAYS_ENDPOINT):
            self.assertNotIn(endpoint, body, f"{BUTTON} calls {endpoint}, which pays")

    def test_it_says_in_the_dialog_that_it_pays_nobody(self):
        """Because the reader arrives here from a headline about a possible payout."""
        body = _body(BUTTON)
        self.assertIn("pays nobody", body)
        self.assertIn("sends nothing to Shopify", body)


class TestThePersonStatesTheFact(unittest.TestCase):
    def test_both_findings_are_offered(self):
        body = _body(BUTTON)
        for finding in FINDINGS:
            self.assertIn(finding, body)

    def test_neither_finding_is_preselected(self):
        """A prefilled answer is one careless Enter from a finding nobody made."""
        body = _body(BUTTON)
        self.assertNotRegex(
            body,
            r"fieldname: 'resolution'[^}]*default:",
            "the resolution field carries a default",
        )
        self.assertRegex(
            body,
            r"\{ value: '', label: __\('Choose",
            "the resolution field has no blank first option, so one finding is "
            "preselected by the browser",
        )

    def test_nothing_is_sent_when_no_finding_was_chosen(self):
        self.assertIn("if (!values.resolution) return;", _body(BUTTON))

    def test_the_refund_id_is_collected_when_the_answer_is_paid(self):
        """The server refuses `paid` without it, and for a reason worth repeating.

        That gid is what `refund_request_for_shopify_refund` matches on, so a
        `Done` row without one lets our own refunds/create webhook raise a second
        Credit Note for a refund ERPNext already has.
        """
        body = _body(BUTTON)
        self.assertIn("fieldname: 'shopify_refund_gid'", body)
        self.assertIn("mandatory_depends_on: 'eval:doc.resolution==\"paid\"'", body)
        self.assertIn("Credit Note", body, "the field does not say why it is required")


class TestARefusalIsReported(unittest.TestCase):
    def test_a_returned_refusal_is_shown_and_does_not_reload(self):
        """`resolve_unverified_writeback` refuses by returning, not by throwing.

        An unparseable gid and a state that has moved since the form loaded both
        come back as `{ok: false, message}` with HTTP 200. A caller that reloaded
        regardless would show a document that looks resolved and is not -- on the
        state whose whole problem is not knowing.
        """
        body = _body(BUTTON)
        self.assertIn("if (!answer || !answer.ok)", body)
        refusal = body.index("if (!answer || !answer.ok)")
        reload_at = body.index("frm.reload_doc()")
        self.assertLess(refusal, reload_at, "the refusal is checked after the reload")
        self.assertIn("Nothing Was Recorded", body)

    def test_a_thrown_error_is_reported_too(self):
        """A permission refusal or a lost connection, and neither wrote anything."""
        body = _body(BUTTON)
        self.assertIn(".catch(", body)
        self.assertEqual(
            body.count("Nothing Was Recorded"),
            2,
            "both the returned refusal and the thrown one must say nothing was "
            "recorded; a dialog that closes silently on a money decision reads "
            "as success",
        )


if __name__ == "__main__":
    unittest.main()
