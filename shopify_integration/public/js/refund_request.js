// refund_request.js — Shopify refund write-back on the Refund Request form.
//
// Refund Request belongs to payment_portals.  This file only ever adds, and
// what it adds falls into two halves that answer to different rules.
//
//   FACTS ABOUT THIS DOCUMENT — a stored write-back status, a stored refund
//   GID, a stored write-back error, Unverified.  Each is one of this app's own
//   fields, nothing else on the form holds it, and each is true whatever
//   refund_channel the refund carries.  They render on any channel.
//
//   ADVISORY RENDERS — the orange "Shopify: not written back" indicator, which
//   promises a payout still to come, and the Check What Shopify Says button,
//   which diagnoses a payout this app is about to make.  Both presuppose that
//   this app is the one that pays this refund, so both are gated on
//   frm.doc.refund_channel, read off the document exactly as
//   shopify_refund_dead_end_message reads it.
//
// That gate is a fix, and this header is where the bug was written down as the
// contract.  It promised outright silence off the Shopify refund channel, and
// refresh never asked what the channel was: it branched on info.is_shopify,
// which is payout_owner == OWNER_SHOPIFY, which check_eligibility settles from
// a stored refund GID **or the Sales Order's shopify_order_id** — never from
// refund_channel.  So it is true of every refund raised against a
// Shopify-backed order whatever channel that refund travels by, and a Bank
// Transfer, Manual Portal Refund, Payment Portal or blank-channel refund on
// such an order got the advisory pair: an orange indicator announcing a Shopify
// payout as outstanding when nothing was ever going to make it — flatly
// contradicting Manual Portal Refund, which means Shopify has already paid it —
// and a button offering to interrogate Shopify about it.  On Payment Portal and
// on a blank channel it was worse than noise: both are inside payment_portals'
// is_portal_refund, so that app offers Send Refund to Portal on them, and the
// two apps then described one refund as two outstanding payouts.
//
// The other half of the old claim — that on a Shopify-channel refund it always
// says something — was false too.  The xcall carries .catch(() => null), so a
// lookup that throws renders nothing: deliberate, because a failed read must
// not paint a form belonging to another app, and a silence, so the promise went
// rather than the catch.
//
// The contract as the code has it: on the Shopify refund channel this file
// speaks unless the lookup fails; on every other channel it renders nothing at
// all unless the document itself carries a write-back fact, and nothing
// whatsoever when it carries none.  An unmigrated site is not an exception, and
// this header used to claim it was — {is_shopify: false, migrated: false} is
// exactly what shopify_refund_dead_end_message answers, on the dispatch
// channel, restating the migrate instruction because that answer carries no
// reason of its own.
//
// One live fact, because it decides which of these paths a reader actually
// meets — so it is dated, and the place it is really read is named.
// can_write_back needs Shopify Settings for the store with enable_sync and
// enable_refund_writeback both set and Admin API credentials present.  Miss the
// toggle and this file speaks writeback_unavailable_for_store; miss the token
// and it speaks no_api_credentials; in either state there is no Refund in
// Shopify button on the form at all, and since neither setting is legible from
// the form, that message is the only account a reader gets.  As of 2026-09-08
// the toggle is set: 1 on both production stores, per the project's refund
// write-back working record of that date.  Shopify Settings for the store is
// where the current answer lives, not this comment.  So the button does render
// on a submitted Shopify-channel refund that passes the rest of
// check_eligibility, and the setup refusal is what a misconfigured store gets
// rather than this module's ordinary output.  This header claimed the reverse
// for a while, off a copy of the toggle's then value that no settings change
// could reach.
//
// Who owns the form's headline is a module-wide rule, not a per-path choice.
// payment_portals' render_summary puts this refund's net-refund figures in the
// headline on every submitted document, synchronously, from its own refresh —
// while everything here arrives inside an xcall().then() and therefore lands
// last and wins.  So the headline is spent only on money that has moved or may
// have moved: Unverified, a stored refund GID, a stored write-back error.  The
// advisory paths — the eligibility refusal and the dead-end message — use
// frm.dashboard.add_comment, which is what payment_portals itself uses for a
// note of that kind (note_payouts_off), and which leaves the figures alone.
//
// There is deliberately no doc_events hook: a successful refundCreate pays the
// customer (the Cashfree-OCC app bridges it into a real Cashfree refund), and
// deciding to pay somebody belongs with a person or with whatever owns the
// refund's money path — not with a save handler in this app.  So the
// confirmation below is not a formality.
//
// The button is not the ONLY trigger, which this header used to claim.
// utils/refund's module docstring names two callers of write_back_refund: this
// button, through writeback_now, and payment_portals' refund_payout_dispatchers
// hook, registered in hooks.py.  The hook is reached only from that app's own
// Send step, and `sent_by_this_app` withholds that on the Shopify channel — so
// no route on the form but this button gets there today, which is why the
// messages below say "nothing on this form" and not "nothing else".  And where
// the store is not set up, no route at all: the one route the form has is a
// button nothing renders while enable_refund_writeback is off, which is why the
// messages have to be right about what is missing rather than which button to
// press.

