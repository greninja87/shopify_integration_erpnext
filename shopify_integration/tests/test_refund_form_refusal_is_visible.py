"""
test_refund_form_refusal_is_visible.py — the form says why it will not refund.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_refund_form_refusal_is_visible -v

`get_refund_writeback_status` has always returned `reason` and `reason_code`
next to `can_write_back`.  `public/js/refund_request.js` used `can_write_back` to
decide whether to draw the button and threw the reason away; its only message
came off the stored `shopify_writeback_error`, which is empty on a refund that
was never sent.  That was survivable while payment_portals still offered **Send
Refund to Portal** on this channel — a refused write-back left the other button
and its own refusal text.  It stopped being survivable with payment_portals'
`sent_by_this_app`, which withholds that button on the Shopify channel: the
Shopify one is now the only route, so a state that withholds it withholds
everything, and the form said nothing whatsoever.

Nothing else moves such a refund on, either.  The 15-minute sweep skips the
Shopify channel by name (`settled_elsewhere`) because there is no `cf_refund_id`
to ask a gateway about, so an unexplained refusal here is a refund that sits
where it is until somebody reads the code.

The two states, both from the live shape of the data rather than invented:

  * Ineligible for a setup reason — Refund Write-Back off for the store, or no
    Admin API token.  An orange "Shopify: not written back" indicator, no
    button, no explanation, while payment_portals' Approved intro for the
    channel sent the reader to this app for the payout.  Two settings make the
    difference and neither is legible from the form:
    `enable_refund_writeback` on the store's Shopify Settings, refused as
    `writeback_unavailable_for_store`, and the Admin API credentials, refused
    as `no_api_credentials`.  Whichever of them stands, this message is the
    only account a reader gets.  (This paragraph used to add that the toggle
    was clear across the estate and had to stay that way, so **Refund in
    Shopify** was on no form anywhere — a live setting copied into source,
    which then moved.  See TestNoFileHardCodesTheLiveToggleValue at the foot of
    this file.)
  * `is_shopify` false on a Shopify-channel refund.  It is
    `payout_owner == OWNER_SHOPIFY`, true only once a GID or the Sales Order's
    `shopify_order_id` exists; let the order link stop reading — no Sales Order
    on the refund, the order deleted, or an order whose `shopify_order_id` was
    cleared, which `sales_order.clear_shopify_fields_on_amend` does to a manual
    duplicate — and the refresh handler returned early and rendered nothing at
    all.  ("Amend the Sales Order and it loses the id" is written into comments
    and docstrings in both repos, including this file until now.  That hook
    returns early on `amended_from` precisely so an amended copy keeps the
    Shopify fields, and the cancelled original keeps its own.  The state is
    real, the named mechanism is not.)

A third state, and the one this round adds: `is_shopify` **true** on a refund
travelling by some other channel.  It is true of every refund raised against a
Shopify-backed order, whatever channel pays it, so both of this file's advisory
renders were appearing on Bank Transfer, Manual Portal Refund, Payment Portal
and blank-channel refunds — see TestTheAdvisoryRendersAreGatedOnTheChannel.

The form script has no JS harness in this app and no bench here to run one in,
so these tests read the source and pin the decisions that matter: which
refusals are spoken, that neither silent path can go silent again, which
renders are gated on the channel and which are facts that are not, and that no
advisory path pays for its sentence with payment_portals' money headline.  Same
approach
as payment_portals' tests/test_a_shopify_refund_has_one_send_button.py, which
pins the other half of this change.

That third decision is not decoration.  payment_portals' `render_summary` sets
the headline to this refund's net-refund figures on every submitted document,
synchronously, from its own `refresh`.  Everything in this file arrives inside
an `xcall().then()`, so a `set_headline_alert` here lands last and always wins —
and on an unmigrated site that meant EVERY submitted Shopify-channel refund lost
its figures in favour of a `bench migrate` instruction aimed at a sysadmin.  The
advisory paths therefore use `frm.dashboard.add_comment`, which is what
payment_portals itself uses for a note of this kind (`note_payouts_off`).  The
states that describe money that has or may have moved — `Unverified`, a stored
GID, a stored write-back error — keep the headline on purpose and are pinned
that way below.

Both positional pins here exist because of a demonstrated hole: the first
version of `TestTheEligibilityRefusalIsRendered` asserted only that certain
strings occurred *somewhere* in `shopify_refund_message`, so moving the whole
eligibility block below `if (!messages.length) return;` — which restores the
original defect completely, the block being unreachable on the one condition it
exists for — left the suite green.  Ordering is the property; assert the
ordering.
"""

import inspect
import re
import unittest
from pathlib import Path

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.utils import refund as r  # noqa: E402

FORM = Path(__file__).resolve().parents[1] / "public" / "js" / "refund_request.js"

#: Refusals a person must be shown.  Each is setup, nothing else on the form
#: says it, and there is no button at all while it stands.
#:
#: `not_installed` used to be here and is deliberately gone — see MUST_NOT_SPEAK
#: and TestTheUnmigratedStateIsHandledElsewhere for where that state is really
#: answered.
MUST_SPEAK = (
    "writeback_unavailable_for_store",
    "no_api_credentials",
)

#: The whole allow-list, exactly.  `MUST_SPEAK` above is a floor and
#: `MUST_NOT_SPEAK` below is a ceiling on seven named codes, and between them
#: they left `nothing_to_refund` unheld in both directions: deleting it kept the
#: suite green while restoring the silent refusal for every refund whose net
#: figure is <= 0 — the form shows 0.00, no button and no word about the
#: connection between them.  Equality is the property, so assert equality.
SPOKEN_EXACTLY = MUST_SPEAK + ("nothing_to_refund",)

#: Refusals that must stay silent, and why.
#:
#: The two channel codes are the module's contract: this file renders nothing on
#: a refund that is not travelling by the Shopify channel, and both of them mean
#: precisely that — one because Shopify has already paid it, one because another
#: route pays it and that channel's own intro on the form says so.  (Not because
#: payment_portals still offers **Send Refund to Portal** there: the archetype
#: `channel_does_not_dispatch` names is Bank Transfer, and payment_portals'
#: `is_portal_refund` excludes Bank Transfer, so there is no such button on that
#: channel either.  The conclusion holds; the old reason for it did not.)
#: `already_paid` is said better by the GID banner, in the GID.  The next two
#: come back with `is_shopify` false, so they belong to the dead-end path and
#: never reach the eligibility message.
#:
#: `not_installed` is the same shape as those two and was on the spoken list by
#: mistake: `check_eligibility` returns it with `payout_owner` still
#: `OWNER_UNKNOWN`, so `is_shopify` is false and the answer goes to the dead-end
#: path.  It is unreachable here today AND in the hypothetical its old comment
#: invoked — `get_refund_writeback_status`'s early return going away changes
#: nothing, because `check_eligibility` still answers `OWNER_UNKNOWN` on an
#: unmigrated site.
MUST_NOT_SPEAK = (
    "already_paid",
    "channel_is_manual_portal_refund",
    "channel_does_not_dispatch",
    "not_a_shopify_order",
    "refund_request_missing",
    "unverified_previous_attempt",
    "not_installed",
)


def source() -> str:
    return FORM.read_text(encoding="utf-8")