// payment_portals' `refund_channel` value whose payout goes out through the
// Shopify order — utils/refund.CHANNEL_DISPATCH, which this side cannot import.
// tests/test_refund_form_refusal_is_visible.py pins the literal against it,
// because a drifted string here silences the message below rather than breaking
// anything loudly.
//
// The prefix is not decoration.  payment_portals' own form script for this
// doctype declares a top-level `const SHOPIFY`, and both apps' `doctype_js` for
// one doctype can end up evaluated in a single global scope — where a duplicated
// top-level `const` is a SyntaxError that takes every handler in that scope down
// with it, theirs as well as ours.  So the obvious name is the one name this
// file must not use.
const SHOPIFY_REFUND_CHANNEL = 'Shopify';

// The channel that means Shopify has ALREADY refunded this and ERPNext is
// recording it, rather than that this app is about to make the refund. Named
// here only so the read-only probe can be offered on it -- nothing that acts is.
//
// Spelled the long way for the shared-scope reason above: payment_portals'
// script declares `const MANUAL_PORTAL` for this same channel, and a second
// top-level `const` of that name in one doctype_js scope is a SyntaxError that
// takes both apps' handlers down.
const MANUAL_PORTAL_REFUND_CHANNEL = 'Manual Portal Refund';

// The eligibility refusals this form says out loud, and deliberately only these.
//
// `get_refund_writeback_status` returns `reason` and `reason_code` off
// `check_eligibility` for every refusal.  Showing all of them would nag on half
// the states a refund passes through on its way to Approved; showing none of
// them — what this file did until now — strands the reader on the ones where
// there is no button and no other route, because payment_portals withholds its
// own send button on this channel.  A refusal nobody can read is then a refund
// nobody can move.  payment_portals' intro for this channel points here by
// name: it says the payout comes from this app's write-back, that this app's
// button appears only where the store is set up for write-back, and that where
// it is not, this app's own message on the form says so.  This is that message,
// and wherever enable_refund_writeback is off for a store it is the whole of
// what a reader gets, not an aside.  So: a code is spoken when it is
// reachable on a submitted Shopify-channel refund, is not already accounted for
// by something else on the form, and is somebody's to fix.
//
// Every code `check_eligibility` can return, and why it is in or out:
//
//   SPOKEN
//   writeback_unavailable_for_store  Refund Write-Back is off for the store, or
//   no_api_credentials               it has no Admin API token.  Setup, fixable,
//                                    and invisible from this form otherwise.
//   nothing_to_refund                Net refund is <= 0.  The figure is on the
//                                    form; that it withheld the only button is
//                                    not, and reading 0.00 as "so no payout is
//                                    offered" is a leap.
//
//   NOT LISTED AT ALL, because this function never sees it
//   not_installed                    Answered by shopify_refund_dead_end_message,
//                                    and only there.  check_eligibility returns
//                                    this before it reads anything, leaving
//                                    payout_owner at its initial OWNER_UNKNOWN,
//                                    so get_refund_writeback_status reports
//                                    is_shopify false and the refresh handler
//                                    routes the answer to the dead-end path.  It
//                                    was on the list below as insurance against
//                                    get_refund_writeback_status's own early
//                                    return going away, and insured nothing:
//                                    without that return check_eligibility still
//                                    answers OWNER_UNKNOWN on an unmigrated
//                                    site, so the answer still goes to the
//                                    dead-end path — which is why that path
//                                    restates _NOT_MIGRATED_REASON itself
//                                    instead of relying on info.reason.
//
//   SILENT, because something else already says it
//   already_paid                     the GID banner below says it, in the GID.
//   unverified_previous_attempt      its own branch at the top of
//                                    shopify_refund_message, which says far
//                                    more than the reason does and must keep
//                                    saying it.
//   already_booked                   status Completed with a Payment Entry, both
//                                    on the form, and the orange "not written
//                                    back" indicator carries the other half.
//                                    Nobody is hunting for a button here.
//   wrong_refund_status              the status field says it, payment_portals
//                                    writes a sentence per status, and the state
//                                    resolves itself as the document advances.
//   not_submitted                    unreachable: the refresh handler returns on
//                                    docstatus !== 1.
//
//   SILENT, because it is not this app's business
//   channel_is_manual_portal_refund  Shopify has already paid that refund.  The
//                                    advisory renders are now withheld on that
//                                    channel too: the orange "not written back"
//                                    indicator was contradicting this very
//                                    entry on every such refund.
//   channel_does_not_dispatch        another route pays it, and that channel's
//                                    own intro on this form is what says so —
//                                    Bank Transfer, the archetype this slug's
//                                    reason names, is told to make the transfer
//                                    and then Book Payment Entry.  NOT because
//                                    payment_portals offers Send Refund to
//                                    Portal there: its is_portal_refund excludes
//                                    Bank Transfer, so that button is absent on
//                                    that channel too.  The conclusion survives
//                                    the correction; the old reason for it did
//                                    not.  On Payment Portal and on a blank
//                                    channel, which this same slug covers,
//                                    is_portal_refund does include them and that
//                                    button is the route that pays.
//   not_a_shopify_order              both come back with is_shopify false, so
//   refund_request_missing           they never reach here; they are the
//                                    dead-end path's to explain.
//   amount_mismatch                  reserved for a cross-check that is not
//                                    built; check_eligibility never emits it.
//
// Neither channel entry is a live filter, and both are here so that nobody adds
// one: check_eligibility judges the channel BEFORE the status, the amount and
// the store, and both channel guards return, so a refund on any channel but
// Shopify's carries one of those two codes and can never carry a code from the
// spoken list at all.  What used to justify their silence — that speaking would
// break this module's contract of rendering nothing whatsoever off the channel
// — was a contract the module did not keep; see the header for the one it keeps
// now and for the write-back facts that are the exception to it.
//
// The FAILED_UNSENT and IN_PROGRESS codes are not this function's at all — they
// come off a send that actually happened, and reach this form as the stored
// `shopify_writeback_error`, which `info.error` already renders.
const SHOPIFY_REFUSALS_WORTH_SAYING = [
    'writeback_unavailable_for_store',
    'no_api_credentials',
    'nothing_to_refund'
];

frappe.ui.form.on('Refund Request', {
    refresh: function(frm) {
        if (frm.doc.docstatus !== 1) return;

        frappe.xcall(
            'shopify_integration.utils.refund.get_refund_writeback_status',
            { refund_name: frm.doc.name }
        ).then(function(info) {
            if (!info) return;

            // Whether this app is the one that pays THIS refund — which
            // info.is_shopify does not answer.  That is payout_owner ==
            // OWNER_SHOPIFY, settled from a stored refund GID or the Sales
            // Order's shopify_order_id, so it is true of every refund raised
            // against a Shopify-backed order on any channel.  The channel is
            // the only thing on the document that says which route pays, and
            // it is read off frm.doc because the status endpoint returns no
            // channel at all — the unmigrated answer is {is_shopify: false,
            // migrated: false} — which is why shopify_refund_dead_end_message
            // already read it there.  See the header for what this gates and
            // what it deliberately does not.
            const dispatches_here =
                frm.doc.refund_channel === SHOPIFY_REFUND_CHANNEL;

            // Read the same way and for the same reason: the channel is the only
            // thing on the document that says which route paid, and the status
            // endpoint returns none.
            const records_a_shopify_refund =
                frm.doc.refund_channel === MANUAL_PORTAL_REFUND_CHANNEL;

            // Facts first, and above the branch below, which returns.  A stored
            // write-back status is one of this app's own fields and true on any
            // channel — and on the dead-end path it is the only sign the form
            // gives that an attempt was ever claimed against this document:
            // _claim writes Pending and commits BEFORE refundCreate is posted,
            // so a worker killed between the two leaves Pending with no GID.
            //
            // The advisory fallback inside — "Shopify: not written back" —
            // needs both halves, that this app pays this channel and that the
            // payout is Shopify's to make at all, so it is handed the
            // conjunction rather than reading either half itself.  is_shopify
            // false with no status belongs to the dead-end path, which draws no
            // indicator on purpose.
            shopify_refund_indicator(frm, info, dispatches_here && info.is_shopify);

            // Not a bare return any more.  A refund whose own channel says
            // Shopify and whose answer says is_shopify false is the one state
            // where saying nothing strands it: see
            // shopify_refund_dead_end_message, which is gated on the channel
            // and so leaves this file's contract intact on every other refund.
            //
            // `Unverified` is excepted, and the two states really do meet.  An
            // attempt whose outcome could not be read back stores no refund GID
            // — that is what Unverified means — so ownership rests on the Sales
            // Order's shopify_order_id alone.  Let that link stop reading (no
            // Sales Order on the refund, the order deleted, or an order with no
            // shopify_order_id) and check_eligibility returns
            // not_a_shopify_order from the guard that sits ABOVE its Unverified
            // guard, so the answer arrives is_shopify false for a document the
            // customer may already have been paid for.  The dead-end sentence is
            // true of it and is not the sentence it needs.  Excepted here rather
            // than inside the dead-end message because the handler returns on
            // that call, so nothing below would ever run.
            //
            // Everything below is safe in that state: the button refuses
            // Unverified by name, Check What Shopify Says sends no mutation, and
            // shopify_refund_message's first branch is the warning itself.
            if (!info.is_shopify && info.status !== 'Unverified') {
                shopify_refund_dead_end_message(frm, info);
                return;
            }

            // Everything that offers to act.  The button spends money; Check
            // What Shopify Says diagnoses a payout this app is about to make,
            // and off this channel it is about to make none.  On this channel it
            // is deliberately NOT conditional on can_write_back: a refusal is
            // when somebody most needs to see what Shopify actually reports,
            // and on a store with enable_refund_writeback off the refusal is
            // the only state there is.
            if (dispatches_here) {
                shopify_refund_button(frm, info);
                shopify_resolve_unverified_button(frm, info);
            }

            // The read-only probe, on a wider set of channels than the button.
            //
            // It went out with the false "not written back" indicator when the
            // channel gate landed, and one channel lost something real with it.
            // `Manual Portal Refund` *means* Shopify has already refunded this and
            // somebody is recording it here -- so "what does Shopify say about
            // this order" is not a claim about a payout this app is about to make,
            // which was the reason for withholding it. It is verification of the
            // fact the channel asserts, and the only way to check that assertion
            // from ERPNext.
            //
            // Still gated on `is_shopify`: without a Shopify-backed order behind
            // the refund there is no order to ask about, and the probe would fail
            // into its catch.
            if (dispatches_here || (records_a_shopify_refund && info.is_shopify)) {
                shopify_refund_targets_button(frm);
            }

            // Ungated: every state this renders — Unverified, a stored GID, a
            // stored write-back error — is a fact about this document.  Its one
            // advisory branch, the eligibility refusal, needs no gate here
            // because the server already is one: check_eligibility judges the
            // channel before the status, the amount and the store, so an
            // off-channel refund carries channel_does_not_dispatch or
            // channel_is_manual_portal_refund and can never carry a code from
            // SHOPIFY_REFUSALS_WORTH_SAYING.
            shopify_refund_message(frm, info);
        }).catch(() => null);
    }
});