def without_comments(js: str) -> str:
    """`js` with whole-line `//` comments removed.

    Every assertion below about what the script *does* runs on this, so that a
    comment naming `set_headline_alert` — and the comments here have to name it,
    because the split between headline and dashboard comment is the decision
    being recorded — cannot satisfy or break a claim about the code.  Only
    whole-line comments go: nothing in this file trails code with `//`.
    """
    return re.sub(r"^[ \t]*//.*$", "", js, flags=re.MULTILINE)


def module_header() -> str:
    """The file's opening comment block: everything above the first blank line."""
    return source().split("\n\n", 1)[0]


def header_prose() -> str:
    """`module_header()` as flowing prose: `//` markers gone, runs of whitespace
    collapsed to one space.

    A phrase pinned against the raw block is defeated by a line wrap landing in
    the middle of it, silently and in the safe-looking direction: the header's
    contract sentence wrapped between "renders nothing at" and "all", and the
    test that quotes the contract reported that the header no longer stated one
    at all.  Assertions about wording read this; assertions that a name is
    mentioned somewhere can read either.
    """
    return re.sub(
        r"\s+", " ", re.sub(r"^\s*//", "", module_header(), flags=re.MULTILINE)
    )


def _body(name: str) -> str:
    """One top-level function's body out of the form script."""
    match = re.search(rf"\nfunction {name}\([^)]*\) \{{(.*?)\n\}}", source(), re.DOTALL)
    assert match, f"{name} not found in refund_request.js"
    return match.group(1)


def _refresh() -> str:
    """The body of the form's refresh handler."""
    match = re.search(r"refresh: function\(frm\) \{(.*?)\n    \}", source(), re.DOTALL)
    assert match, "the refresh handler was not found"
    return match.group(1)


def _spoken() -> list:
    """The reason_codes the script is willing to say out loud."""
    match = re.search(
        r"const SHOPIFY_REFUSALS_WORTH_SAYING = \[(.*?)\];", source(), re.DOTALL
    )
    assert match, (
        "no allow-list of spoken refusals — the reason that came with "
        "can_write_back is being dropped again"
    )
    return re.findall(r"'([a-z_]+)'", match.group(1))


class TestWhichRefusalsAreSpoken(unittest.TestCase):
    """The list is a decision, not a dump: every refusal would nag, none strands."""

    def test_the_spoken_refusals_are_real_reason_codes(self):
        """A slug that is not in the vocabulary matches nothing and fails silently
        — the same silence this whole change exists to remove."""
        vocabulary = r.REASON_CODES[r.OUTCOME_REFUSED]
        for code in _spoken():
            self.assertIn(code, vocabulary, f"{code} is not a refusal reason_code")

    def test_the_setup_refusals_are_spoken(self):
        """Write-back off for the store, or no Admin API token: no button can
        work, and nothing else on the form accounts for its absence."""
        spoken = _spoken()
        for code in MUST_SPEAK:
            self.assertIn(
                code, spoken, f"{code} leaves the reader with no route and no reason"
            )

    def test_the_list_is_exactly_these_three(self):
        """Neither of the two tests around this one holds `nothing_to_refund`.

        Adding a code is caught by the vocabulary check and by MUST_NOT_SPEAK;
        taking one away is caught only for the two in MUST_SPEAK.  Deleting
        `nothing_to_refund` therefore passed, and it restores the regression this
        whole file exists for: `check_eligibility` refuses a refund whose net
        figure is <= 0 with `nothing_to_refund`, so there is no button, and the
        form's only account of that is a 0.00 in a currency field — reading it as
        "so no payout is offered" is a leap, and it is the reader's to make with
        nothing pointing at it.
        """
        self.assertEqual(
            sorted(_spoken()),
            sorted(SPOKEN_EXACTLY),
            "the spoken allow-list has drifted; see the comment above it in "
            "refund_request.js for why each code is in or out",
        )

    def test_the_refusals_that_are_not_ours_stay_silent(self):
        """See MUST_NOT_SPEAK. The channel codes are the module's contract."""
        spoken = set(_spoken())
        for code in MUST_NOT_SPEAK:
            self.assertNotIn(code, spoken, f"{code} must not produce a message")

    def test_the_channel_exclusion_is_justified_by_something_true(self):
        """`channel_does_not_dispatch` stays silent, and the reason given for it
        was false of the archetypal case its own comment names. Bank Transfer is
        excluded from payment_portals' `is_portal_refund`, so **Send Refund to
        Portal** is not offered there either — nobody was being kept un-stranded
        by a button that does not exist. What actually covers that channel is its
        own intro on the form ("make the bank transfer, then Book Payment
        Entry")."""
        self.assertNotIn(
            "still offers its own Send Refund to Portal",
            source(),
            "payment_portals' is_portal_refund excludes Bank Transfer, the "
            "channel this comment names, so that button is absent there too",
        )

    def test_the_list_is_justified_where_it_is_declared(self):
        """The reason each code is in or out is the load-bearing part; a bare
        array invites the next reader to extend it by feel."""
        preamble = source().split("const SHOPIFY_REFUSALS_WORTH_SAYING")[0]
        self.assertGreater(
            preamble.rsplit("\n\n", 1)[-1].count("//"),
            10,
            "the allow-list needs the comment that says why each code is in or out",
        )