// `may_promise_a_payout` is the caller's conjunction — the document's channel is
// this app's dispatch channel AND the payout is Shopify's to make — and it gates
// the fallback below and nothing else.  Passed in rather than decided here
// because both halves belong to the caller: the channel is on frm.doc, and
// is_shopify false sends it down the dead-end path, which draws no indicator of
// its own and is reached only after this function has already had its say.
function shopify_refund_indicator(frm, info, may_promise_a_payout) {
    const colours = {
        'Done': 'green',
        'Pending': 'blue',
        'Failed': 'red',
        'Skipped': 'grey',
        // Not 'red'. Red reads as "it failed, try again", and trying again is
        // the one thing that must not happen here.
        'Unverified': 'orange'
    };

    // A stored status is a fact about this document — this app's own field, held
    // by nothing else on the form — so it is drawn on any channel and in any
    // ownership state, above every gate below.  On the dead-end path it is the
    // only sign of a claim that was taken and never released.
    if (info.status) {
        frm.dashboard.add_indicator(
            __('Shopify Refund: {0}', [__(info.status)]),
            colours[info.status] || 'grey'
        );
        return;
    }

    // Everything past here is a promise that a payout is still to come, and
    // this app makes one on no channel but its own.  Every refund raised against
    // a Shopify-backed order used to get it, because the caller asked
    // info.is_shopify and nothing else: a Manual Portal Refund — which means
    // Shopify has already paid it — was told the write-back was outstanding, and
    // a Payment Portal or blank-channel refund was told so beside
    // payment_portals' own Send Refund to Portal, the button that actually pays
    // it there.
    if (!may_promise_a_payout) return;

    // Submitted Shopify refund, nothing sent yet.  Nothing will send it on its
    // own, so say that rather than leaving it looking queued.
    frm.dashboard.add_indicator(__('Shopify: not written back'), 'orange');
}


function shopify_refund_button(frm, info) {
    // A GID means Shopify has already refunded this; the server refuses anyway,
    // so don't offer a button that cannot work.
    //
    // Withholding it is no longer silent: the `reason` that came with
    // `can_write_back` is rendered by shopify_refund_message for the refusals a
    // person can act on.  See SHOPIFY_REFUSALS_WORTH_SAYING for which, and why
    // the rest stay quiet.
    if (info.refund_gid || !info.can_write_back) return;

    // Belt and braces. can_write_back is already false for an unconfirmed
    // attempt, but this state is the one where a stray retry pays the customer
    // twice, so it is refused here by name as well as by eligibility.
    if (info.status === 'Unverified') return;

    const label = (info.status === 'Failed')
        ? __('Retry Shopify Refund')
        : __('Refund in Shopify');

    frm.add_custom_button(label, function() {
        shopify_confirm_and_write_back(frm, info);
    }, __('Shopify'));
}