class TestTheEligibilityRefusalIsRendered(unittest.TestCase):
    """`can_write_back` decided the button; `reason` came with it and was dropped."""

    #: The line that strands the whole eligibility block if the block ends up
    #: below it: a bare return on "nothing else spoke", which is precisely the
    #: condition the eligibility message exists for.  Moving the block past this
    #: restores the original defect in full and nothing else about the file
    #: changes, which is why every pin in this class is positional against it.
    STRANDING_RETURN = "if (!messages.length) return;"

    def setUp(self):
        self.body = without_comments(_body("shopify_refund_message"))

    def _stranding_return(self) -> int:
        where = self.body.find(self.STRANDING_RETURN)
        self.assertNotEqual(
            where,
            -1,
            "shopify_refund_message no longer has the early return this class "
            "pins against; re-read the function before changing this test",
        )
        return where

    def test_the_reason_is_surfaced(self):
        self.assertIn(
            "info.reason",
            self.body,
            "shopify_refund_message never reads the reason it was sent",
        )

    def test_it_only_speaks_when_the_button_is_withheld(self):
        self.assertIn(
            "!info.can_write_back",
            self.body,
            "an eligibility refusal is only worth saying when it actually "
            "withheld the button",
        )

    def test_it_only_speaks_for_a_code_on_the_list(self):
        self.assertIn("SHOPIFY_REFUSALS_WORTH_SAYING", self.body)

    def test_a_gid_or_a_stored_error_still_wins(self):
        """Both are a better account of the same document than an eligibility
        refusal is, and `already_paid` is exactly the refusal a GID produces."""
        self.assertTrue(
            re.search(
                r"if \(!messages\.length\s*\n?\s*&&\s*!info\.can_write_back", self.body
            ),
            "the new message must be gated on nothing else having spoken first",
        )

    def test_the_reason_is_escaped(self):
        """These sentences are composed from document values a person types — the
        store domain, the Sales Order name, the channel string."""
        self.assertIn("frappe.utils.escape_html(info.reason)", self.body)
        raw = re.search(
            r"(\+\s*info\.reason|\$\{info\.reason\}|\[\s*info\.reason)", self.body
        )
        self.assertIsNone(raw, f"info.reason is interpolated unescaped: {raw}")

    def test_the_escaping_note_does_not_overclaim_the_targets_table(self):
        """The comment above the gate used to justify escaping `reason` by saying
        it matched "every cell of the targets table". Four of the six cells:
        Refundable goes through `frappe.format`, and Verdict interpolates
        `t.rejected_because` raw. A true reason for escaping is available and was
        already in the same comment, so the false one has no job to do."""
        self.assertNotIn(
            "every cell of the targets table",
            source(),
            "the targets table escapes four of six cells; Refundable goes "
            "through frappe.format and Verdict interpolates rejected_because raw",
        )

    def test_the_block_sits_ahead_of_the_return_that_would_stand_it_down(self):
        """The regression this class exists to catch, and the one an editor is
        most likely to introduce: the block reads well anywhere in the function
        and is dead everywhere below this line."""
        stranding = self._stranding_return()
        gate = re.search(
            r"if \(!messages\.length\s*\n?\s*&&\s*!info\.can_write_back", self.body
        )
        self.assertTrue(gate, "the eligibility gate is gone")
        self.assertLess(
            gate.start(),
            stranding,
            "the eligibility block is below `if (!messages.length) return;`, so "
            "it can never run: that return fires on exactly the condition the "
            "block is gated on. The refusal is silent again.",
        )
        for needle in (
            "SHOPIFY_REFUSALS_WORTH_SAYING",
            "frappe.utils.escape_html(info.reason)",
            "Shopify Refund: not sent",
            "frm.dashboard.add_comment",
        ):
            where = self.body.find(needle)
            self.assertNotEqual(where, -1, f"{needle} is gone from the function")
            self.assertLess(
                where,
                stranding,
                f"{needle} sits below the early return and is unreachable",
            )

    def test_it_does_not_take_the_money_headline(self):
        """payment_portals' render_summary puts this refund's net-refund figures
        in the headline, synchronously, on every submitted document. This message
        arrives inside an xcall().then(), so a headline set here lands last and
        wins — and an advisory sentence about a missing button is not worth the
        figures the form is about. add_comment sits beside them instead."""
        stranding = self._stranding_return()
        block = self.body[self.body.find("!info.can_write_back") : stranding]
        self.assertIn(
            "frm.dashboard.add_comment",
            block,
            "the eligibility refusal must render as a dashboard comment",
        )
        for banned in ("set_headline_alert", "clear_headline"):
            self.assertNotIn(
                banned,
                block,
                f"{banned} in the eligibility block destroys payment_portals' "
                f"net-refund headline for a note that only explains a button",
            )


class TestTheDeadEndPathIsNotSilent(unittest.TestCase):
    """`is_shopify` false on a Shopify-channel refund used to render nothing."""

    def test_the_refresh_handler_no_longer_just_returns(self):
        refresh = without_comments(_refresh())
        self.assertIsNone(
            re.search(r"if \(!info \|\| !info\.is_shopify\) return;", refresh),
            "the silent early return is back: a Shopify-channel refund whose "
            "Sales Order lost its shopify_order_id renders nothing at all",
        )
        block = re.search(r"if \(!info\.is_shopify[^)]*\) \{(.*?)\}", refresh, re.DOTALL)
        self.assertTrue(
            block, "the is_shopify guard must do something before it returns"
        )
        self.assertIn("shopify_refund_dead_end_message(frm, info)", block.group(1))

    def test_an_unverified_attempt_is_not_routed_to_the_dead_end(self):
        """`Unverified` outranks `is_shopify` false, and the two really can meet.

        An attempt whose outcome could not be read back stores no refund GID —
        that is what Unverified means — so ownership rests on the Sales Order's
        `shopify_order_id` alone. Let that link stop reading — the module
        docstring above has the ways it can — `payout_owner` falls back to
        `OWNER_CALLER`, and `check_eligibility` returns `not_a_shopify_order`
        from the guard above its Unverified guard. The answer then arrives
        `is_shopify` false for a document the customer may already have been paid
        for, and the dead-end message — "this app cannot send it", true but mild,
        and no word about a possible payout — was the only thing rendered,
        because the refresh handler returns on it.
        """
        refresh = without_comments(_refresh())
        guard = re.search(r"if \(!info\.is_shopify([^)]*)\) \{", refresh)
        self.assertTrue(guard, "the is_shopify guard is gone")
        self.assertIn(
            "info.status !== 'Unverified'",
            guard.group(1),
            "an Unverified attempt whose order link no longer reads arrives here "
            "with is_shopify false and must still get the Unverified warning, "
            "which "
            "only shopify_refund_message renders",
        )

    def test_the_channel_is_read_from_the_document(self):
        """`info` may be {is_shopify: false, migrated: false} — no channel, no
        reason, no reason_code. The document always has the channel."""
        body = _body("shopify_refund_dead_end_message")
        self.assertIn("frm.doc.refund_channel", body)
        self.assertNotIn("info.refund_channel", body)

    def test_it_stays_silent_on_every_other_channel(self):
        """This function's own contract, which is narrower than the module's and
        is the only one that was ever true of it: it renders on a refund whose
        `refund_channel` is the dispatch channel and on no other, so the channel
        guard has to come before anything is drawn.

        Two corrections, because this docstring has now been wrong twice. It
        first quoted the module contract with the unmigrated clause silently
        dropped — a stale header blessed by a passing test — and was then
        rewritten to quote the "corrected" module contract, "renders nothing at
        all on a refund that is not travelling by the Shopify refund channel",
        which the module did not keep: `refresh` never read the channel, so the
        orange indicator and the targets button appeared on every refund against
        a Shopify-backed order. That is fixed in `refresh`, not here, and the
        module contract has an exception this function does not — a stored
        write-back fact renders on any channel. So this test pins the function
        and cites nothing. See TestTheAdvisoryRendersAreGatedOnTheChannel and
        TestTheModuleHeaderIsTrue for the module's half."""
        body = without_comments(_body("shopify_refund_dead_end_message"))
        guard = re.search(
            r"if \(frm\.doc\.refund_channel !== SHOPIFY_REFUND_CHANNEL\) return;", body
        )
        self.assertTrue(
            guard, "the dead-end message must be gated strictly on the channel string"
        )
        rendered = body.find("frm.dashboard.add_comment")
        self.assertNotEqual(rendered, -1, "it renders no message at all")
        self.assertLess(
            guard.start(),
            rendered,
            "the channel guard must precede anything being rendered",
        )

    def test_it_does_not_take_the_money_headline(self):
        """The worst case of the headline grab lived here: on a site that has not
        run `bench migrate`, this path fires on EVERY submitted Shopify-channel
        refund, so every one of them lost its net-refund figures in favour of a
        `bench migrate` instruction aimed at a sysadmin."""
        body = without_comments(_body("shopify_refund_dead_end_message"))
        self.assertIn("frm.dashboard.add_comment", body)
        for banned in ("set_headline_alert", "clear_headline"):
            self.assertNotIn(
                banned,
                body,
                f"{banned} here costs every submitted Shopify-channel refund on "
                f"an unmigrated site its net-refund figures",
            )

    def test_the_channel_literal_is_the_servers(self):
        """A drifted literal silences the whole path."""
        match = re.search(r"const SHOPIFY_REFUND_CHANNEL = '([^']+)';", source())
        self.assertTrue(match, "no channel constant in the form script")
        self.assertEqual(match.group(1), r.CHANNEL_DISPATCH)

    def test_the_unmigrated_answer_gets_the_migrate_sentence(self):
        """That answer carries no reason at all, so `info.reason` is undefined
        and the sentence has to be restated here."""
        self.assertIn(
            "info.migrated === false", _body("shopify_refund_dead_end_message")
        )
        self.assertIn(r._NOT_MIGRATED_REASON, source())

    def test_a_recorded_attempt_is_named(self):
        """The comment here used to justify the dashboard comment with "no money
        has moved on either of the two states this explains", and the
        counterexample is in the same module.

        `_claim` writes `shopify_writeback_status = Pending` and commits before
        `refundCreate` is posted — deliberately, so other workers can see the
        claim — so a worker killed between the two leaves `Pending` with no GID.
        Let that document's order link stop reading as well and
        `check_eligibility` answers `not_a_shopify_order` from the guard above
        its Unverified guard, so it arrives here with the customer possibly paid
        and `reason` describing only the ownership answer.

        Asserted as a branch on `info.status` rather than on a wording, because
        the wording is not what a mutation would take away.
        """
        body = without_comments(_body("shopify_refund_dead_end_message"))
        self.assertIn(
            "info.status",
            body,
            "this path says nothing about a claim that was taken and never "
            "released, which is the one state on it where money may have moved",
        )
        rendered = re.search(r"frm\.dashboard\.add_comment\((.*?)\);", body, re.DOTALL)
        self.assertTrue(rendered, "nothing is rendered at all")
        named = re.search(r"const (\w+) = info\.status\s*\n?\s*\?", body)
        self.assertTrue(
            named, "the recorded attempt is no longer conditional on a status"
        )
        self.assertIn(
            named.group(1),
            rendered.group(1),
            f"{named.group(1)} is computed and never rendered",
        )

    def test_the_claim_is_committed_before_the_mutation_is_posted(self):
        """The fact the test above rests on, asserted against the server: if the
        claim were written after the post, `Pending` with no GID would mean
        nothing was ever sent and the sentence would be scaremongering."""
        body = inspect.getsource(r._claim)
        pending = body.find("STATUS_PENDING")
        self.assertNotEqual(pending, -1, "_claim no longer writes Pending")
        self.assertIn(
            "frappe.db.commit()",
            body[pending:],
            "_claim no longer commits its own claim, so a killed worker leaves "
            "no Pending row and this path's sentence is wrong",
        )
        writeback = inspect.getsource(r.write_back_refund)
        claimed = writeback.find("_claim(refund_name)")
        # `sent = True`, the local the module calls the single fact separating
        # "nobody was paid" from "somebody might have been", set immediately
        # before the post. Matched instead of the word `refundCreate`, which is
        # all over the docstring above the code.
        posted = writeback.find("sent = True")
        self.assertNotEqual(claimed, -1, "write_back_refund no longer claims")
        self.assertNotEqual(posted, -1, "the pre-post marker is gone")
        self.assertLess(
            claimed,
            posted,
            "the claim is taken after the mutation is posted, which would make "
            "Pending-with-no-GID proof that nothing was sent",
        )

    def test_it_offers_no_button(self):
        """The server has refused, and no click fixes a missing order link or an
        unmigrated site."""
        self.assertNotIn("add_custom_button", _body("shopify_refund_dead_end_message"))

    def test_the_reason_is_escaped_here_too(self):
        body = _body("shopify_refund_dead_end_message")
        self.assertIn("frappe.utils.escape_html(reason)", body)
        self.assertIsNone(re.search(r"(\+\s*reason\b|\$\{reason\})", body))