// The way out of `Unverified`, and the only one there is.
//
// `Unverified` means the mutation went out and this app never read the answer, so
// the customer may already have been paid.  Nothing automatic touches it -- by
// design: every trigger and both apps' send paths refuse it, because a retry
// there is a second real payout.  The exit is a person reading the Shopify order
// and saying what is on it, which `resolve_unverified_writeback` records.
//
// Until now that exit was a whitelisted endpoint with nothing calling it.  The
// headline for this state told the reader to "record what you find against this
// write-back" and gave them no way to do it -- and payment_portals now refuses to
// cancel the document as well (`cancellation_allowed` reads the write-back state
// and not only the gid, because this state carries no gid by construction).  So
// the document sat in the one state that must not be left alone, with two
// refusals and no exit, which is how a stuck payout becomes a forgotten one.
//
// This button is NOT a payout and must never become one.  It records a finding.
// `paid` demands the refund's own id from the order, because that gid is what the
// credit-note guard matches on and a `Done` row without one lets the
// refunds/create webhook build a second Credit Note; `not_paid` clears the state
// so the ordinary path may send again.  Both write who decided and which way into
// the write-back error field, because this is a decision about whether a customer
// has been paid, taken without a machine-readable answer in hand.
//
// Deliberately not a `frappe.confirm`: the two answers are not yes and no to one
// question, they are opposite statements of fact, and a dialog that makes the
// person choose the fact -- and type the evidence for one of them -- is the point.
function shopify_resolve_unverified_button(frm, info) {
    if (info.status !== 'Unverified') return;

    frm.add_custom_button(__('Record What Shopify Says'), function() {
        const dialog = new frappe.ui.Dialog({
            title: __('Resolve an unconfirmed refund'),
            fields: [
                {
                    fieldtype: 'HTML',
                    options:
                        '<p>' +
                        __('This records what you found on the Shopify order. It sends nothing to Shopify and pays nobody.') +
                        '</p><p>' +
                        __('Open the order in Shopify first — <b>Check What Shopify Says</b> lists its transactions — and answer from what is there, not from what this refund was meant to do.') +
                        '</p>'
                },
                {
                    fieldname: 'resolution',
                    fieldtype: 'Select',
                    label: __('Does a refund for this exist on the Shopify order?'),
                    // No default, and a blank first option.  The whole purpose is
                    // that somebody states which; a prefilled answer is one
                    // careless Enter away from being recorded as a finding nobody
                    // actually made.
                    options: [
                        { value: '', label: __('Choose...') },
                        { value: 'paid', label: __('Yes - a refund is on the order') },
                        { value: 'not_paid', label: __('No - there is no refund on the order') }
                    ],
                    reqd: 1
                },
                {
                    fieldname: 'shopify_refund_gid',
                    fieldtype: 'Data',
                    label: __('The refund id in Shopify'),
                    depends_on: 'eval:doc.resolution=="paid"',
                    mandatory_depends_on: 'eval:doc.resolution=="paid"',
                    description: __('The refund\u2019s numeric id or its full GID, e.g. gid://shopify/Refund/1011205505129 - taken from the refund on this order, not the order id. The server refuses anything else; it is what stops a second Credit Note being raised for the same refund.')
                },
                {
                    fieldname: 'gateway',
                    fieldtype: 'Data',
                    label: __('Gateway the refund went out by, if Shopify names one'),
                    depends_on: 'eval:doc.resolution=="paid"'
                },
                {
                    fieldname: 'note',
                    fieldtype: 'Small Text',
                    label: __('What you saw'),
                    description: __('Stored with your name against this write-back. Worth a sentence: the next reader has only this.')
                }
            ],
            primary_action_label: __('Record it'),
            primary_action: function(values) {
                if (!values.resolution) return;
                dialog.hide();
                frappe.xcall(
                    'shopify_integration.utils.refund.resolve_unverified_writeback',
                    {
                        refund_name: frm.doc.name,
                        resolution: values.resolution,
                        shopify_refund_gid: values.shopify_refund_gid || '',
                        gateway: values.gateway || '',
                        note: values.note || ''
                    }
                ).then(function(answer) {
                    // The endpoint refuses by RETURNING `ok: false` with a reason
                    // rather than by throwing -- a gid it cannot parse, a state
                    // that has moved since this form loaded -- so a caller that
                    // only reloaded would leave a form looking resolved when
                    // nothing had been written.
                    if (!answer || !answer.ok) {
                        frappe.msgprint({
                            title: __('Nothing Was Recorded'),
                            message: (answer && answer.message) || __('The write-back state could not be resolved.'),
                            indicator: 'red'
                        });
                        return;
                    }
                    frappe.show_alert({ message: answer.message, indicator: 'green' });
                    frm.reload_doc();
                }).catch(function() {
                    // A throw here is a permission refusal or a lost connection.
                    // Either way nothing was recorded, and saying so beats a
                    // dialog that closes silently on a money decision.
                    frappe.msgprint({
                        title: __('Nothing Was Recorded'),
                        message: __('The request did not reach the server, or you may not submit this Refund Request. Nothing was changed.'),
                        indicator: 'red'
                    });
                });
            }
        });
        dialog.show();
    }, __('Shopify'));
}


// Offered on every refund travelling by the Shopify refund channel, including
// ones the write-back refuses, because the refusals are exactly when somebody
// needs it — and a store with enable_refund_writeback off refuses every one
// of them.  REF-00207 failed twice on production with "no transaction
// on this order can take a refund" and there was no way to see, from ERPNext,
// which row failed which test — the response had been discarded.  This asks
// Shopify and shows the answer.
//
// Not offered off that channel, which the refresh handler decides.  It reads as
// harmless — it sends no mutation — but it is a diagnosis of a payout this app
// is about to make, and on a Bank Transfer or Manual Portal Refund it is about
// to make none, so the offer itself was the claim that it was.
//
// It sends no mutation and writes nothing.  See utils/refund.refund_targets_now.
function shopify_refund_targets_button(frm) {
    frm.add_custom_button(__('Check What Shopify Says'), function() {
        frappe.xcall(
            'shopify_integration.utils.refund.refund_targets_now',
            { refund_name: frm.doc.name }
        ).then(function(info) {
            if (!info) return;

            const rows = (info.transactions || []).map(function(t) {
                // A row nothing gave a figure for is not a row worth zero, and
                // the whole diagnosis can turn on the difference — that is how
                // the 2026-09-09 defect was finally read off this table.
                //
                // Where the figure came from matters just as much now.  Shopify
                // only reports headroom inside suggestedRefund; everything else
                // is this app's own arithmetic over the transactions, which is
                // sound but is not Shopify's word, so it says so.
                let refundable = t.refundable_reported
                    ? frappe.format(t.refundable, { fieldtype: 'Data' })
                    : '<i>' + __('not reported') + '</i>';
                if (t.refundable_source === 'derived') {
                    refundable += ' <span class="text-muted">('
                        + __('derived') + ')</span>';
                }
                const verdict = t.rejected_because
                    ? '<span style="color:var(--red-500)">' + t.rejected_because + '</span>'
                    : '<span style="color:var(--green-600)">' + __('usable') + '</span>';
                return `<tr>
                    <td>${frappe.utils.escape_html(t.kind || '')}</td>
                    <td>${frappe.utils.escape_html(t.status || '')}</td>
                    <td>${frappe.utils.escape_html(t.gateway || '')}</td>
                    <td align="right">${frappe.utils.escape_html(t.amount || '')}</td>
                    <td align="right">${refundable}</td>
                    <td>${verdict}</td>
                </tr>`;
            }).join('');

            const table = rows
                ? `<table class="table table-bordered" style="margin-top:10px">
                       <thead><tr>
                           <th>${__('Kind')}</th><th>${__('Status')}</th>
                           <th>${__('Gateway')}</th><th align="right">${__('Amount')}</th>
                           <th align="right">${__('Refundable')}</th>
                           <th>${__('Verdict')}</th>
                       </tr></thead><tbody>${rows}</tbody>
                   </table>`
                : `<p><b>${__('Shopify returned no transactions at all for this order.')}</b><br>
                      ${__('A refund needs a parent transaction to attach to, so there is nothing to refund against — this is not the same as an order that has already been refunded.')}</p>`;

            frappe.msgprint({
                title: __('Shopify Refund Targets'),
                indicator: info.would_refuse_with ? 'orange' : 'green',
                message: `<p>${frappe.utils.escape_html(info.message || '')}</p>`
                    + `<p>${__('Order')}: <b>${frappe.utils.escape_html(info.shopify_order_name || info.shopify_order_id || '')}</b>
                          &middot; ${__('Refundable total')}: <b>${frappe.utils.escape_html(String(info.refundable_total))}</b>
                          &middot; ${__('This refund')}: <b>${frappe.utils.escape_html(String(info.amount))}</b>`
                    + (info.order_refundable_total === null
                        || info.order_refundable_total === undefined
                        ? ''
                        // Shopify's own figure for the whole order — the one the
                        // admin prints. Shown beside ours so a disagreement is
                        // visible rather than having to be suspected.
                        : ` &middot; ${__('Shopify says')}: <b>${frappe.utils.escape_html(String(info.order_refundable_total))}</b>`)
                    + `</p>`
                    + table
                    + (info.suggestion_available ? '' :
                        `<p class="text-muted" style="margin-top:8px">
                            ${__('Shopify offered no suggested refund for this order, so every figure above is worked out from the transactions themselves — what was charged, less what has been given back.')}
                         </p>`),
            });
        }).catch(() => null);
    }, __('Shopify'));
}


function shopify_refund_message(frm, info) {
    const messages = [];

    // The one state on this form where the obvious action pays somebody twice,
    // so it takes the headline from payment_portals' net-refund figures and is
    // meant to: a figure describing a proposed payout is the wrong thing to be
    // reading when the payout may already have gone out.
    //
    // "Do not retry it" was the wording here, and the button is not the route
    // that matters — Shopify's own admin is.  A refund made there reaches
    // Cashfree through the OCC bridge exactly as this app's would, so it is a
    // second real payout, and nothing on this document would record it.  So
    // every route is named rather than only the button: this form — which is
    // both apps' buttons, neither of which is offered in this state anyway — and
    // the Shopify admin, which is the same set payment_portals' intro for this
    // state names one button at a time.  Neither cancelling nor retrying is
    // offered as a way out: cancelling reverses the GL entries for a payout that
    // may genuinely have happened.
    if (info.status === 'Unverified') {
        messages.push({
            text: '<b>' + __('Shopify Refund: outcome unknown') + '</b><br>'
                  + __('The refund request reached Shopify and the result could not be confirmed, so the customer may already have been paid. Do not send it again — not from this form, not by hand in the Shopify admin. Open the Shopify order, see whether a refund exists on it, then record what you find against this write-back; that answer is the only thing that decides whether this refund is paid or still to pay.')
                  + '<br>' + frappe.utils.escape_html(info.error || ''),
            colour: 'orange'
        });
        frm.dashboard.clear_headline();
        frm.dashboard.set_headline_alert(
            '<div style="padding-right:40px;display:block;line-height:1.5;">'
            + messages[0].text + '</div>',
            'orange'
        );
        return;
    }

    if (info.refund_gid) {
        let done = '<b>' + __('Shopify Refund') + ':</b> '
            + __('Sent as {0}.', [frappe.utils.escape_html(info.refund_gid)]);
        if (info.gateway) {
            done += ' ' + __('Gateway: {0}.', [frappe.utils.escape_html(info.gateway)]);
        }
        messages.push({ text: done, colour: 'green' });
    }

    if (info.error) {
        messages.push({
            text: '<b>' + __('Shopify Refund') + ':</b> '
                  + frappe.utils.escape_html(info.error),
            colour: info.status === 'Failed' ? 'red' : 'orange'
        });
    }

    // Why there is no button, when nothing above has accounted for its absence.
    //
    // `can_write_back` decided the button and the `reason` that arrived with it
    // was dropped on the floor.  On a store with Refund Write-Back switched off,
    // or with no Admin API token, that left the orange "not written back"
    // indicator, no button and not one word about either — while
    // payment_portals' Approved intro for this channel sent the reader here for
    // the payout.  Survivable while that app still offered its own send button
    // on this channel; since `sent_by_this_app` withheld that one too, this is
    // the only route, and a silent refusal is a refund nobody can move.  That
    // intro no longer names a button as a certainty — it says the button appears
    // only where the store is set up for write-back, and that where it is not,
    // this app's own message on the form says so.  This is that message, and on
    // the live configuration, where no store is set up, it is the state every
    // Shopify-channel refund is in rather than a corner of one.
    //
    // Gated on `messages.length` rather than on the two fields it stands in for:
    // a GID or a stored error is a better account of the same document than an
    // eligibility refusal is — `already_paid` is *only* ever the GID restated —
    // and anything added above later inherits the same precedence for free.
    //
    // `reason` is escaped, like the other server strings this file interpolates
    // into a message — info.error, the GID, the gateway.  Not "every cell of the
    // targets table", which is what this comment used to claim: four of that
    // table's six cells go through escape_html, the Refundable cell goes through
    // frappe.format, and the Verdict cell interpolates t.rejected_because raw.
    // That last one is safe for a reason that does not generalise to here —
    // rejection_reason returns one of three fixed slugs ('kind', 'status',
    // 'no_headroom') and never a value read off the order.  The Refundable
    // cell's "(derived)" suffix is the same kind of thing: it is a literal in
    // this file, chosen by comparing refundable_source against a constant, and
    // no part of the server's value reaches the markup.  `reason` has no such
    // closed vocabulary: these sentences are built out of document values a
    // person types — the store domain, the Sales Order name, the channel string
    // — so escaping is the right default whether or not a reason_code composes
    // markup today.
    //
    // Rendered as a dashboard comment, and NOT as the headline.  This is the
    // module-wide rule from the file header applied at its first hard case: the
    // headline already holds payment_portals' net-refund figures, put there
    // synchronously by render_summary, and anything set from inside this
    // xcall().then() replaces them.  Trading the money the form is about for a
    // sentence explaining a missing button is the wrong way round, and
    // add_comment says the same thing beside the figures instead of on top of
    // them.
    //
    // The split falls between advice and alarm, and this branch is advice: it
    // says "nobody can send this from here yet", which does not outrank the net
    // figure of a refund that is still only proposed.  Unverified, a stored GID
    // and a stored write-back error all say the customer's money has moved or
    // may have moved, so they keep the headline — see the branches above and the
    // shared render at the foot of this function.
    if (!messages.length
        && !info.can_write_back
        && info.reason
        && SHOPIFY_REFUSALS_WORTH_SAYING.includes(info.reason_code)) {
        // "nothing on this form" and not "nothing else", which would overclaim:
        // payment_portals' server-side storefront dispatcher still exists and
        // would refuse this refund for the same reason, but the only thing that
        // ever calls it is the Send button `sent_by_this_app` withholds on this
        // channel.  From this form there is no route at all while this stands.
        frm.dashboard.add_comment(
            '<b>' + __('Shopify Refund: not sent') + '</b><br>'
            + frappe.utils.escape_html(info.reason)
            + '<br>' + __('That is why there is no <b>Refund in Shopify</b> button, and nothing on this form will send this refund either — Payment Portals withholds its own send button on this channel and the 15-minute sweep skips it.'),
            'orange',
            true
        );
        return;
    }

    if (!messages.length) return;

    const rank = { green: 1, orange: 2, red: 3 };
    const colour = messages.reduce(
        (worst, m) => (rank[m.colour] > rank[worst] ? m.colour : worst),
        messages[0].colour
    );

    frm.dashboard.clear_headline();
    frm.dashboard.set_headline_alert(
        '<div style="padding-right:40px;display:block;line-height:1.5;">'
        + messages.map(m => m.text).join('<br>')
        + '</div>',
        colour
    );
}