class TestTheAdvisoryRendersAreGatedOnTheChannel(unittest.TestCase):
    """This file drew a Shopify payout on refunds that other routes pay.

    `refresh` branched on `info.is_shopify` and on nothing else.  That is
    `payout_owner == OWNER_SHOPIFY`, which `check_eligibility` settles from a
    stored refund GID **or the Sales Order's `shopify_order_id`** — never from
    `refund_channel` — so it is true of every refund raised against a
    Shopify-backed order whatever channel that refund travels by.  Bank
    Transfer, Manual Portal Refund, Payment Portal and a blank channel all got
    the orange "Shopify: not written back" indicator and the Check What Shopify
    Says button: a payout announced as outstanding on Manual Portal Refund,
    which means Shopify has already paid it, and announced beside
    payment_portals' own **Send Refund to Portal** on Payment Portal and on a
    blank channel, both of which its `is_portal_refund` includes.  Two apps, one
    form, one refund described as two outstanding payouts.

    The split the module now keeps, and the reason it is a split rather than one
    channel gate over the whole file: a fact about this document — a stored
    write-back status, a stored GID, a stored error, `Unverified` — is one of
    this app's own fields, is held by nothing else on the form, and is true
    whatever channel the refund carries, so it renders on any of them.  A render
    that presupposes a payout of ours renders only where the document's own
    `refund_channel` says this app is the one that pays.
    """

    def setUp(self):
        self.refresh = without_comments(_refresh())

    def _gate(self) -> str:
        """The local `refresh` binds the channel decision to."""
        match = re.search(
            r"const (\w+)\s*=\s*frm\.doc\.refund_channel\s*===\s*"
            r"SHOPIFY_REFUND_CHANNEL;",
            self.refresh,
        )
        self.assertTrue(
            match,
            "refresh does not read the refund channel off the document, so "
            "every advisory render is back on every Shopify-backed order",
        )
        return match.group(1)

    def test_the_channel_is_read_off_the_document(self):
        """`get_refund_writeback_status` returns no channel at all — look at its
        return dict — and the unmigrated answer is `{is_shopify: false,
        migrated: false}`.  The document always has the field, which is why
        `shopify_refund_dead_end_message` already read it there."""
        self._gate()
        self.assertNotIn(
            "info.refund_channel",
            self.refresh,
            "the status endpoint does not return a channel; reading one off "
            "`info` is reading undefined, which is never the dispatch channel",
        )

    def test_everything_that_acts_is_gated_on_the_dispatch_channel_alone(self):
        """Both controls that write anything, and nothing wider.

        The send button spends money. `Record What Shopify Says` writes a finding
        about whether a customer was paid -- it pays nobody, but it resolves an
        `Unverified` attempt this app made, and off this channel this app made
        none, so there is nothing for anybody to resolve.
        """
        gate = self._gate()
        block = re.search(
            rf"if \({gate}\) \{{(.*?)\n            \}}", self.refresh, re.DOTALL
        )
        self.assertTrue(
            block, f"nothing in refresh is gated on {gate} any more"
        )
        for call in (
            "shopify_refund_button(frm, info);",
            "shopify_resolve_unverified_button(frm, info);",
        ):
            self.assertIn(
                call,
                block.group(1),
                f"{call} is outside the dispatch-channel gate, so it renders on "
                f"every refund against a Shopify-backed order",
            )

    def test_the_read_only_probe_is_offered_where_a_shopify_refund_is_recorded(self):
        """One channel wider than the controls that act, and deliberately.

        `Check What Shopify Says` sends no mutation and writes nothing. It went
        out with the false "not written back" indicator when the channel gate
        landed, and `Manual Portal Refund` lost something real with it: that
        channel *means* Shopify has already refunded this and somebody is
        recording it here. "What does Shopify say about this order" is then
        verification of the fact the channel asserts, not a claim about a payout
        this app is about to make -- which was the whole reason for withholding
        it. It is also the only way to check that assertion from ERPNext.

        Still not offered on Bank Transfer, Payment Portal or a blank channel:
        none of those asserts anything about Shopify, and on the two that
        payment_portals pays it would sit beside that app's own send button
        describing one refund as two outstanding payouts -- the defect this class
        exists for.
        """
        self.assertIn(
            "const MANUAL_PORTAL_REFUND_CHANNEL = 'Manual Portal Refund';",
            without_comments(source()),
            "the recording channel is not named as a constant",
        )

        match = re.search(
            r"const (\w+)\s*=\s*frm\.doc\.refund_channel\s*===\s*"
            r"MANUAL_PORTAL_REFUND_CHANNEL;",
            self.refresh,
        )
        self.assertTrue(
            match, "refresh does not read the recording channel off the document"
        )
        records = match.group(1)
        gate = self._gate()

        # Anchored backwards from the call, not forwards from an `if`. A forward
        # `if \((.*?)\)` over the whole handler is happy to start at an earlier
        # branch and swallow every line between -- which made this assertion pass
        # against `if (true)`, because the over-matched span contained all three
        # names it was looking for. Found by mutation, which is the only way that
        # class of test bug shows up.
        call = "shopify_refund_targets_button(frm);"
        self.assertIn(call, self.refresh, "the probe is no longer offered at all")
        opened = self.refresh.rindex("if (", 0, self.refresh.index(call))
        closed = self.refresh.index("{", opened)
        condition = self.refresh[opened + len("if (") : closed].rstrip().rstrip(")")
        self.assertTrue(
            condition.strip(),
            "the probe is not behind a condition of its own any more -- ungated "
            "it returns to every refund against a Shopify-backed order",
        )
        self.assertIn(gate, condition, "the probe is withheld on the dispatch channel")
        self.assertIn(records, condition, "the probe is not offered where a refund is recorded")
        self.assertIn(
            "info.is_shopify",
            condition,
            "without a Shopify-backed order there is no order to ask about, and "
            "the probe would fail into its catch",
        )

    def test_the_not_written_back_indicator_is_withheld_off_the_channel(self):
        """"Not written back" is a promise that a payout is still to come, and
        this app makes one on no other channel.

        Decided by the caller and passed in, because the indicator needs both
        halves: the channel, and that the payout is Shopify's to make at all.
        `is_shopify` false with no stored status is the dead-end path's, and that
        path deliberately draws no indicator — a note saying only that something
        is wrong is worse than no indicator, which is why it says nothing.
        """
        gate = self._gate()
        call = re.search(
            r"shopify_refund_indicator\(frm, info, ([^)]*)\);", self.refresh
        )
        self.assertTrue(
            call, "the indicator is no longer told anything about the channel"
        )
        self.assertIn(gate, call.group(1))
        self.assertIn("info.is_shopify", call.group(1))

        signature = re.search(
            r"function shopify_refund_indicator\(([^)]*)\)", source()
        )
        self.assertTrue(signature, "shopify_refund_indicator is gone")
        parameters = [p.strip() for p in signature.group(1).split(",")]
        self.assertEqual(
            len(parameters), 3, "the indicator no longer takes the gate at all"
        )

        body = without_comments(_body("shopify_refund_indicator"))
        withheld = body.find(f"if (!{parameters[2]}) return;")
        self.assertNotEqual(
            withheld,
            -1,
            "the indicator accepts the gate and does not act on it, so the "
            "orange promise is back on every channel",
        )
        promised = body.find("Shopify: not written back")
        self.assertNotEqual(promised, -1, "the indicator's fallback is gone")
        self.assertLess(
            withheld,
            promised,
            "the gate is checked after the promise has already been drawn",
        )

    def test_a_stored_status_is_drawn_on_any_channel(self):
        """A fact, so no gate — and on the dead-end path it is the only sign the
        form gives that an attempt was ever claimed against this document.
        `_claim` writes `Pending` and commits BEFORE `refundCreate` is posted, so
        a worker killed between the two leaves `Pending` with no GID."""
        indicator = self.refresh.find("shopify_refund_indicator(frm, info")
        branch = self.refresh.find("if (!info.is_shopify")
        self.assertNotEqual(indicator, -1, "the indicator is not called at all")
        self.assertNotEqual(branch, -1, "the is_shopify branch is gone")
        self.assertLess(
            indicator,
            branch,
            "the indicator is called after the branch that returns on the "
            "dead-end path, so a stored Pending claim is invisible there",
        )

        signature = re.search(
            r"function shopify_refund_indicator\(([^)]*)\)", source()
        )
        gate = [p.strip() for p in signature.group(1).split(",")][2]
        body = without_comments(_body("shopify_refund_indicator"))
        self.assertLess(
            body.find("if (info.status)"),
            body.find(f"if (!{gate}) return;"),
            "the stored status is drawn behind the advisory gate, which makes a "
            "fact about this document conditional on the channel",
        )

    def test_the_message_is_not_gated_on_the_channel(self):
        """Every state `shopify_refund_message` renders is a fact about this
        document: `Unverified`, a stored GID, a stored write-back error.  The one
        advisory branch in it — the eligibility refusal — is gated by the server
        instead, and exactly: `check_eligibility` judges the channel before the
        status, the amount and the store, so a refund on any other channel comes
        back `channel_does_not_dispatch` or `channel_is_manual_portal_refund` and
        can never carry a code from the spoken list."""
        gate = self._gate()
        block = re.search(
            rf"if \({gate}\) \{{(.*?)\n            \}}", self.refresh, re.DOTALL
        )
        self.assertTrue(block)
        self.assertNotIn(
            "shopify_refund_message",
            block.group(1),
            "a stored GID and a stored error are facts about this document and "
            "are held by nothing else on the form",
        )
        self.assertIn("shopify_refund_message(frm, info);", self.refresh)

    def test_the_channel_codes_are_terminal_on_the_server(self):
        """The claim the test above rests on, asserted against the server rather
        than believed: both channel guards return, and both sit above every guard
        that can produce a spoken code."""
        body = inspect.getsource(r.check_eligibility)
        channel = body.find('out["reason_code"] = "channel_does_not_dispatch"')
        self.assertNotEqual(channel, -1, "the channel guard has moved")
        for later in ("nothing_to_refund", "writeback_unavailable_for_store",
                      "no_api_credentials"):
            self.assertLess(
                channel,
                body.find(f'out["reason_code"] = "{later}"'),
                f"{later} is now decided before the channel is, so an "
                f"off-channel refund can reach the spoken allow-list",
            )
        self.assertIn(
            "return out",
            body[channel:channel + 400],
            "the channel guard no longer returns, so it is not terminal",
        )


class TestTheFormWithholdsWhatItMustWithhold(unittest.TestCase):
    """Three guards with nothing holding them, found by mutating a copy.

    Each is one line, each reads as redundant beside something else in the file,
    and each is the only thing standing between this form and a sentence it
    cannot stand behind.
    """

    def test_nothing_is_rendered_on_any_other_docstatus(self):
        """The guard is the whole of this app's silence on a draft and on a
        cancelled Refund Request, and the cancelled case is the one that matters.
        payment_portals' `on_cancel` records the state at length: send and book
        are separate clicks, so a dispatched Shopify refund reaches cancellation
        carrying a GID and no Payment Entry, walks past the booking clause and
        past `cf_refund_id` — the storefront route never calls Cashfree and so
        never learns one — and is cancelled, reversing the GL entries for a
        payout the OCC bridge may really have made.  That comment also notes that
        neither app's form script says anything about the money afterwards, this
        app's because of the line below.  Silence there is a jointly documented
        state, not an oversight; losing it would replace it with an eligibility
        refusal about a button, on a document whose money question is a different
        one.

        Positional, because the guard is worthless below the lookup: the xcall
        would already be in flight and its `.then` would render on whatever came
        back.
        """
        guard = re.search(r"if \(frm\.doc\.docstatus !== 1\) return;", self.refresh())
        self.assertTrue(
            guard,
            "the docstatus guard is gone: this app now renders on drafts and on "
            "cancelled refunds, where payment_portals renders nothing",
        )
        lookup = self.refresh().find("frappe.xcall")
        self.assertNotEqual(lookup, -1, "the status lookup is gone")
        self.assertLess(
            guard.start(),
            lookup,
            "the docstatus guard sits below the lookup, so the answer arrives "
            "and renders anyway",
        )

    def test_the_button_refuses_an_unverified_attempt_by_name(self):
        """Belt and braces, and its own comment says so — which is exactly why no
        test held it.  `can_write_back` is already false for an unconfirmed
        attempt (`unverified_previous_attempt`), so removing this changes nothing
        that any other test observes; it removes the second lock on the one state
        where pressing the button pays the customer twice.  The mutation is a
        one-line deletion that leaves the suite green and the file readable."""
        body = without_comments(_body("shopify_refund_button"))
        guard = body.find("if (info.status === 'Unverified') return;")
        self.assertNotEqual(
            guard,
            -1,
            "the button no longer refuses Unverified by name, leaving eligibility "
            "as the only thing between a stray click and a second real payout",
        )
        offered = body.find("frm.add_custom_button")
        self.assertNotEqual(offered, -1, "the button is gone")
        self.assertLess(
            guard, offered, "the guard sits below the button it is meant to withhold"
        )

    def test_the_unverified_indicator_is_not_red(self):
        """Red reads as "it failed, try again", and trying again is the one thing
        that must not happen in this state — the mutation went out, the customer
        may already have been paid, and the Cashfree-OCC bridge makes that real
        money.  The colour is explained in a comment and pinned by nothing, so
        `'Unverified': 'red'` was a passing change."""
        body = without_comments(_body("shopify_refund_indicator"))
        colour = re.search(r"'Unverified':\s*'(\w+)'", body)
        self.assertTrue(colour, "Unverified has no colour of its own any more")
        self.assertEqual(
            colour.group(1),
            "orange",
            "red invites the retry that pays the customer a second time",
        )

    def refresh(self) -> str:
        return without_comments(_refresh())