// A refund routed through Shopify that this app cannot act on at all — the one
// thing rendered when `is_shopify` comes back false and the write-back status is
// not `Unverified`.  That exception is in the refresh handler, with the reason.
//
// `is_shopify` is `payout_owner == OWNER_SHOPIFY`, which is true only once a
// refund GID or the Sales Order's `shopify_order_id` exists.  So it is false, on
// a refund whose own `refund_channel` still says Shopify, whenever the link to
// the order cannot be read: no Sales Order on the refund, the Sales Order
// deleted, or a Sales Order with no `shopify_order_id` — which is what
// utils/sales_order.clear_shopify_fields_on_amend leaves behind when somebody
// duplicates a Shopify order by hand.
//
// NOT an amendment, which several comments in this repo and the next say it is.
// That hook returns early on `amended_from` for the express purpose of letting
// an amended copy keep the Shopify fields, and the cancelled original keeps its
// own — so no code in this app takes the order id off an amended Sales Order.
// The state is real; the mechanism named for it is not, and it sends the next
// reader to look at the wrong field.
//
// The refresh handler returned on that and drew nothing whatsoever: no
// indicator, no message, no button, on a channel where payment_portals offers
// no send button either (`sent_by_this_app`) and the 15-minute sweep skips the
// refund by name — `settled_elsewhere` in that app's actions/refund_execution,
// because there is no cf_refund_id to ask a gateway about.  So silence was
// somebody waiting on a payout with no route left to make it.
//
// The module's contract is intact: this is gated on the channel and on nothing
// else, so a refund that is not travelling by the Shopify channel still gets
// nothing from this function — see the header for the write-back facts that
// render outside it.
function shopify_refund_dead_end_message(frm, info) {
    // Read off the document, not off `info`.  The unmigrated answer is
    // {is_shopify: false, migrated: false} — no channel, no reason, no
    // reason_code — and it is one of the two states this exists to explain.
    if (frm.doc.refund_channel !== SHOPIFY_REFUND_CHANNEL) return;

    // _NOT_MIGRATED_REASON, restated because that answer arrives without it.
    // Worth saying only here: on any other channel an unmigrated site is this
    // app being inert, which is the intended behaviour and nobody's problem.
    const reason = (info.migrated === false)
        ? __('Refund Request is missing the Shopify write-back fields. Run `bench --site <site> migrate`.')
        : (info.reason || '');

    // No reason means no diagnosis, and a note that only says something is
    // wrong is worse than the silence this falls back to.  This path draws no
    // indicator of its own, and on a document with no stored status the handler
    // drew none either, so returning here really does leave the form as
    // payment_portals rendered it.
    if (!reason) return;

    // A recorded write-back attempt, on a document this app can no longer act
    // on.  `reason` does not cover it: that describes the ownership answer, not
    // this document's history.
    //
    // "No money has moved on either of the two states this explains" was the
    // justification for the dashboard comment below, and it is false.  The
    // counterexample is in this same module: `_claim` writes
    // shopify_writeback_status = Pending and COMMITS before refundCreate is
    // posted, so that other workers can see the claim — and a worker killed
    // between the two leaves Pending with no GID.  Let that document also lose
    // its order link and check_eligibility answers not_a_shopify_order, from the
    // guard above its Unverified guard, so nothing downgrades the answer and it
    // arrives here with the customer possibly paid.
    //
    // Named, then, and not left to `reason` — but named without asserting a
    // payout, because Pending is equally consistent with a worker that died
    // before posting anything, and because it is not the only status that
    // reaches here.  Skipped (_record_skip, after a refusal that sent nothing)
    // and Failed (_release_claim from fail_unsent, whose whole contract is that
    // nobody was paid — the field's own description says "nothing was sent, safe
    // to retry") both do, and on those the sentence would be alarming if it
    // asserted a payout.  So it says what is recorded and asks for the order to
    // be read.  Done cannot reach here at all: it implies a GID, and a GID makes
    // the payout Shopify's, which is is_shopify true.
    //
    // info.status goes through __() unescaped, like the indicator's, because it
    // is a Select field of this app's own — install.py gives it a blank plus
    // Pending, Done, Failed, Skipped, Unverified and nothing else.
    const attempted = info.status
        ? '<br>' + __('A Shopify write-back attempt is recorded against this refund as {0}, with no Shopify refund id. Open the Shopify order and see whether a refund exists on it before treating this refund as unpaid.', [__(info.status)])
        : '';

    // Escaped for the reason given in shopify_refund_message.  No button either:
    // the server has refused, and neither a missing order link nor an unmigrated
    // site is fixed by a click.
    //
    // A dashboard comment, for the reason given there — and this path is where
    // the headline grab did the most damage, because it is not a rare state on
    // an unmigrated site, it is EVERY submitted Shopify-channel refund on one.
    // Each of them lost its net-refund figures to a `bench migrate` instruction
    // addressed to somebody who is not reading this form.  The sentence above is
    // what the false justification was covering for, and it stays a comment
    // rather than taking the headline: the loud lane is for a document whose own
    // fields say a mutation went out, which is what Unverified means and what
    // the refresh handler excepts by name to route it there.  A claim is not
    // that, and the reader is not left without a signal either way — the handler
    // now draws the stored status as an indicator before this branch is reached.
    frm.dashboard.add_comment(
        '<b>' + __('Shopify Refund: this app cannot send it') + '</b><br>'
        + frappe.utils.escape_html(reason)
        + '<br>' + __('The refund channel still says Shopify, so nothing on this form pays this refund either — the sweep skips this channel and Payment Portals offers no send button on it.')
        + attempted,
        'orange',
        true
    );
}