class TestTheModuleHeaderIsTrue(unittest.TestCase):
    """The header is the first thing a reader believes about this file.

    It described the file as it was before the dead-end path existed: "renders
    nothing at all when the refund is not a Shopify one, **or when
    payment_portals is installed without the write-back fields migrated**".  The
    second clause stopped being true the moment `shopify_refund_dead_end_message`
    landed, because that path fires on exactly {is_shopify: false,
    migrated: false} — and the only test that quoted the contract quoted it with
    the migration clause silently dropped.

    The correction it was given then was false in both halves, and by execution:
    "renders nothing at all when the refund is not travelling by the Shopify
    refund channel" was a channel the code never read — `refresh` branched on
    `info.is_shopify`, which is true of every refund against a Shopify-backed
    order on any channel — and "on one that is, it always says something now"
    ignores the `.catch(() => null)` on the lookup.  So the pins below are on the
    contract's *terms*: the channel has to be named as the gate, and the silences
    the file really keeps have to be the ones it claims.
    """

    def test_it_does_not_promise_silence_on_an_unmigrated_site(self):
        claim = re.search(r"renders nothing at all(.*?)\.", header_prose())
        self.assertTrue(
            claim, "the header no longer states the module's contract at all"
        )
        for stale in ("migrated", "write-back fields"):
            self.assertNotIn(
                stale,
                claim.group(1),
                "an unmigrated site on the Shopify channel now gets "
                "shopify_refund_dead_end_message, not silence",
            )

    def test_the_contract_it_states_is_the_one_the_code_keeps(self):
        """Three things have to hold together, because the header is read as the
        licence for the next path somebody adds:

        the silence is per-channel and conditional, since a write-back fact
        renders anywhere; the channel is read off the document, which is the
        decision `refresh` now makes; and the promise of always saying something
        is gone, because the lookup swallows its own failure.
        """
        prose = header_prose()
        claim = re.search(r"renders nothing at all(.*?)\.", prose)
        self.assertTrue(claim, "the header states no contract at all")
        self.assertRegex(
            claim.group(1),
            r"unless|except",
            "the contract is stated without its exception, and the exception is "
            "the half the code actually implements: a stored status, GID or "
            "error renders on any channel",
        )
        self.assertIn(
            "frm.doc.refund_channel",
            prose,
            "the header must name what the gate reads, because reading the "
            "channel off `info` is reading undefined",
        )
        # The header names this claim, as it names every claim it corrects. What
        # it must never do is state it: the xcall carries `.catch(() => null)`,
        # so a lookup that throws renders nothing whatsoever. A retracted claim
        # left standing next to its correction is how the migration clause
        # survived a whole round.
        self.assertRegex(
            prose,
            r"it always says something[^.]*(false|wrong|not true)",
            "the header asserts that a Shopify-channel refund always gets a "
            "render, and the lookup's own catch is the counterexample",
        )
        self.assertIn(
            "catch",
            prose,
            "the silence the catch produces is part of the contract and has to "
            "be stated with it",
        )

    def test_it_says_what_makes_the_button_absent_when_it_is_absent(self):
        """`can_write_back` needs `enable_sync` and `enable_refund_writeback` on
        the store's Shopify Settings plus Admin API credentials, and a reader
        looking at the form can see neither of those settings.  A missing button
        with no word about why is indistinguishable from a broken page, so the
        header has to name both halves of the setup and both refusal codes.

        That is the durable half.  This test used to pin the other half: that
        the estate was currently set one way, that the button was therefore on
        no form at all and the eligibility refusal was this file's ordinary
        output — and it held the sentence in place by asserting it was *present*
        in tests/test_refund_report.py, so correcting the record there broke
        this test.  The premise had already inverted: the payout exists only
        while somebody has armed the toggle, and the project's working record
        for 2026-09-08 has it armed on production, so the button does render on
        a submitted Shopify-channel refund that passes eligibility.  Which
        stores it is absent on is not this file's business; what makes it absent
        is.  See TestNoFileHardCodesTheLiveToggleValue.
        """
        prose = header_prose()
        self.assertIn("enable_refund_writeback", prose)
        self.assertIn("Admin API", prose)
        for code in MUST_SPEAK:
            self.assertIn(
                code,
                prose,
                "the header must name the refusal a reader meets when that half "
                "of the setup is missing — nothing else on the form says it, "
                "and the header is where the paths get ranked for the next "
                "editor",
            )

    def test_it_says_what_the_unmigrated_site_gets_instead(self):
        """Correcting the clause by deleting it would leave the reader with no
        account of the state at all, which is how the stale clause survived."""
        self.assertIn(
            "shopify_refund_dead_end_message",
            module_header(),
            "the header must name the path that answers an unmigrated site",
        )

    def test_it_does_not_claim_the_button_is_the_only_trigger(self):
        """The third stale sentence in the same paragraph, and the one the new
        message wording depends on. `utils/refund`'s own module docstring says
        "Two callers reach write_back_refund": `writeback_now` behind this
        button, and payment_portals' `refund_payout_dispatchers` hook. The hook
        is only reachable from that app's Send step, which `sent_by_this_app`
        withholds on this channel — which is exactly why the messages below say
        "nothing on this form" rather than "nothing else"."""
        self.assertIn(
            "refund_payout_dispatchers",
            module_header(),
            "the header must account for the second caller of write_back_refund",
        )
        claim = re.search(r"ONLY trigger for the write-back", module_header())
        self.assertIsNone(
            claim,
            "hooks.py registers refund_payout_dispatchers, so the button is not "
            "the only trigger — see utils/refund's module docstring",
        )

    def test_it_states_who_owns_the_headline(self):
        """The other module-wide rule this round establishes, and the one an
        editor adding a fifth message path will otherwise break."""
        header = module_header()
        self.assertIn("render_summary", header)
        self.assertIn("add_comment", header)


class TestTheLoudStatesKeepTheHeadline(unittest.TestCase):
    """The split is between advice and alarm, and only advice gives up the headline.

    `Unverified` means the mutation went out and the customer may already have
    been paid; a stored GID means Shopify has paid and the books do not say so.
    Both outrank the net figure of a refund that is merely proposed, and
    `Unverified` is meant to shout.  Moving those to a dashboard comment for
    tidiness is a real temptation after the change above, so pin them.
    """

    def setUp(self):
        self.body = without_comments(_body("shopify_refund_message"))

    def test_unverified_still_takes_the_headline(self):
        block = re.search(
            r"if \(info\.status === 'Unverified'\) \{(.*?)\n    \}",
            self.body,
            re.DOTALL,
        )
        self.assertTrue(block, "the Unverified branch is gone")
        self.assertIn(
            "set_headline_alert",
            block.group(1),
            "Unverified is the loudest state on this form and owns the headline",
        )

    def test_unverified_refuses_every_route_and_not_just_the_button(self):
        """"Do not retry it" reads as "do not press that button again", and the
        button is not the dangerous route — refunding the order by hand in the
        Shopify admin is, because the Cashfree-OCC bridge turns that into a real
        gateway refund too and this document would then be the second payout.
        payment_portals' own intro for this state already names all three routes;
        the headline that sits above it must not be the milder sentence."""
        block = re.search(
            r"if \(info\.status === 'Unverified'\) \{(.*?)\n    \}",
            self.body,
            re.DOTALL,
        )
        self.assertTrue(block, "the Unverified branch is gone")
        text = block.group(1)
        self.assertIn("Do not send it again", text)
        self.assertIn("Shopify admin", text)

    def test_unverified_names_no_send_route_and_no_cancellation(self):
        """The loudest state must not hand the reader an action that pays the
        customer twice, nor cancelling — which reverses the GL entries for a
        payout that may really have happened."""
        block = re.search(
            r"if \(info\.status === 'Unverified'\) \{(.*?)\n    \}",
            self.body,
            re.DOTALL,
        )
        self.assertTrue(block)
        text = block.group(1).lower()
        for banned in ("cancel", "retry", "send refund to portal"):
            self.assertNotIn(banned, text, f"the Unverified headline offers {banned!r}")

    def test_a_gid_or_a_stored_error_still_takes_the_headline(self):
        """Both are pushed into `messages` and rendered by the shared block at
        the foot of the function, which is the headline."""
        tail = self.body[self._stranding():]
        self.assertIn("set_headline_alert", tail)

    def _stranding(self) -> int:
        where = self.body.find("if (!messages.length) return;")
        self.assertNotEqual(where, -1)
        return where


class TestTheUnmigratedStateIsHandledElsewhere(unittest.TestCase):
    """`not_installed` was on the spoken allow-list and could never arrive there.

    `check_eligibility` returns it before it reads anything, with `payout_owner`
    still `OWNER_UNKNOWN`, so `get_refund_writeback_status` reports `is_shopify`
    false and the refresh handler routes the answer to the dead-end path.  Its
    old comment justified the entry as insurance against
    `get_refund_writeback_status`'s own early return going away — insurance
    against nothing, since `check_eligibility` answers `OWNER_UNKNOWN` on an
    unmigrated site with or without that return.
    """

    def test_check_eligibility_leaves_ownership_unknown_on_an_unmigrated_site(self):
        """The fact the whole argument rests on, asserted against the server."""
        out = r.check_eligibility.__doc__
        self.assertTrue(out, "check_eligibility lost its docstring")
        body = inspect.getsource(r.check_eligibility)
        head = body.split("if not _has_writeback_fields():", 1)
        self.assertEqual(len(head), 2, "the not_installed guard has moved")
        branch = head[1].split("return", 1)[0]
        self.assertIn('out["reason_code"] = "not_installed"', branch)
        self.assertNotIn("payout_owner", branch)
        self.assertIn(
            '"payout_owner": OWNER_UNKNOWN',
            head[0],
            "payout_owner starts unknown, which is why is_shopify comes back "
            "false and this refusal never reaches the eligibility message",
        )
        self.assertNotEqual(r.OWNER_UNKNOWN, r.OWNER_SHOPIFY)

    def test_the_comment_records_where_the_state_is_really_answered(self):
        preamble = source().split("const SHOPIFY_REFUSALS_WORTH_SAYING")[0]
        self.assertIn(
            "not_installed",
            preamble,
            "removing the entry without saying where that state goes invites the "
            "next reader to put it back",
        )
        self.assertIn("shopify_refund_dead_end_message", preamble)


# ── The live setting of the store checkbox is not a fact about this code ─────

#: The files that have carried a claim about how `enable_refund_writeback` is
#: set in production.  Seven such claims stood in these five, all of them
#: saying the checkbox was clear across the estate and had to remain so, while
#: the project's refund write-back working record for 2026-09-08 has it armed
#: on the two production stores on purpose: with it clear,
#: `_settings_for_store(require_enabled=True)` finds nothing and the payout
#: refuses `writeback_unavailable_for_store`, so somebody arming it is what
#: makes the feature exist at all.  Nothing caught the drift, because a
#: checkbox anybody can change in a settings form had been copied into source
#: as though it were a property of the code.
#:
#: Two documents outside this list also carry a setting for the same checkbox
#: (REFUND-DISPATCH-CONTRACT.md, REFUND-WRITEBACK-HANDOFF.md).  They belong to
#: other work and are deliberately not scanned here.
TOGGLE_CLAIM_FILES = (
    Path(__file__).resolve(),
    Path(__file__).resolve().parent / "test_refund_report.py",
    Path(__file__).resolve().parents[1] / "utils" / "refund_report.py",
    FORM,
    Path(__file__).resolve().parents[2] / "REFUND-REPORT-CONTRACT.md",
)