function shopify_confirm_and_write_back(frm, info) {
    // This is a payout, not a record.  The dialog says so in those words,
    // because "write back to Shopify" sounds like bookkeeping and is not.
    // Refund Request has no currency field — its Currency fields are in company
    // currency, which is what format_currency falls back to.
    const amount = format_currency(info.amount);

    let warning = '<p>' + __('This asks Shopify to refund {0} on order {1}.',
        [amount, frappe.utils.escape_html(info.shopify_order_id)]) + '</p>'
        + '<p><b>' + __('This pays the customer.') + '</b> '
        + __('It is a payment instruction, not a record of one. Do not send it if the refund has already been paid by another route.')
        + '</p>';

    frappe.confirm(
        warning,
        function() {
            frappe.dom.freeze(__('Refunding in Shopify…'));
            frappe.xcall(
                'shopify_integration.utils.refund.writeback_now',
                { refund_name: frm.doc.name }
            ).then(function(result) {
                frappe.dom.unfreeze();
                if (result && result.ok) {
                    frappe.show_alert({
                        message: __('Refunded in Shopify'),
                        indicator: 'green'
                    });
                } else if (result && result.possibly_paid) {
                    // "Not sent" would be a lie here, and acting on it — trying
                    // again — is what pays the customer twice.
                    frappe.msgprint({
                        title: __('Refund May Have Been Sent'),
                        message: '<p><b>'
                            + __('Do not retry this refund.')
                            + '</b> '
                            + __('The request reached Shopify and the outcome could not be confirmed, so the customer may already have been paid. Check the Shopify order and record what you find.')
                            + '</p><p>'
                            + frappe.utils.escape_html(result.message || '')
                            + '</p>',
                        indicator: 'orange'
                    });
                } else {
                    frappe.msgprint({
                        title: __('Refund Not Sent'),
                        message: frappe.utils.escape_html(
                            (result && result.message) || __('Unknown error')
                        ),
                        indicator: 'red'
                    });
                }
                frm.reload_doc();
            }).catch(function() {
                frappe.dom.unfreeze();
            });
        }
    );
}