#: The ways the checkbox's setting gets written down.
TOGGLE_VALUES = ("0", "1", "on", "off")

#: The words that turn one store's setting into a claim about the estate.
TOGGLE_SCOPES = ("every", "each", "all", "both")

_VALUE = r"\b(?:" + "|".join(TOGGLE_VALUES) + r")\b"
_SCOPE = (
    r"\b(?:" + "|".join(TOGGLE_SCOPES) + r")\b"
    r"(?:\s+\w+)?(?:\s+\w+)?\s+stores?\b"
)
_WITHIN_ONE_SENTENCE = r"[^.\n]*?"

#: A sentence putting a setting on the whole estate, in either word order.
ESTATE_CLAIM = re.compile(
    _VALUE + _WITHIN_ONE_SENTENCE + _SCOPE
    + "|" + _SCOPE + _WITHIN_ONE_SENTENCE + _VALUE,
    re.IGNORECASE,
)

#: A sentence telling the reader the checkbox has to remain at some setting.
#: Which stores have the payout armed is the owner's call in either direction,
#: and phrasing it as a rule is why the setting went unread for so long: a rule
#: does not look like something to go and re-check.
PRESCRIPTION = re.compile(
    r"must stay(?:\s+\w+)?(?:\s+\w+)?\s+" + _VALUE, re.IGNORECASE
)

#: An as-of date.  It is what separates a claim a reader can act on from one
#: they cannot: a dated claim can be recognised as stale, an undated one is
#: indistinguishable from a current fact.
AS_OF_DATE = re.compile(r"\b\d{4}-\d\d-\d\d\b")

#: How far either side of a claim its date is allowed to sit.
DATE_WINDOW = 240

_FIRST_SCOPE_WORD = TOGGLE_SCOPES[0]


def claim_prose(path: Path) -> str:
    """`path` as one line of flowing prose: comment markers gone, runs of
    whitespace collapsed.

    A phrase pinned against raw text is defeated by a line wrap landing inside
    it — that is how the module header's contract sentence escaped its own test
    once already, and why `header_prose` exists.  A claim spread over three
    `//` lines is the same claim.
    """
    text = path.read_text(encoding="utf-8")
    return re.sub(
        r"\s+", " ", re.sub(r"^\s*(?://+|#+)\s?", "", text, flags=re.MULTILINE)
    )


def claims(pattern, text: str):
    """Every match of `pattern` in `text`."""
    return [match.group(0) for match in pattern.finditer(text)]


def undated(pattern, text: str):
    """Matches of `pattern` in `text` with no as-of date within reach of them.

    Takes text rather than a path so the rule itself can be exercised on a
    string — the rule is "date it", not "never say it", and a guard that only
    ever ran over the tree would leave the next editor free to read it as the
    latter and delete the fact instead of dating it.
    """
    return [
        match.group(0)
        for match in pattern.finditer(text)
        if not AS_OF_DATE.search(
            text[max(0, match.start() - DATE_WINDOW):match.end() + DATE_WINDOW]
        )
    ]


def stale_claim(value: str) -> str:
    """The sentence this round removed, and its inverse, assembled from the
    vocabulary above rather than spelled out.

    This scan reads its own file, so a needle written out here would be a
    needle this file contains.  That is not hypothetical: the test this class
    stands next to used to assert the stale sentence was *present* in
    tests/test_refund_report.py, which is how correcting the record there came
    to break a test, and how the sentence survived a round.
    """
    return (
        "enable_refund_writeback is {v} on {scope} store and must stay {v}"
    ).format(v=value, scope=_FIRST_SCOPE_WORD)


class TestNoFileHardCodesTheLiveToggleValue(unittest.TestCase):
    """What a store's checkbox is set to today is not a fact about this code.

    Seven claims across five files said it was clear everywhere and had to
    remain so.  It is not, and it is not this app's call: the payout exists
    only while somebody has armed it, and the project's working record for
    2026-09-08 has it armed on production.  Every one of those claims read as
    a standing truth, which is exactly why none of them was ever re-read — so
    the property worth holding is not the newer setting but the absence of any
    undated one.
    """

    # Each of these collects across every file before asserting.  Failing on
    # the first offender would have hidden four of the seven, and the seven
    # were only ever found because somebody grepped for the sentence.

    def test_the_stale_sentence_and_its_inverse_are_both_gone(self):
        found = [
            "{name}: {needle}".format(name=path.name, needle=needle)
            for value in TOGGLE_VALUES
            for needle in [stale_claim(value)]
            for path in TOGGLE_CLAIM_FILES
            if needle in claim_prose(path)
        ]
        self.assertEqual(
            [],
            found,
            "these state the checkbox's setting as a standing fact: {found!r}. "
            "Flipping the digit is the same defect carrying a newer "
            "value.".format(found=found),
        )

    def test_no_file_puts_a_setting_on_the_whole_estate_undated(self):
        found = [
            "{name}: {claim}".format(name=path.name, claim=claim)
            for path in TOGGLE_CLAIM_FILES
            for claim in undated(ESTATE_CLAIM, claim_prose(path))
        ]
        self.assertEqual(
            [],
            found,
            "these put a setting on the estate with no as-of date: {found!r}. "
            "Where the reasoning does not need the setting, state the "
            "reasoning without it; where it does, date it and say where the "
            "real setting is read.".format(found=found),
        )

    def test_nothing_prescribes_a_setting_for_the_checkbox(self):
        found = [
            "{name}: {claim}".format(name=path.name, claim=claim)
            for path in TOGGLE_CLAIM_FILES
            for claim in claims(PRESCRIPTION, claim_prose(path))
        ]
        self.assertEqual(
            [],
            found,
            "these prescribe a setting: {found!r}. Arming the payout is the "
            "owner's decision, and with it disarmed the payout refuses "
            "writeback_unavailable_for_store.".format(found=found),
        )

    def test_a_dated_claim_is_allowed_and_an_undated_one_is_not(self):
        """The rule these tests enforce, exercised on a string rather than on
        the tree.

        The JS header has to name the setting — which of its paths a reader
        meets depends on it — so "never write the value down" is the wrong
        rule and would be obeyed by deleting the fact.  The rule is that a
        volatile fact carries an as-of date, which is what lets the next
        reader tell a stale claim from a current one.  The stale sentence had
        no date, so nothing about it looked old.
        """
        claim = "the toggle is {v} on {scope} store".format(
            v=TOGGLE_VALUES[1], scope=_FIRST_SCOPE_WORD
        )
        self.assertTrue(
            claims(ESTATE_CLAIM, claim),
            "the scan no longer recognises a claim about the whole estate",
        )
        self.assertTrue(
            undated(ESTATE_CLAIM, claim),
            "an undated claim about the whole estate has to be reported, or "
            "the stale sentence comes back exactly as it left",
        )
        self.assertEqual(
            [],
            undated(ESTATE_CLAIM, "Read as of 2026-09-08: " + claim),
            "a dated claim must pass, or the rule reads as a ban on stating "
            "the setting at all",
        )


if __name__ == "__main__":
    unittest.main()
