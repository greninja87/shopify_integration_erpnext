"""
refund.py — Write ERPNext refunds back to Shopify.

Refunds are raised in ERPNext (payment_portals → Refund Request).  Shopify knows
nothing about them, so an order that has been fully refunded here still shows
its whole value as refundable there — and the next person to open it in the
admin can refund it a second time.  This module tells Shopify, once the ERPNext
refund is booked.

This is a payment instruction, not a record
-------------------------------------------
A successful refundCreate **pays the customer**.  That is easy to get backwards,
because of how the orders on this store are built.  Cashfree-OCC-Notdrones
creates the order and marks it paid *manually*, so Shopify holds no gateway
transaction of its own and logs the refund against the "manual" gateway.  The
wording invites the conclusion that nothing moved.  It is wrong: production
Gateway Transaction data shows a real Cashfree refund behind every one of these,
#6491's landing in the same minute as the Shopify action.  The OCC app is
bridging Shopify's refund into a real Cashfree refund.

So never read "manual gateway", or a timeline reading "manually marked as
refunded", as evidence that no money moved.  Treat every call here as a payout.

What that means for the code: read the gateway off the order's own parent
transaction and copy it verbatim.  Never hardcode "manual".  That is what
Shopify's own admin does, and it is the shape the OCC bridge recognises.
`shopify_refund_gateway` on the Refund Request records which gateway was used,
so nobody has to guess later.

The double-refund hazard
------------------------
Because Shopify's refund reaches Cashfree by itself, one refund must never go out
through both this write-back and payment_portals' own Cashfree refund call — that
pays the customer twice.  The split is by order origin: a refund whose Sales Order
carries a shopify_order_id is Shopify's payout, and a payment-link or direct
Cashfree payment stays with the Cashfree API.

That routing gate lives in payment_portals (portal_channel_blocked) and is not
this app's to write.  It still refuses a *Payment Portal* refund on a Shopify
order; what changed in 3bc6164 is that it now points the reader at the new
"Shopify" refund channel, which delegates here, instead of at a manual refund in
the Shopify admin.

Two callers reach write_back_refund, and both are deliberate:

  * payment_portals' send step, through the refund_payout_dispatchers hook
    registered in hooks.py.  It arrives with status Queued.
  * writeback_now, the whitelisted endpoint behind the form button.  It arrives
    with status Approved.

write_back_refund itself is NOT whitelisted, so registering the hook opened no
HTTP door — a payout one call away from anyone logged in is exactly what that
protects against.  See REFUND-DISPATCH-CONTRACT.md §2 and §2b.

Snapmint needs no guard here.  The discriminator is not visible in the
transaction nodes — OCC and Snapmint orders both read "manual" — and it does not
have to be: payment_portals decides it upstream from
Refund Request.portal_account -> provider, where SnapmintProvider.supports_refunds
is False and portal_channel_blocked already refuses the portal channel outright.
A Snapmint refund therefore never reaches this module on the portal channel, and
one recorded as Bank Transfer or Manual Portal Refund is skipped by the guards
below.

Coupling
--------
Refund Request belongs to payment_portals, and this module never imports it.
Everything goes through frappe.db / frappe.get_doc by doctype name, so
shopify_integration stays installable on a site without payment_portals, and
every entry point is inert when the doctype is absent.

The shape asymmetry
-------------------
order.transactions takes `first:` but appears to return a plain list, while
refund.transactions on the mutation result is a connection with edges/node.
Both forms appear in the official examples and neither could be verified against
a live response, so transaction_nodes() accepts list, {"nodes": [...]} and
{"edges": [{"node": ...}]}.  Guessing wrong here fails silently as an empty
list, which would read as "no refundable parents".

Layout follows utils/fulfillment.py: pure decision functions first, testable
with no bench, then the frappe-bound orchestration.
"""

import re

import frappe
from frappe.utils import add_to_date, cint, flt, now_datetime

from shopify_integration.utils.shopify_api import ShopifyAPIError, has_admin_api_credentials
from shopify_integration.utils.shopify_graphql import (
    ShopifyUserError,
    check_user_errors,
    execute,
    gid,
)

REFUND_REQUEST = "Refund Request"

# Bumped when the shape of write_back_refund's result dict changes in a way a
# caller has to care about.  See REFUND-DISPATCH-CONTRACT.md.
#
# 2: owns_payout became three-state and its truth table changed.  In 1 it was
#    bool(shopify_order_id), which several guards never populated, so it read
#    False — "not a Shopify order" — for refunds that were, including ones
#    Shopify had already paid.  Callers should branch on caller_must_pay.
# 3: the no_permission code is gone, and with it the submit-permission check in
#    write_back_refund.  Two permission models guarding one payout deadlocked:
#    a caller payment_portals had authorised could still be refused here, and
#    the resulting "unknown" left neither app willing to pay.  Authorisation now
#    belongs to the caller; writeback_now still guards the HTTP door.
# 4: the state this accepts is inverted.  Until now it demanded status
#    "Completed" — booked and paid — on the reasoning "Shopify is told once
#    ERPNext has booked the refund, not before".  That is write-back ordering and
#    it contradicts §0 of this app's own contract: a successful refundCreate PAYS
#    the customer.  payment_portals sets Completed only when the Payment Entry is
#    posted, so book-then-call paid the customer AFTER ERPNext had recorded
#    paying them.  Dispatch now runs at Approved/Queued on the new "Shopify"
#    refund channel, mirroring the Cashfree send path in that app, and the gate
#    is a positive allow-list on the channel — which also closes a hazard the old
#    one left open (a Bank Transfer refund on a Shopify order was accepted, and
#    writing that back pays a NEFT refund a second time through the OCC bridge).
#    Two new refused codes: channel_does_not_dispatch, already_booked.
# 5: at most one post of refundCreate can ever have EXECUTED —
#    execute(..., idempotent=False).  Stated that way deliberately: it is NOT
#    "one post".  One failure is still re-POSTED, up to five times — Shopify's
#    own THROTTLED 200-body in the shape that says it refused the document
#    before execution began — and that is safe because every one of those posts
#    was refused, not because there was one.  The guarantee rests on the
#    refusal, not on the count, and a reader who takes "posted once" as the
#    headline is reasoning from something false; the likeliest thing they then
#    do with it is restore the ordinary retries.
#    No key in the result changed shape; what changed is what two of them MEAN.
#    The post-send failed_unsent verdicts (userErrors -> rejected_by_shopify,
#    401/403 -> not_authorised) each read one response and told the caller
#    "nobody was paid, safe to retry".  Under the ordinary retries that response
#    was the last of up to five attempts that could each have run, so the
#    verdict was hopeful rather than sound: attempt 1 can create the refund,
#    lose its answer to the socket timeout, and attempt 2 be declined for
#    exceeding the refundable amount PRECISELY because attempt 1 consumed it —
#    reported here as retry-safe, on a refund the customer had already been
#    paid.  With only one post able to have executed, those verdicts describe
#    the only request that could have run, and they are true.
#    One new reason_code, inside the existing failed_unsent outcome:
#    rate_limited, for an exhausted pre-execution THROTTLED refusal after the
#    post.  That is Shopify refusing to run the document, and it used to be
#    filed as failed_unknown — which left Shopify's ordinary cost limiting
#    sitting in Unverified for a person to clear by hand.  A bare HTTP 429 is
#    deliberately NOT in it: see the code's own comment, and §7a of the
#    contract.  A caller that branches on reason_code should accept the slug as
#    one more retry-safe one; one that branches on outcome alone needs no
#    change at all.
CONTRACT_VERSION = 5

# Refund Request state fields, created by this app as Custom Fields.
REFUND_GID_FIELD      = "shopify_refund_gid"
WRITEBACK_STATUS_FIELD = "shopify_writeback_status"
REFUND_GATEWAY_FIELD  = "shopify_refund_gateway"
WRITEBACK_ERROR_FIELD = "shopify_writeback_error"
WRITEBACK_AT_FIELD    = "shopify_writeback_at"

STATUS_PENDING = "Pending"
STATUS_DONE    = "Done"
STATUS_FAILED  = "Failed"
STATUS_SKIPPED = "Skipped"

# The refund was sent and its fate is unknown — the customer may or may not have
# been paid.  Deliberately NOT "Failed": a Failed row invites a retry, and
# retrying a refund that already went through pays the customer twice.  Nothing
# retries this state; it is cleared by a person via
# resolve_unverified_writeback() after checking the order in Shopify.
STATUS_UNVERIFIED = "Unverified"

# ── The outcome vocabulary ───────────────────────────────────────────────────
#
# write_back_refund's `status` is the document state.  `outcome` is the caller's
# contract, and it exists because "Failed" is not precise enough for something
# that moves money: a caller has to know whether nobody was paid (retry) or
# whether it cannot tell (reconcile, never retry).  See
# REFUND-DISPATCH-CONTRACT.md.
OUTCOME_PAID           = "paid"            # Shopify accepted; refund_gid is set
OUTCOME_REFUSED        = "refused"         # not sent; nobody paid; config/data
OUTCOME_FAILED_UNSENT  = "failed_unsent"   # not sent; nobody paid; safe to retry
OUTCOME_FAILED_UNKNOWN = "failed_unknown"  # SENT, fate unknown; POSSIBLY PAID
OUTCOME_IN_PROGRESS    = "in_progress"     # another worker holds the claim

# Only one outcome is ever safe to retry automatically.
_RETRY_SAFE_OUTCOMES = (OUTCOME_FAILED_UNSENT,)

# ── Who owes this customer the money ─────────────────────────────────────────
#
# Three states, not two.  The first version of this was a bool derived from
# shopify_order_id, and it was wrong on the live site: several guards return
# before the Sales Order is ever looked up, so a blank order id there meant "not
# determined" and read as "not a Shopify order".  A Manual Portal Refund — a
# refund Shopify has ALREADY paid — came back as the caller's to pay, which is
# the double payout this contract exists to prevent.
#
# So ownership is settled before any guard that could return without looking,
# and "we could not tell" is its own answer rather than being folded into "no".
OWNER_SHOPIFY = "shopify"   # Shopify's payout; it may already have made it
OWNER_CALLER  = "caller"    # not a Shopify order; the caller pays it
OWNER_UNKNOWN = "unknown"   # undeterminable (app not installed, no such document)

# The ONLY reason_code that means "not mine, you pay it".  A constant because
# the contract's routing rule is a biconditional with it, and
# tests/test_refund_contract.py enumerates REASON_CODES to pin that.
REASON_NOT_OURS = "not_a_shopify_order"

# The closed reason_code vocabulary, per outcome, and the single source of truth
# for REFUND-DISPATCH-CONTRACT.md — tests/test_refund_contract.py asserts the
# document lists every code here and that nothing is emitted from outside it.
# It is closed because payment_portals branches on these slugs to decide whether
# a customer might already have been paid.
REASON_CODES = {
    OUTCOME_PAID: frozenset({""}),
    OUTCOME_REFUSED: frozenset({
        "already_paid",
        "not_a_shopify_order",
        "channel_is_manual_portal_refund",
        # The channel pays this refund some other way — Cashfree's own API, or a
        # bank transfer — so telling Shopify to pay it as well pays the customer
        # twice.  Distinct from channel_is_manual_portal_refund, which means
        # Shopify has already paid it.
        "channel_does_not_dispatch",
        "wrong_refund_status",
        # Completed with a Payment Entry: ERPNext has booked this refund, so a
        # payout now would land after its own record.  Its own code because a
        # booking with no GID means something paid it outside this flow, which
        # is a person's problem and not a status to correct.
        "already_booked",
        "not_submitted",
        "nothing_to_refund",
        "writeback_unavailable_for_store",
        "no_api_credentials",
        "not_installed",
        "refund_request_missing",
        # Reserved for the optional expected_amount cross-check on
        # write_back_refund, which is not built yet — see the contract's §9.2,
        # including the expected_amount_checked acknowledgement it cannot ship
        # without.
        "amount_mismatch",
    }),
    OUTCOME_FAILED_UNSENT: frozenset({
        "query_failed",
        "shopify_order_not_found",
        "insufficient_refundable",
        "no_refundable_transactions",
        "rejected_by_shopify",
        "not_authorised",
        # Shopify's own THROTTLED 200-body, in the shape that says it refused
        # the document BEFORE execution began, outliving execute()'s retries
        # after the mutation was posted — and only where the client vouched for
        # it with proves_not_executed.  Shopify answered instead of running the
        # document, so nobody was paid.  Two things that carry the same
        # extensions.code are NOT this code: a THROTTLED body that shows
        # execution began, which may hide a committed mutation, and a bare HTTP
        # 429, which says a rate limiter refused the request without saying
        # WHICH one — Shopify throttles GraphQL with a 200 body, so a 429 here
        # may come from a CDN or a proxy in front of the store.  Both are
        # response_unverifiable below.  Until CONTRACT_VERSION 5 the refusal
        # fell into failed_unknown too, which parked Shopify's ordinary cost
        # limiting in Unverified — a state nothing retries and only a person can
        # clear, by opening the order in Shopify to decide whether a customer
        # had been paid.  That is a great deal of hand work for a request
        # Shopify said it had not run.
        "rate_limited",
        "setup_failed",
    }),
    OUTCOME_FAILED_UNKNOWN: frozenset({
        # The two possibly-paid codes split on whether Shopify ANSWERED, which
        # is the only evidence available and is what the contract's §6 table
        # says each of them means.  No status_code on the exception -> the
        # transport failed and nothing was read back.  A status_code -> Shopify
        # answered something unusable: an HTTP 400/404/5xx, or a
        # 200-with-errors that was not a proven refusal.  Both are "a person
        # must look" and neither is ever retried, so the split changes where a
        # reader is sent, not what anybody may do — but it was filing every
        # non-refusal post-send failure under "transport error", which told
        # that reader the network had broken when Shopify had replied in full.
        "transport_error_after_send",
        "response_unverifiable",
        "unverified_previous_attempt",
    }),
    OUTCOME_IN_PROGRESS: frozenset({"claimed_elsewhere"}),
}

# ── Which refund this app may pay, and in what state ────────────────────────
#
# The one refund channel that dispatches a payout through Shopify.  Added to
# payment_portals in 3bc6164 precisely so the dispatch case is identifiable
# without inference: before it, a Payment Portal refund on a Shopify-backed
# order was simply refused and a person was told to go and refund it by hand.
#
# An ALLOW-LIST rather than a set of exclusions, and for the same reason
# caller_must_pay is a positive flag: the dangerous action needs a positive
# assertion, so an unrecognised channel — including one a later payment_portals
# adds — refuses on its own instead of paying.  The old gate excluded only
# CHANNEL_FROM_SHOPIFY, which left Bank Transfer accepted: money already sent by
# NEFT, written back to Shopify, paid a second time through the OCC bridge.
CHANNEL_DISPATCH = "Shopify"

# A refund that came *from* Shopify is already recorded there; writing it back
# would duplicate it.  NOT the same as CHANNEL_DISPATCH and the two must never be
# conflated: this one records a refund Shopify made, that one asks Shopify to
# make one.
CHANNEL_FROM_SHOPIFY = "Manual Portal Refund"

# The Refund Request statuses a payout may be dispatched from.
#
# Both, and only both.  `Approved` is payment_portals' SENDABLE_STATUSES in full
# — what a person sees on the form and what a refusal returns the document to —
# and `Queued` is what send_refund_to_portal commits before enqueueing the job,
# so a dispatched payout arrives here on Queued and never on Approved.  Omitting
# either breaks one of the two callers.
#
# DO NOT "simplify" this to {"Approved"} on the strength of reading
# execute_queued_refund.  That job passes
#
#     status="Approved" if refund.status == "Queued" else refund.status
#
# to its own sending_allowed re-check — an argument normalisation, and it is
# never written back.  The stored status is still `Queued` when
# _dispatch_to_storefront calls in here, so a reader who takes that line as
# evidence the document has been promoted would refuse every dispatched payout
# while the form button carried on working.  Confirmed from that side
# 2026-09-07.  tests/test_refund_dispatch_gate.py fails if either state is
# dropped.
#
# Everything else refuses, including:
#   Processing  a call has already gone out; a GID or an Unverified row is the
#               record of it, and a second send is a second payout
#   Failed      not re-sendable in payment_portals either
#   Completed   ERPNext has already booked it — see already_booked
DISPATCHABLE_STATUSES = frozenset({"Approved", "Queued"})

# Transaction kinds a refund can attach itself to.  An AUTHORIZATION has taken
# no money, a VOID has given it back already, and a REFUND row is the result of
# a refund rather than something to refund.
_PARENT_KINDS = ("SALE", "CAPTURE")

# A Pending claim older than this is assumed abandoned (worker killed
# mid-request) and becomes eligible again.  Mirrors fulfillment.py.
STALE_CLAIM_MINUTES = 30

_NOT_MIGRATED_REASON = (
    f"{REFUND_REQUEST} is missing the Shopify write-back fields. "
    f"Run `bench --site <site> migrate`."
)


# ── GraphQL documents ─────────────────────────────────────────────────────────

_REFUND_TARGETS_QUERY = """
query RefundTargets($orderId: ID!) {
  order(id: $orderId) {
    id
    name
    transactions(first: 20) {
      id
      kind
      status
      gateway
      formattedGateway
      amountSet { presentmentMoney { amount currencyCode } }
      maximumRefundableV2 { amount currencyCode }
      parentTransaction { id }
    }
  }
}
"""

_REFUND_CREATE_MUTATION = """
mutation PortalRefundWriteBack($input: RefundInput!) {
  refundCreate(input: $input)%(directive)s {
    refund {
      id
      note
      totalRefundedSet { presentmentMoney { amount currencyCode } }
      transactions(first: 10) {
        edges { node { id gateway kind status
                       amountSet { presentmentMoney { amount } } } }
      }
    }
    userErrors { field message }
  }
}
"""


# ── Money ─────────────────────────────────────────────────────────────────────

def _paise(value) -> int:
    """
    An amount as an integer number of minor units.

    Allocation is done in integers throughout.  Splitting 46952.16 across two
    parents in floats leaves a fraction of a paisa behind, which then renders as
    "952.1599999999999" in the mutation — Shopify rejects it, and the failure
    looks like a mystery.
    """
    try:
        return int(round(float(value or 0) * 100))
    except (TypeError, ValueError):
        return 0


def _money(paise: int) -> str:
    """Minor units back to the decimal string Shopify's Money scalar wants."""
    return f"{paise / 100:.2f}"


# ── Pure decisions ────────────────────────────────────────────────────────────

def transaction_nodes(container) -> list:
    """
    Transaction nodes out of whichever shape Shopify returned.

    Accepts a plain list, {"nodes": [...]} and {"edges": [{"node": ...}]}, for
    the reason in the module docstring: the two transaction fields we touch do
    not agree, and a wrong guess reads as an empty list rather than an error.
    """
    if isinstance(container, dict):
        if isinstance(container.get("nodes"), list):
            rows = container["nodes"]
        elif isinstance(container.get("edges"), list):
            rows = [
                (edge or {}).get("node")
                for edge in container["edges"]
                if isinstance(edge, dict)
            ]
        else:
            rows = []
    elif isinstance(container, list):
        rows = container
    else:
        rows = []

    return [row for row in rows if isinstance(row, dict)]


def _headroom(node) -> int:
    """maximumRefundableV2 in minor units — how much this parent has left."""
    return _paise(((node.get("maximumRefundableV2") or {}).get("amount")))


def rejection_reason(node) -> str:
    """Which test disqualified this transaction as a refund parent, or "".

    One function so the filter, the log and the diagnostic can never disagree
    about why a row was dropped — which is exactly what happened on `REF-00207`:
    the rows were examined, the verdict was recorded as a sentence that did not
    fit them, and the rows themselves were discarded.

    Order matters and follows `refundable_parents`: `kind` first, because an
    AUTHORIZATION or a REFUND row is not a parent whatever its status; then
    `status`; then headroom, which is the only one that can change over time.
    """
    if (node.get("kind") or "").upper() not in _PARENT_KINDS:
        return "kind"
    if (node.get("status") or "").upper() != "SUCCESS":
        return "status"
    if _headroom(node) <= 0:
        return "no_headroom"
    return ""


def _headroom_reported(node) -> bool:
    """Did Shopify actually give us a maximumRefundableV2 amount?

    `_headroom` cannot answer this: `_paise(None)` is 0, so a field Shopify did
    not populate and one it reported as 0.00 are the same number by the time the
    filter sees them — and they mean opposite things.  A reported zero is a fact
    about the order (nothing left to refund).  An absent one is a fact about
    *this app* (we are reading a field this API version does not fill for this
    transaction), and in that case no order is ever refundable anywhere, which
    presents as every order being fully refunded.

    Kept separate from `rejection_reason` on purpose: the filter must treat both
    as no-headroom — refusing to refund on a number we do not have is the safe
    direction — while a person reading the log has to be able to tell which it
    was.
    """
    money = node.get("maximumRefundableV2")
    if not isinstance(money, dict):
        return False
    amount = money.get("amount")
    return amount is not None and str(amount).strip() != ""


def transaction_summary(nodes) -> list:
    """Every transaction Shopify returned, with the verdict on each.

    Deliberately small and deliberately free of anything about a person: this
    goes into Shopify Log, which many people can read, and into a whitelisted
    diagnostic.  Ids, kinds, statuses, gateways and figures answer "why did
    every row fail" completely; nothing else is needed for it.

    `gateway` is reported rather than judged.  It is `manual` on some of these
    orders and a named gateway such as `CASHFREE - UPI` on others, and nothing
    in this module branches on it — `plan_refund` copies whatever is there onto
    the refund verbatim.  Reading a gateway name as evidence about whether money
    moved is the misreading that produced two wrong conclusions on this project.
    """
    return [
        {
            "id": str(node.get("id") or ""),
            "kind": str(node.get("kind") or ""),
            "status": str(node.get("status") or ""),
            "gateway": str(node.get("gateway") or ""),
            "amount": _money(
                _paise(((node.get("amountSet") or {})
                        .get("presentmentMoney") or {}).get("amount"))
            ),
            "refundable": _money(_headroom(node)),
            "refundable_reported": _headroom_reported(node),
            "rejected_because": rejection_reason(node),
        }
        for node in transaction_nodes(nodes)
    ]


def refundable_parents(nodes) -> list:
    """
    Parent transactions a refund can attach to, best first.

    Keeps kind in {"SALE", "CAPTURE"} with status "SUCCESS" and
    maximumRefundableV2.amount > 0.  Everything else — REFUND rows, VOID,
    FAILURE, AUTHORIZATION with nothing captured — is not a parent.

    "Best first" is largest headroom first, so a refund is spread over as few
    transactions as possible.  Shopify's per-row cap is the authority on how
    much each one can take, not the amount originally charged: a row that has
    been partly refunded already still reports its full amountSet.
    """
    parents = [
        node
        for node in transaction_nodes(nodes)
        if (node.get("kind") or "").upper() in _PARENT_KINDS
        and (node.get("status") or "").upper() == "SUCCESS"
        and _headroom(node) > 0
    ]
    parents.sort(key=_headroom, reverse=True)
    return parents


def plan_refund(nodes, amount) -> dict:
    """
    Allocate `amount` across refundable parents, capped per parent by its
    maximumRefundableV2.

    :return: {"transactions": [{"parentId", "kind", "gateway", "amount"}],
              "gateways": [gateway names actually allocated, in order],
              "allocated": float, "problem": str | None,
              "problem_code": str}

    `problem_code` is a reason_code from the published vocabulary, because the
    caller hands it straight to payment_portals.  It exists because three
    different refusals used to share one slug: an order with no refundable rows
    at all was reported as merely short of headroom, and the documented
    `no_refundable_transactions` was unreachable.

    `gateways` carries only the gateways that were actually allocated against —
    a parent left untouched contributes nothing, however large its headroom.
    There is deliberately no "does this move money" flag derived from those
    names: on these orders "manual" is the normal gateway *and* the customer
    gets paid, via the Cashfree-OCC bridge, so such a flag is false comfort.
    The gateway names themselves are recorded on the Refund Request, which is
    the whole of what is knowable here.

    Allocates nothing and sets `problem` when the parents' combined headroom is
    short of `amount`: a partial Shopify record is worse than none, because it
    looks settled and is not.  The message names both figures, since the only
    useful next question is "short by how much".
    """
    def empty(problem, problem_code):
        return {"transactions": [], "gateways": [], "allocated": 0.0,
                "problem": problem, "problem_code": problem_code}

    wanted = _paise(amount)
    parents = refundable_parents(nodes)
    available = sum(_headroom(p) for p in parents)

    if wanted <= 0:
        return empty(
            f"The refund amount is {_money(wanted)} — there is nothing to record "
            f"in Shopify.",
            "nothing_to_refund",
        )

    if not parents:
        # Two different facts, one code, and they were reported with the same
        # sentence until 2026-09-08.  REF-00207 on production refused with "every
        # row is a refund, a void, unsuccessful, or already fully refunded" for
        # an order whose ERPNext side showed one successful ₹5 payment and no
        # refund at all — so the message asserted rows that may not have
        # existed, and the two readings have opposite remedies: pick another
        # order, versus this store cannot be refunded this way at all.
        #
        # The reason_code stays `no_refundable_transactions` for both, because
        # the caller's decision is identical (nothing was sent, Shopify still
        # owns the payout) and splitting it would be a CONTRACT_VERSION bump for
        # no behavioural gain.  It is the human sentence that has to be honest.
        rows = transaction_nodes(nodes)
        if not rows:
            return empty(
                f"Shopify holds no transactions on this order at all, so there "
                f"is nothing for a refund to attach to — `refundCreate` needs a "
                f"parent transaction. This is not the same as an order that has "
                f"already been refunded. The refund is {_money(wanted)}.",
                "no_refundable_transactions",
            )
        return empty(
            f"None of the {len(rows)} transactions on this order can take a "
            f"refund — each one is a refund, a void, unsuccessful, or already "
            f"fully refunded. The refund is {_money(wanted)}.",
            "no_refundable_transactions",
        )

    if available < wanted:
        return empty(
            f"Shopify shows only {_money(available)} still refundable on this "
            f"order, but the refund is {_money(wanted)}. Nothing was sent — a "
            f"partial refund in Shopify would look settled when it is not.",
            "insufficient_refundable",
        )

    transactions = []
    gateways = []
    remaining = wanted

    for parent in parents:
        if remaining <= 0:
            break
        take = min(remaining, _headroom(parent))
        if take <= 0:
            continue

        gateway = str(parent.get("gateway") or "").strip()
        transactions.append({
            "parentId": str(parent.get("id") or ""),
            "kind": "REFUND",
            "gateway": gateway,
            "amount": _money(take),
        })
        if gateway not in gateways:
            gateways.append(gateway)
        remaining -= take

    return {
        "transactions": transactions,
        "gateways": gateways,
        "allocated": flt(_money(wanted - remaining)),
        "problem": None,
        "problem_code": "",
    }


def build_refund_input(order_gid, plan, note, notify=False, fallback_note="") -> dict:
    """
    The RefundInput for refundCreate, or None when the plan allocated nothing.

    No `refundLineItems`: sending line items makes Shopify restock, and ERPNext
    is the inventory master here.  An amount-only refund is a legitimate
    refundCreate.  No `shipping` either — the Refund Request does not model it.

    `note` is the refund *reason*.  RefundInput has no separate reason field;
    `note` is what the admin's "Reason for refund" box writes, and it is
    staff-visible only, so it can carry internal detail.  A blank one falls back
    to `fallback_note` rather than reaching Shopify as "no reason provided" when
    ERPNext had a reason for it.  `discrepancyReason` is deliberately unset — it
    categorises an order-adjustment discrepancy, not the human reason.
    """
    if not plan or not plan.get("transactions"):
        return None

    payload = {
        "orderId": order_gid,
        "notify": bool(notify),
        "transactions": [
            {"orderId": order_gid, **transaction} for transaction in plan["transactions"]
        ],
    }

    resolved_note = str(note or "").strip() or str(fallback_note or "").strip()
    if resolved_note:
        payload["note"] = resolved_note

    return {"input": payload}


def build_refund_mutation(key: str = "") -> str:
    """
    The refundCreate document, with the @idempotent directive only when asked.

    refundCreate accepts `@idempotent(key: "…")`, but that could not be
    confirmed against the configured API version, and an unknown directive is a
    query-level error — which execute() raises on, so it would fail *every*
    write-back rather than degrade.  It is therefore off unless a caller passes
    a key.  Turn it on once a live response has confirmed it.

    What carries idempotency instead is three things, and the third is the easy
    one to forget: the stored-GID guard, the worker claim, and — for the window
    neither of those can reach, between the POST and the answer — the fact that
    write_back_refund posts this document with execute(..., idempotent=False),
    so it goes out at most once in any way that could have executed.  Without a
    key that is the only protection there is; see the comment at the post.

    No key is generated anywhere yet, deliberately — a helper that minted one
    while nothing sent it would read as though retries were already protected.
    When this is switched on, the key should be the Refund Request name and the
    amount in minor units (name plus net_refund_amount to two places), so it
    changes whenever the refund does.

    Quotes are stripped from the key rather than escaped: a key is ours to
    generate, so a quote in one is a bug, and silently breaking out of the
    directive's string would corrupt the whole document.
    """
    key = str(key or "").replace('"', "").replace("\\", "").strip()
    directive = f' @idempotent(key: "{key}")' if key else ""
    return _REFUND_CREATE_MUTATION % {"directive": directive}


# ── Availability / state ──────────────────────────────────────────────────────

def _has_writeback_fields() -> bool:
    """
    Whether Refund Request exists here and carries our write-back fields.

    False on a site without payment_portals, and on one where `bench migrate`
    has not yet run the patch.  Every entry point starts here, so the app is
    inert rather than broken in both cases.
    """
    try:
        meta = frappe.get_meta(REFUND_REQUEST)
        return bool(meta.has_field(REFUND_GID_FIELD)) and bool(
            meta.has_field(WRITEBACK_STATUS_FIELD)
        )
    except Exception:
        return False


def _set_state(refund_name: str, **values):
    """Write write-back state without touching `modified` or making Versions."""
    if not values:
        return
    frappe.db.set_value(REFUND_REQUEST, refund_name, values, update_modified=False)


def _claim_timestamp(refund_name: str):
    """
    When the current Pending claim was taken.

    State writes use update_modified=False, so `modified` cannot date the claim.
    shopify_writeback_at doubles as the claim stamp: written when the claim is
    taken, overwritten with the real write-back time on success.  On a Failed
    row it therefore reads as "last attempted at", which is what the field
    description says.
    """
    return frappe.db.get_value(REFUND_REQUEST, refund_name, WRITEBACK_AT_FIELD)


def _claim(refund_name: str) -> bool:
    """
    Claim a Refund Request for write-back, or return False if someone else has.

    The form button, a retry and any future dispatcher can all fire at the same
    document.  A read-then-write check would let two of them both see "not
    written back" and refund the customer twice, so the claim is a
    compare-and-swap under a row lock, committed before any HTTP happens.

    A Pending claim older than STALE_CLAIM_MINUTES is treated as abandoned — a
    worker killed mid-request must not block the document forever, because a
    refund stuck at Pending is a customer who never gets paid and nothing that
    says so.
    """
    # Short row lock: read-modify-write only, no network inside it.
    frappe.db.sql(
        "SELECT name FROM `tab{0}` WHERE name = %(name)s FOR UPDATE".format(REFUND_REQUEST),
        {"name": refund_name},
    )

    current = frappe.db.get_value(
        REFUND_REQUEST, refund_name,
        [REFUND_GID_FIELD, WRITEBACK_STATUS_FIELD],
        as_dict=True,
    ) or {}

    if (current.get(REFUND_GID_FIELD) or "").strip():
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — release the row lock
        return False

    # Re-checked here and not only in check_eligibility: two callers can both
    # pass eligibility before either writes its result, and the loser must not
    # send a second refund against an attempt whose fate is unknown.
    if (current.get(WRITEBACK_STATUS_FIELD) or "") == STATUS_UNVERIFIED:
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — release the row lock
        return False

    if (current.get(WRITEBACK_STATUS_FIELD) or "") == STATUS_PENDING:
        claimed_at = _claim_timestamp(refund_name)
        cutoff = add_to_date(now_datetime(), minutes=-STALE_CLAIM_MINUTES)
        if claimed_at and claimed_at > cutoff:
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — release the row lock
            return False

    # Status and stamp in ONE write.  Writing the stamp separately would leave a
    # window where another worker sees status=Pending with a stale-or-absent
    # timestamp, judges the claim abandoned, and claims it as well.
    _set_state(refund_name, **{
        WRITEBACK_STATUS_FIELD: STATUS_PENDING,
        WRITEBACK_AT_FIELD: now_datetime(),
    })
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — claim must be visible to other workers before we call Shopify
    return True


def _unverified_warning(shopify_order_id, detail: str = "") -> str:
    """
    The text for a refund whose fate is unknown, warning first.

    Deliberately front-loaded: _release_claim keeps only the first 1000
    characters, and appending the warning after a verbose Shopify error — the
    GraphQL error path alone can carry 500 — pushed it off the end of the one
    field a person reads to learn this refund may already have been paid.  The
    natural response to a truncated error message is to retry.

    It also has to stand on its own, because the copy written before the mutation
    is posted is all there is if the worker never comes back to append anything.
    """
    warning = (
        f"POSSIBLY PAID — do NOT retry. This refund request reached Shopify and "
        f"its outcome is not confirmed, so the customer may already have been "
        f"paid. Open order {shopify_order_id} in Shopify: if a refund is there, "
        f"record it with resolve_unverified_writeback; if not, clear it there to "
        f"allow another attempt."
    )
    detail = str(detail or "").strip()
    return f"{warning}\n\n{detail}" if detail else warning


def _release_claim(refund_name: str, status: str, error: str = ""):
    _set_state(refund_name, **{
        WRITEBACK_STATUS_FIELD: status,
        WRITEBACK_ERROR_FIELD: (error or "")[:1000],
    })
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job; state must persist


def _record_skip(refund_name: str, reason: str):
    """
    A skip is a decision, not a non-event — record it so the next person does
    not have to re-derive why nothing was sent.

    Never called for the already-written case, which must keep its Done status
    and its GID.
    """
    try:
        _set_state(refund_name, **{
            WRITEBACK_STATUS_FIELD: STATUS_SKIPPED,
            WRITEBACK_ERROR_FIELD: (reason or "")[:1000],
        })
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — may run in a background job
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"Shopify: Refund Skip Not Recorded — {refund_name}"
        )


def _settings_for_store(shop_domain: str, require_enabled: bool = True):
    """
    Shopify Settings for a store, or None.

    Same shape as fulfillment._settings_for_store, filtering on this feature's
    own toggle rather than lifting a shared helper that would then have to take
    the toggle name as an argument.
    """
    filters = {"shop_domain": shop_domain, "enable_sync": 1}
    if require_enabled:
        filters["enable_refund_writeback"] = 1
    name = frappe.db.get_value("Shopify Settings", filters, "name")
    return frappe.get_doc("Shopify Settings", name) if name else None


# ── Eligibility ───────────────────────────────────────────────────────────────

def _ownership(eligibility: dict) -> dict:
    """
    The routing keys, in the one shape a caller cannot misread.

    `caller_must_pay` is a **positive** assertion of the dangerous action, and
    that is the whole point of its existence.  The predecessor of these keys was
    a single bool, and the natural way to use it — `if not owns_payout: pay()` —
    silently did the wrong thing for every value that meant "we could not tell".
    A flag that is true only for the one code meaning "not mine" defaults to the
    safe direction on anything unknown, unrecognised, or added later.

    `owns_payout` is kept for the caller already reading it, and is now
    three-state: True, False, or None when undeterminable.  `not owns_payout` is
    NOT a safe test — None is falsy.  Branch on `caller_must_pay`.
    """
    owner = eligibility.get("payout_owner") or OWNER_UNKNOWN
    return {
        "payout_owner": owner,
        "caller_must_pay": owner == OWNER_CALLER,
        "owns_payout": {
            OWNER_SHOPIFY: True, OWNER_CALLER: False, OWNER_UNKNOWN: None,
        }[owner],
    }


def check_eligibility(refund_name: str) -> dict:
    """
    Can this Refund Request be written back to Shopify right now?

    Returns {"ok", "reason", "reason_code", "payout_owner", "settings",
             "shopify_order_id", "shopify_store", "amount", "note", "status",
             "refund_gid"}.  Read-only — safe to call from the client on every
    form refresh.

    Every guard in the brief's §6 lives here, and every one returns a reason
    rather than raising.  A payout path that throws on an edge case is worse
    than one that refuses.

    **Ownership is settled before any guard that can return**, and the order is
    load-bearing.  Leave "is this even a Shopify order?" until after the other
    checks and the guards that fire first return without ever looking — which is
    exactly how this reported a Manual Portal Refund, a refund Shopify had
    already paid, as the caller's to pay.  Deciding it first also keeps the
    contract's routing invariant exact: `payout_owner == OWNER_CALLER` if and
    only if `reason_code == REASON_NOT_OURS`.
    """
    out = {"ok": False, "reason": "", "reason_code": "",
           "payout_owner": OWNER_UNKNOWN, "settings": None,
           "shopify_order_id": "", "shopify_store": "", "amount": 0.0,
           "note": "", "status": "", "refund_gid": ""}

    if not _has_writeback_fields():
        # Ownership stays unknown, not "not ours": without our fields we cannot
        # read what deciding it needs, and answering "not ours" here would invite
        # the caller to pay a refund Shopify may already have paid.
        out["reason_code"] = "not_installed"
        out["reason"] = _NOT_MIGRATED_REASON
        return out

    row = frappe.db.get_value(
        REFUND_REQUEST, refund_name,
        ["docstatus", "status", "refund_channel", "sales_order",
         # Read to tell "not booked yet" from "booked already", which are
         # opposite answers on a Completed row — see already_booked.  A field
         # payment_portals owns; it has existed on Refund Request since the
         # doctype did, so it needs no capability probe.
         "payment_entry",
         "net_refund_amount", "reason_note",
         REFUND_GID_FIELD, WRITEBACK_STATUS_FIELD],
        as_dict=True,
    )
    if not row:
        out["reason_code"] = "refund_request_missing"
        out["reason"] = f"{REFUND_REQUEST} {refund_name} does not exist."
        return out

    out["status"] = row.get(WRITEBACK_STATUS_FIELD) or ""
    out["refund_gid"] = (row.get(REFUND_GID_FIELD) or "").strip()
    out["amount"] = flt(row.get("net_refund_amount"))
    out["note"] = str(row.get("reason_note") or "")

    # ── Ownership, before every guard that could return without looking ──────
    sales_order = str(row.get("sales_order") or "").strip()
    order = (frappe.db.get_value(
        "Sales Order", sales_order, ["shopify_order_id", "shopify_store"], as_dict=True
    ) or {}) if sales_order else {}
    out["shopify_order_id"] = str(order.get("shopify_order_id") or "").strip()
    out["shopify_store"] = str(order.get("shopify_store") or "").strip()

    # A stored GID is proof Shopify accepted a refund for this document, so it
    # settles ownership by itself — including when the Sales Order has since been
    # amended and lost its order id, which would otherwise read as "not ours"
    # for a refund Shopify demonstrably made.
    out["payout_owner"] = (
        OWNER_SHOPIFY if (out["refund_gid"] or out["shopify_order_id"])
        else OWNER_CALLER
    )

    # Idempotency next, and deliberately before the rest: it is the one guard
    # that must hold even if the document has since been edited into a state the
    # other guards would reject.
    if out["refund_gid"]:
        out["reason_code"] = "already_paid"
        out["reason"] = (
            f"Already written back to Shopify as {out['refund_gid']}. No further "
            f"refund is ever sent for this document."
        )
        return out

    # Second, so that every guard below is reached only by a refund already
    # known to be Shopify's.  That is what makes payout_owner trustworthy on all
    # of those paths, rather than only on the ones that happened to look.
    if out["payout_owner"] == OWNER_CALLER:
        out["reason_code"] = REASON_NOT_OURS
        out["reason"] = (
            f"Sales Order {sales_order} has no Shopify order id — this is a "
            f"payment link or a direct gateway payment, and its refund does not "
            f"go through Shopify."
        ) if sales_order else (
            "No Sales Order on this refund, so no Shopify order to refund."
        )
        return out

    if out["status"] == STATUS_UNVERIFIED:
        out["reason_code"] = "unverified_previous_attempt"
        out["reason"] = (
            "A previous attempt was sent to Shopify and its outcome could not be "
            "confirmed, so this refund may already have been paid. Retrying could "
            "pay the customer twice. Check the order in Shopify, then record what "
            "you found with resolve_unverified_writeback."
        )
        return out

    if cint(row.get("docstatus")) != 1:
        out["reason_code"] = "not_submitted"
        out["reason"] = "Only a submitted Refund Request is written back."
        return out

    # ── The channel, before the status ──────────────────────────────────────
    #
    # Judged first because it is the more informative refusal: a Bank Transfer
    # refund is not dispatchable in ANY status, so complaining about the status
    # would name the wrong problem and send somebody off to change a field that
    # would not help.
    channel = (row.get("refund_channel") or "").strip()

    if channel == CHANNEL_FROM_SHOPIFY:
        out["reason_code"] = "channel_is_manual_portal_refund"
        out["reason"] = (
            f"Refund channel is '{CHANNEL_FROM_SHOPIFY}' — this refund was made "
            f"in Shopify already, so writing it back would refund it twice."
        )
        return out

    if channel != CHANNEL_DISPATCH:
        out["reason_code"] = "channel_does_not_dispatch"
        out["reason"] = (
            f"Refund channel is '{channel or 'blank'}', not '{CHANNEL_DISPATCH}' "
            f"— that channel pays the customer by another route, so asking "
            f"Shopify to pay as well would refund them twice. Only a "
            f"'{CHANNEL_DISPATCH}' refund is dispatched here."
        )
        return out

    # ── The state, which is BEFORE the booking and not after it ─────────────
    #
    # See CONTRACT_VERSION 4.  A successful refundCreate pays the customer, so
    # the accepted state has to be the one that precedes ERPNext's own record of
    # the payment.
    status = (row.get("status") or "").strip()
    if status not in DISPATCHABLE_STATUSES:
        booked = str(row.get("payment_entry") or "").strip()
        if status == "Completed" and booked:
            # Its own code, and not a status complaint: this is either a payout
            # about to land after its own booking, or — since no GID is set,
            # which the guard above would have caught — a refund something paid
            # outside this flow.  Both need a person, neither needs a field
            # changed.
            out["reason_code"] = "already_booked"
            out["reason"] = (
                f"ERPNext has already booked this refund as {booked}, and no "
                f"Shopify refund is recorded against it. Paying it now would "
                f"pay the customer after the books said they were paid. Check "
                f"what settled this refund before sending anything."
            )
            return out

        out["reason_code"] = "wrong_refund_status"
        out["reason"] = (
            f"Refund status is '{status or 'blank'}', and a refund is dispatched "
            f"to Shopify from "
            f"{' or '.join(sorted(DISPATCHABLE_STATUSES))} — the payout happens "
            f"before ERPNext books it, because a successful Shopify refund is "
            f"what pays the customer."
        )
        return out

    if out["amount"] <= 0:
        out["reason_code"] = "nothing_to_refund"
        out["reason"] = (
            f"Net refund to customer is {out['amount']:.2f} — there is nothing "
            f"to send."
        )
        return out

    # Resolved from the refund's own Sales Order and from nothing else.  This
    # took a `settings` override until write_back_refund stopped accepting one;
    # leaving it here would have kept the single route by which a caller could
    # aim eligibility at an unrelated store's credentials.
    shop_domain = out["shopify_store"]
    settings = _settings_for_store(shop_domain)
    if not settings:
        out["reason_code"] = "writeback_unavailable_for_store"
        out["reason"] = (
            f"No enabled Shopify Settings for store '{shop_domain or '?'}' with "
            f"Refund Write-Back switched on. Nothing was sent."
        )
        return out

    if not has_admin_api_credentials(settings):
        out["reason_code"] = "no_api_credentials"
        out["reason"] = (
            f"Store '{shop_domain}' has no Admin API credentials configured, so "
            f"the refund cannot be sent."
        )
        return out

    out["settings"] = settings
    out["ok"] = True
    return out


# ── The write-back ────────────────────────────────────────────────────────────

def _response_gateways(refund) -> list:
    """
    The gateways Shopify says it used, deduplicated, in response order.

    Shopify is the authority on this, not our plan: the plan says what we asked
    for, the response says what happened.  refund.transactions is a connection
    here, unlike order.transactions — transaction_nodes tolerates either.
    """
    gateways = []
    for node in transaction_nodes((refund or {}).get("transactions")):
        gateway = str(node.get("gateway") or "").strip()
        if gateway and gateway not in gateways:
            gateways.append(gateway)
    return gateways


def _proves_not_executed(exc) -> bool:
    """
    Whether this failure is PROOF that Shopify never ran the document.

    Named for what it asserts, because the assertion is the whole of it.  This
    is consulted only after refundCreate has been posted, and a True here is
    what licenses reporting a payout as failed_unsent — "nobody was paid, safe
    to retry".  Wrong in the True direction and payment_portals retries a refund
    that has already paid the customer through the Cashfree-OCC bridge.

    So nothing is derived here.  There is exactly ONE source of truth and it is
    the exception's `proves_not_executed`, which the CLIENT sets, because the
    client is the only place that can know it: only the raise site saw the
    response body and knows which branch raised.

    What the client will certify
    ---------------------------
    Two raises, and they are narrow:

        401 / 403        refused at the auth layer, the document unreached.
        200 + THROTTLED  Shopify's own cost refusal, and ONLY in the shape the
        refused before   GraphQL spec reserves for a request refused before
        execution        execution began — no `data` key at all, and no
                         errors[].path.  See
                         shopify_graphql._refused_before_execution.

    What it will not, however the failure looks
    -------------------------------------------
        200 + THROTTLED  the document resolved part way and then blew the cost
        after execution  budget, so it may have COMMITTED.  refundCreate
        began            resolves a transactions(first: 10) connection with real
                         query cost, so this is reachable on the very mutation
                         that pays a customer — and it carries the SAME
                         extensions.code as the pure refusal above.
        HTTP 429         Shopify throttles GraphQL with a 200 body, so a 429 on
                         graphql.json may be a CDN, a WAF or an egress proxy in
                         FRONT of the store, and a layer like that knows nothing
                         about whether the document behind it ran.  Round 2
                         certified it as a refusal on the premise "a rate
                         limiter rejects without executing"; that is a premise
                         about infrastructure nobody here owns, and the price of
                         it being wrong is a second real refund.  So a bare 429
                         no longer qualifies and lands on Unverified instead.
        transport / 5xx  only the ANSWER is known to be lost.

    Two inferences used to live here and both are gone.  The first tested
    `"THROTTLED" in exc.error_codes`, which reads the partly-executed response
    above as "nobody was paid, safe to retry" — exactly backwards, on the one
    response that might already have paid a customer.  The second tested
    `status_code in (401, 403)`, justified as belt-and-braces for an "older code
    path" that raised without the flag; no such path exists — every raise in the
    client sets it and the class defaults it False — so what the fallback could
    actually reach was an exception from somewhere that declined to make the
    claim, with this side then making it on their behalf.  Two sources of truth
    for one money decision is the deadlock CONTRACT_VERSION 3 already removed
    once.

    Neither may come back as an `or` beside the flag.  The flag is False on
    precisely the responses those tests fire on, so such an `or` restores the
    defect while reading as a belt beside a brace.

    The default is False, and it is the safe one: False means "assume the
    document may have run", which lands the row on Unverified where a person
    decides.  When the choice is between "might pay twice" and "might need a
    human to look", this module chooses the human.

    getattr with a default rather than attribute access, deliberately: this runs
    inside the handler that decides whether a customer was paid, so an exception
    carrying no such attribute has to read as "proves nothing" instead of
    raising there.
    """
    return bool(getattr(exc, "proves_not_executed", False))


def _log(settings, refund_name, shopify_order_id, status, message, payload=None):
    """One Shopify Log entry per attempt, successful or not."""
    from shopify_integration.utils.webhook import log_webhook

    try:
        log_webhook(
            topic="refund/writeback",
            shop_domain=str(settings.get("shop_domain") or "") if settings else "",
            order_data=payload or {"refund_request": refund_name},
            status=status,
            error_message=message,
            shopify_order_id=str(shopify_order_id or ""),
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"Shopify: Refund Log Failed — {refund_name}"
        )


def write_back_refund(refund_name: str, triggered_by: str = "manual") -> dict:
    """
    Tell Shopify about one booked ERPNext refund.

    **This pays the customer.**  A successful refundCreate on these orders is
    bridged into a real Cashfree refund by the Cashfree-OCC app, so treat it as
    a payout and not as bookkeeping.  There is no doc_events hook, deliberately,
    because deciding to pay somebody belongs with whatever owns the refund's
    money path and not with a save handler here.  But two callers do reach this,
    and since f431c17 one of them is automatic: payment_portals' Send step,
    through the refund_payout_dispatchers hook registered in hooks.py, and
    writeback_now behind the form button.  The module docstring above has both,
    and REFUND-DISPATCH-CONTRACT.md 2 and 2b have the states each arrives in.

    Idempotent and safe to call twice: shopify_refund_gid being set is a hard
    stop, and the worker claim stops two callers racing.  Never raises — every
    outcome comes back as a result dict so the caller can log it.

    **Authorisation is the caller's, and deliberately so.**  This is not
    whitelisted: it is not reachable over HTTP, so every caller is in-process and
    trusted, and by the time a dispatcher gets here payment_portals has already
    authorised the payout against its own PAYOUT_ROLES.  An earlier version did
    check submit permission on the Refund Request, which created a deadlock —
    somebody holding Refund Approver but not doctype submit permission passed
    that gate and failed this one, and the resulting "unknown" meant neither app
    would pay the refund, for a reason that explained nothing.  Two permission
    models guarding one payout is one too many.  The HTTP door is writeback_now,
    which does check.

    It also takes no `settings` argument: the store is resolved from the refund's
    own Sales Order, so no caller can aim this at another store's credentials.

    :return: the result dict described in REFUND-DISPATCH-CONTRACT.md §4
    """
    # Ownership as far as it is known at the moment result() is called.  Set once
    # eligibility has run; before that — the app is not installed here, or there
    # is no such document — it is genuinely undeterminable and must not read as
    # "not ours".
    owner = {"payout_owner": OWNER_UNKNOWN}

    def result(ok, status, message, outcome, reason_code="",
               refund_gid="", gateway="", amount=0.0):
        return {
            "ok": ok,
            "outcome": outcome,
            "reason_code": reason_code,
            **_ownership(owner),
            # Derivable from `outcome`, stated anyway so a caller cannot get the
            # mapping wrong on the one axis where being wrong pays twice.
            "retry_safe": outcome in _RETRY_SAFE_OUTCOMES,
            "possibly_paid": outcome in (OUTCOME_PAID, OUTCOME_FAILED_UNKNOWN),
            "status": status,
            "message": message,
            "refund_gid": refund_gid,
            "gateway": gateway,
            "amount": amount,
            "refund_request": refund_name,
            "provider": "shopify",
            "contract_version": CONTRACT_VERSION,
        }

    if not _has_writeback_fields():
        return result(False, STATUS_SKIPPED, _NOT_MIGRATED_REASON,
                      OUTCOME_REFUSED, reason_code="not_installed")

    try:
        eligibility = check_eligibility(refund_name)
        owner["payout_owner"] = eligibility["payout_owner"]
        if not eligibility["ok"]:
            # An already-written refund keeps its Done status and its GID; every
            # other refusal is recorded as a Skip with its reason.
            if eligibility["refund_gid"]:
                return result(False, STATUS_DONE, eligibility["reason"],
                              OUTCOME_REFUSED, reason_code="already_paid",
                              refund_gid=eligibility["refund_gid"],
                              amount=eligibility["amount"])
            # An unconfirmed earlier attempt keeps its Unverified status; it must
            # not be flattened into a Skip, which reads as "nothing happened".
            if eligibility["status"] == STATUS_UNVERIFIED:
                return result(False, STATUS_UNVERIFIED, eligibility["reason"],
                              OUTCOME_FAILED_UNKNOWN,
                              reason_code=eligibility["reason_code"],
                              amount=eligibility["amount"])
            _record_skip(refund_name, eligibility["reason"])
            return result(False, STATUS_SKIPPED, eligibility["reason"],
                          OUTCOME_REFUSED, reason_code=eligibility["reason_code"],
                          amount=eligibility["amount"])

        settings = eligibility["settings"]
        shopify_order_id = eligibility["shopify_order_id"]
        amount = eligibility["amount"]
        note = eligibility["note"]

        if not _claim(refund_name):
            return result(False, STATUS_PENDING,
                          "Another process is already writing this refund back to "
                          "Shopify.",
                          OUTCOME_IN_PROGRESS, reason_code="claimed_elsewhere",
                          amount=amount)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"Shopify: Refund Write-Back Setup Failed — {refund_name}"
        )
        # Thrown before the claim, so before any HTTP: nothing was sent.
        return result(False, "", "Could not start the write-back; see the Error Log.",
                      OUTCOME_FAILED_UNSENT, reason_code="setup_failed")

    def fail_unsent(message, reason_code, seen=None):
        """Nothing left this process, or Shopify explicitly declined it.  Nobody
        was paid, and a retry cannot double-pay.

        `seen` is the transaction summary when the refusal was a verdict *about*
        the transactions.  Recorded because REF-00207 proved the alternative
        unusable: two identical failures whose log payload was
        {"refund_request": "REF-00207"} and nothing else, so the only question a
        reader had — which row failed which test — could not be answered from
        ERPNext at all, and needed a Shopify login nobody had for that store.
        """
        _release_claim(refund_name, STATUS_FAILED, message)
        payload = {"refund_request": refund_name}
        if seen is not None:
            payload["transactions"] = seen
        _log(settings, refund_name, shopify_order_id, "Failed", message,
             payload=payload)
        return result(False, STATUS_FAILED, message,
                      OUTCOME_FAILED_UNSENT, reason_code=reason_code, amount=amount)

    def fail_unknown(message, reason_code):
        """The mutation went out and we cannot prove what became of it.

        The customer may already have been paid, so this must never be reported
        as a plain failure and must never be retried automatically.  It lands on
        Unverified, which no trigger picks up.
        """
        message = _unverified_warning(shopify_order_id, message)
        _release_claim(refund_name, STATUS_UNVERIFIED, message)
        _log(settings, refund_name, shopify_order_id, "Failed", message)
        frappe.log_error(message, f"Shopify: Refund Outcome Unknown — {refund_name}")
        return result(False, STATUS_UNVERIFIED, message,
                      OUTCOME_FAILED_UNKNOWN, reason_code=reason_code, amount=amount)

    # Flipped immediately before the mutation is posted and never reset.  It is
    # the single fact that separates "nobody was paid" from "somebody might have
    # been", so it is a plain local rather than anything inferred after the fact.
    sent = False

    # ── Everything past the claim must land in a definite state ──────────────
    try:
        order_gid = gid("Order", shopify_order_id)

        data = execute(
            settings,
            _REFUND_TARGETS_QUERY,
            {"orderId": order_gid},
            operation="RefundTargets",
        )

        order = (data or {}).get("order")
        if not order:
            return fail_unsent(
                f"Shopify order {shopify_order_id} not found — it may have been "
                f"deleted, or the token cannot see it. Nothing was refunded.",
                "shopify_order_not_found",
            )

        plan = plan_refund(order.get("transactions"), amount)
        if plan["problem"]:
            seen = transaction_summary(order.get("transactions"))
            # check_eligibility has already refused amount <= 0, so only the two
            # headroom codes can reach here and both belong to failed_unsent.
            # Pinned rather than assumed: a code from the wrong outcome would
            # tell payment_portals to branch on something this outcome never
            # carries.
            problem_code = plan["problem_code"]
            if problem_code not in REASON_CODES[OUTCOME_FAILED_UNSENT]:
                problem_code = "insufficient_refundable"
            return fail_unsent(plan["problem"], problem_code, seen=seen)

        payload = build_refund_input(
            order_gid,
            plan,
            note,
            notify=bool(cint(settings.get("notify_customer_on_refund"))),
            fallback_note=f"Refund {refund_name}",
        )
        if not payload:
            return fail_unsent(
                "Nothing could be allocated to a refundable transaction on "
                "this order.",
                "no_refundable_transactions",
            )

        # ── The durable marker, committed BEFORE the post ────────────────────
        # `sent` below is a local and dies with the worker.  A process killed
        # during execute() — a container restart, an OOM, an eviction — would
        # otherwise leave this row at Pending, which is indistinguishable from
        # one that never posted: the staleness escape would hand it to the next
        # caller and Shopify would refund the customer a second time, because a
        # partial refund leaves the order enough headroom to take another.
        #
        # Unverified already means "sent, fate unknown", which is exactly true
        # from this instant onward, and both check_eligibility and _claim
        # already refuse it.  So the risky state is entered before the risk
        # rather than after it, and a worker that never returns leaves behind
        # the correct answer instead of a retryable one.
        _set_state(refund_name, **{
            WRITEBACK_STATUS_FIELD: STATUS_UNVERIFIED,
            WRITEBACK_ERROR_FIELD: _unverified_warning(shopify_order_id)[:1000],
        })
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — must survive a worker that never returns

        # ── The post: at most one of these can ever have EXECUTED ──────────
        # Not "the post happens once", which is the headline this comment used
        # to carry and is false.  execute() may still POST this identical
        # mutation up to five times, on exactly one failure: Shopify's own
        # THROTTLED 200-body in the shape the GraphQL spec reserves for a
        # request refused BEFORE execution began.  That is safe because every
        # one of those posts was REFUSED, not because there was only one.  The
        # property the verdicts below rest on is the weaker and true one: at
        # most one post that Shopify could have run.
        #
        # idempotent=False is the line that gives it, and it is the only thing
        # that makes every post-send "nothing was sent" claim below provable.
        # build_refund_mutation() is called with no key, so the @idempotent
        # directive is absent and Shopify cannot recognise a second POST of
        # this document as the same refund: attempt 1 creates a real partial
        # refund, its answer dies in the socket timeout, the order still holds
        # the headroom a partial refund leaves, and attempt 2 pays the customer
        # a SECOND time through the Cashfree-OCC bridge.
        #
        # So it does not mean "one attempt".  execute() still retries the one
        # failure Shopify itself certifies as a pre-execution refusal — a
        # THROTTLED 200 with no `data` key and no errors[].path — because
        # re-posting after a refusal cannot double-pay.  What it no longer
        # retries is a transport error, a 5xx, a bare HTTP 429, or a THROTTLED
        # body that shows execution began; none of those is evidence about
        # whether the mutation ran.  The 429 because Shopify throttles GraphQL
        # with a 200 body, so one here may be a CDN or a proxy in front of the
        # store; the last because a document can resolve part way and then blow
        # the cost budget.  The table is in shopify_graphql.execute()'s
        # docstring.
        #
        # DO NOT restore the default here without reading the two handlers at
        # the foot of this function.  Their failed_unsent verdicts — userErrors,
        # 401/403, and the refusals the client vouches for with
        # proves_not_executed, every one of them "nobody was paid, safe to
        # retry" — are sound only because the answer they read describes the ONLY
        # request that could have executed.  With retries on it described the
        # last of up to five that could each have run, and the scenario a code
        # review named is: attempt 1
        # creates the refund, its response is lost, attempt 2 is declined with
        # "Refund amount exceeds the amount refundable on this order" PRECISELY
        # BECAUSE attempt 1 consumed the headroom — whereupon this function
        # wrote STATUS_FAILED over the durable Unverified marker, returned
        # retry_safe=True, and refund_request.js offered "Retry Shopify Refund"
        # on a refund the customer had already been paid.
        #
        # The RefundTargets query above keeps the default on purpose: it is a
        # read, re-posting it cannot pay anybody, and losing that resilience
        # would trade a safe retry for a fragile one.
        sent = True
        data = execute(
            settings,
            build_refund_mutation(),
            payload,
            operation="refundCreate",
            idempotent=False,
        )

        # The load-bearing call.  HTTP 200 with userErrors means Shopify
        # declined and nothing happened; recording success here would tell
        # everyone a customer had been paid when they had not.
        mutation_payload = check_user_errors(data, "refundCreate", context=refund_name)

        refund = mutation_payload.get("refund") or {}
        refund_gid = str(refund.get("id") or "").strip()
        if not refund_gid:
            # HTTP 200, no errors, no userErrors, and no refund either.  The
            # request reached Shopify and it answered without complaining, so
            # "nothing happened" is an assumption, not a fact.
            return fail_unknown(
                "Shopify accepted the request but returned no refund object.",
                "response_unverifiable",
            )

        gateways = _response_gateways(refund) or plan["gateways"]

        # The GID goes in and commits together with the success, because our own
        # write fires the refunds/create webhook: if the webhook lands before the
        # GID is visible, create_credit_note_from_shopify_refund cannot recognise
        # the refund as ours and ERPNext gets a second Credit Note.
        _set_state(refund_name, **{
            REFUND_GID_FIELD: refund_gid,
            WRITEBACK_STATUS_FIELD: STATUS_DONE,
            REFUND_GATEWAY_FIELD: ", ".join(gateways)[:140],
            WRITEBACK_AT_FIELD: now_datetime(),
            WRITEBACK_ERROR_FIELD: "",
        })
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — the GID must be visible before the refunds/create webhook arrives

        message = (
            f"Shopify refund {refund_gid} created for "
            f"{plan['allocated']:.2f} via {', '.join(gateways) or 'unknown gateway'}."
        )
        frappe.logger().info(
            f"Shopify: refunded order {shopify_order_id} from {refund_name} "
            f"({plan['allocated']:.2f}, gateways={', '.join(gateways) or '?'}, "
            f"trigger={triggered_by}) → {refund_gid}"
        )
        _log(settings, refund_name, shopify_order_id, "Processed", message,
             payload={"refund_request": refund_name, "refund": refund})
        return result(True, STATUS_DONE, message, OUTCOME_PAID,
                      refund_gid=refund_gid, gateway=", ".join(gateways),
                      amount=plan["allocated"])

    except ShopifyUserError as exc:
        # userErrors is unambiguous: the request was well-formed, Shopify read it
        # and declined it, and nothing was refunded.
        #
        # Unambiguous only because at most one post of this mutation could have
        # executed.  check_user_errors sees one response, and with
        # idempotent=False that response describes the only request that could
        # have run — five refused posts are still one that could have run.
        # Under the ordinary retries it saw the last of up to five that could
        # each have run, and "Refund amount
        # exceeds the amount refundable on this order" is exactly the answer a
        # second attempt gets when the FIRST one succeeded and consumed the
        # headroom — so this handler wrote STATUS_FAILED over the durable
        # Unverified marker, returned retry_safe=True, and the form offered
        # "Retry Shopify Refund" for a refund already paid.  Keep the
        # classification, and keep idempotent=False at the post with it: the two
        # are one decision, and separating them restores the defect silently.
        return fail_unsent(str(exc), "rejected_by_shopify")

    except ShopifyAPIError as exc:
        # Which phase raised decides whether the customer might have been paid.
        # Before the mutation was posted, nothing can have happened.  After, it
        # can: the POST may have been delivered and executed with only its answer
        # lost.  That is true of a single attempt, so it does not rest on
        # retries — and for this document there are none left on that path.
        if not sent:
            return fail_unsent(str(exc), "query_failed")
        if _proves_not_executed(exc):
            # Shopify produced this INSTEAD of running the document: refused at
            # the auth layer (401/403), or refused on its own structured
            # GraphQL throttle error before execution began.  Both are safe to
            # call unsent — and safe only because no earlier post of this
            # mutation could have executed.  That is the guarantee the comment
            # here used to assert ("rejected at the auth layer, before the
            # document ran") without being able to make it: with the ordinary
            # retries on, an earlier attempt could have run and paid before the
            # token was rotated or the cost bucket ran dry, and the 401 we ended
            # up reading said nothing about it.
            #
            # The claim itself is the client's, read off proves_not_executed —
            # see _proves_not_executed for why this side must not re-derive it,
            # from the error codes OR from the status.
            #
            # `rate_limited` therefore now means exactly ONE thing: Shopify
            # refused the document before executing it, on its own THROTTLED
            # 200-body in the shape the spec reserves for a pre-execution
            # refusal.  A bare HTTP 429 is NOT this: Shopify throttles GraphQL
            # with a 200 body, so a 429 on graphql.json may be a CDN, a WAF or
            # an egress proxy in front of the store, and it now falls through to
            # fail_unknown and parks the row on Unverified for a person.  That
            # costs somebody opening the order in Shopify, and it is the
            # deliberate trade: the alternative asserted a premise about
            # infrastructure nobody here controls.
            #
            # The status is read only to SPLIT two proven refusals that the
            # client has already vouched for, never to establish one.
            if getattr(exc, "status_code", None) in (401, 403):
                return fail_unsent(str(exc), "not_authorised")
            return fail_unsent(str(exc), "rate_limited")
        # Possibly paid either way, so both codes live in failed_unknown and
        # neither is ever retried.  They are split on the one piece of evidence
        # already in hand, because they are different facts and §6 of the
        # contract defines them narrowly:
        #
        #   no status_code   the transport failed — DNS, TLS, a reset, a socket
        #                    timeout — and nothing was read back at all.
        #   a status_code    Shopify ANSWERED and the answer was unusable: an
        #                    HTTP 400/404/5xx, or a 200-with-errors that was not
        #                    a proven refusal (including the THROTTLED body that
        #                    may carry a committed mutation).
        #
        # Filing the second under transport_error_after_send told a reader the
        # network broke and sent them to look at the wrong thing.
        if getattr(exc, "status_code", None) is None:
            return fail_unknown(str(exc), "transport_error_after_send")
        return fail_unknown(str(exc), "response_unverifiable")

    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"Shopify: Refund Write-Back Failed — {refund_name}"
        )
        if not sent:
            return fail_unsent(
                "Unexpected error before the refund was sent; see the Error Log.",
                "setup_failed",
            )
        return fail_unknown(
            "Unexpected error after the refund was sent; see the Error Log.",
            "response_unverifiable",
        )


# ── The credit-note loop guard ────────────────────────────────────────────────

def refund_request_for_shopify_refund(shopify_refund_id):
    """
    The Refund Request this app wrote for a Shopify refund, or None.

    Our own refundCreate fires the refunds/create webhook, and without this the
    handler would build a second Credit Note for a refund ERPNext already has.
    Both the GID and the bare numeric id are matched, so a caller can hand over
    whatever the payload gave it.
    """
    if not _has_writeback_fields():
        return None

    raw = str(shopify_refund_id or "").strip()
    if not raw:
        return None

    candidates = {raw, gid("Refund", raw)}
    return frappe.db.get_value(
        REFUND_REQUEST, {REFUND_GID_FIELD: ["in", sorted(candidates)]}, "name"
    )


def unverified_writebacks_for_order(shopify_order_id) -> list:
    """
    Every unresolved write-back of ours on this Shopify order, as a list.

    The GID lookup above is exact and covers only the window AFTER a successful
    refundCreate response has been read.  It cannot cover the window this
    function exists for, and that window is the dangerous one:

        _set_state(STATUS_UNVERIFIED)  ← committed BEFORE the post, GID EMPTY
        execute(refundCreate)          ← the post
        _set_state(REFUND_GID_FIELD)   ← the GID, only on a response we read

    Every failed_unknown outcome stops between the first and the third: a
    worker killed after the post, a socket timeout, a 5xx, a 200 that answered
    without a refund object.  Each leaves a row that IS ours, carries no GID,
    and sits beside a Shopify order that may genuinely hold the refund.  The
    refunds/create webhook for that refund then finds nothing on the GID lookup
    and is reported to payment_portals as a refund somebody made in Shopify —
    the two-recorders defect the guard exists to stop, in the one state where
    nobody can tell what happened.

    An Unverified row is not proof the refund is ours; it is grounds for
    withholding a report until a person has said which it is.  The caller
    decides what to do with that — see refund_report._surface, which logs it
    rather than skipping silently.

    EVERY match, not one of them
    ---------------------------
    This returned a single arbitrary row through frappe.db.get_value until
    round 3, and an order can carry two: a first attempt that lost its answer,
    then a second refund raised for the rest of the order that lost its answer
    too.  The caller names the row it is given in the message that tells a
    person what to resolve, so naming one of two says the order is clear once
    that one is cleared — while the refund the unnamed row may have paid is the
    one nothing ever records, and an omission is the failure nothing can
    recover.  Sorted, so two readers of the same order are told the same thing
    and a log line can be diffed.

    Submitted rows only
    -------------------
    docstatus == 1, and the filter is not hypothetical: cancelling the stuck
    request is exactly what an operator reaches for when a row will not clear.
    A cancelled Refund Request is no longer ERPNext's record of anything, so it
    must stop withholding reports for the order — otherwise the refund Shopify
    really made is recorded nowhere at all.  A draft never dispatched, so no
    mutation was ever posted from it.

    Resolved by doctype name through frappe, and inert when the write-back
    fields are absent, so this module stays installable on a site with no
    payment_portals.  The Sales Order fan-out is a multi-row read because a
    Sales Order can be AMENDED: the amendment carries the same
    shopify_order_id, the Refund Request points at one of the two, and matching
    a single Sales Order would miss the row exactly when the refund had been
    disputed and re-cut.  Never raises — it is consulted from inside a webhook
    that must return 200 about a refund that has already happened.
    """
    if not _has_writeback_fields():
        return []

    order_id = str(shopify_order_id or "").strip()
    if not order_id:
        return []

    try:
        sales_orders = frappe.get_all(
            "Sales Order", filters={"shopify_order_id": order_id}, pluck="name"
        )
        if not sales_orders:
            return []
        return sorted(frappe.get_all(
            REFUND_REQUEST,
            filters={
                WRITEBACK_STATUS_FIELD: STATUS_UNVERIFIED,
                "docstatus": 1,
                "sales_order": ["in", sorted(sales_orders)],
            },
            pluck="name",
        ))
    except Exception:
        # A guard that cannot decide answers "no hit" and says why.  Raising
        # here would take down a webhook about a refund that has already
        # happened, and no retry of it can un-refund anything.
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: Unverified Write-Back Lookup Failed — {order_id}",
        )
        return []


# ── Clearing an unconfirmed attempt ───────────────────────────────────────────

# What a Shopify refund id may look like, and nothing else.  Both forms a person
# can legitimately have in hand: the bare numeric id, and the GID Shopify's own
# API returns.  [0-9] rather than \d on purpose — \d also matches other Unicode
# decimal digits, and "１２３" is not an id Shopify will ever match.  Matched with
# fullmatch, because `$` also matches before a trailing newline and a value that
# is a real id plus one more line is not a real id.
_BARE_REFUND_ID = re.compile(r"[0-9]+")
_REFUND_GID = re.compile(r"gid://shopify/Refund/[0-9]+")

_REFUND_GID_EXAMPLE = "1234567890 or gid://shopify/Refund/1234567890"


def _validated_refund_gid(value) -> str:
    """
    The GID to store for a hand-entered refund id, or "" if it is not one.

    gid() passes anything already starting with "gid://" straight through, so
    without this a mis-paste — another resource type, another store's refund, a
    truncated id, an admin URL — was accepted verbatim and then permanently
    satisfied BOTH loop guards for that order: refund_request_for_shopify_refund
    would never match the real refund's id, and unverified_writebacks_for_order
    stops matching the moment the status moves to Done.  The refunds/create
    webhook for the refund Shopify actually made would then be reported as an
    externally-made refund and earn a second Payment Entry.

    This is the ONLY exit from Unverified, so refusing has to cost a retype and
    no more: surrounding whitespace is stripped, because that is a copy-paste
    and not a mis-paste.  Accepting the wrong value costs a refund that is
    never recorded, which is the failure nothing can recover.
    """
    raw = str(value or "").strip()
    if _BARE_REFUND_ID.fullmatch(raw):
        return gid("Refund", raw)
    if _REFUND_GID.fullmatch(raw):
        return raw
    return ""


@frappe.whitelist()
def resolve_unverified_writeback(refund_name: str, resolution: str,
                                 shopify_refund_gid: str = "",
                                 gateway: str = "", note: str = "") -> dict:
    """
    Close out an Unverified write-back, once a person has checked Shopify.

    Unverified means the mutation went out and its fate is unknown, so nothing
    automatic touches it — and without this it would be a dead end, which is the
    same silent-trap failure the staleness escape exists to avoid.  The only way
    out is a person reading the order in Shopify and saying what is there:

        resolution="paid"      a refund exists; supply its GID.  Recorded as
                               Done, exactly as if we had seen the response.
        resolution="not_paid"  no refund exists.  Cleared back to blank so the
                               ordinary path can send it.

    "paid" demands a GID rather than taking somebody's word for it: the GID is
    what the credit-note loop guard matches on, so a Done row without one would
    let the refunds/create webhook build a second Credit Note.  And it must be
    a refund id and not merely a non-empty string — a Done row carrying the
    WRONG id fails that guard exactly as a blank one does, while also looking
    settled.  See _validated_refund_gid.

    Who resolved it and which way is written into the note, because this is a
    decision about whether a customer has been paid, made without evidence in
    hand.
    """
    def refuse(message):
        return {"ok": False, "message": message, "refund_request": refund_name,
                "contract_version": CONTRACT_VERSION}

    # Availability before permission, for the reason write_back_refund has the
    # same ordering: on a site without payment_portals, frappe.has_permission has
    # no DocType to resolve and raises, so "you lack permission" would be the
    # wrong diagnosis for "this app is inert here" — and the refusal below could
    # never be reached to say otherwise.
    if not _has_writeback_fields():
        return refuse(_NOT_MIGRATED_REASON)

    frappe.has_permission(REFUND_REQUEST, "submit", doc=refund_name, throw=True)

    resolution = str(resolution or "").strip().lower()
    if resolution not in ("paid", "not_paid"):
        return refuse("Resolution must be 'paid' or 'not_paid'.")

    current = frappe.db.get_value(
        REFUND_REQUEST, refund_name, WRITEBACK_STATUS_FIELD
    )
    if current != STATUS_UNVERIFIED:
        # Deliberately narrow.  This is not a general "fix the status" tool; it
        # exists for one state, and pointing it at a Done row would overwrite a
        # real GID with a hand-typed one.
        return refuse(
            f"Write-back status is '{current or 'blank'}', not "
            f"'{STATUS_UNVERIFIED}'. Nothing was changed."
        )

    who = frappe.session.user
    detail = f" {note.strip()}" if str(note or "").strip() else ""

    if resolution == "paid":
        raw_gid = str(shopify_refund_gid or "").strip()
        if not raw_gid:
            return refuse(
                "Recording this as paid needs the Shopify refund id (the GID "
                "from the order's refund in Shopify). Without it the "
                "credit-note guard cannot recognise the refund and the "
                "refunds/create webhook would create a second Credit Note."
            )
        refund_gid = _validated_refund_gid(raw_gid)
        if not refund_gid:
            # Refusing costs a retype; accepting costs a refund that is never
            # recorded — see _validated_refund_gid.  The message names the two
            # accepted forms, because "invalid" alone sends a person back to
            # guess with the same value.
            return refuse(
                f"'{raw_gid[:60]}' is not a Shopify refund id. It must be the "
                f"refund's numeric id or its full GID — {_REFUND_GID_EXAMPLE} "
                f"— taken from the refund on this order in Shopify, not the "
                f"order id, the Refund Request name, or an admin URL. Nothing "
                f"was changed."
            )
        _set_state(refund_name, **{
            REFUND_GID_FIELD: refund_gid,
            WRITEBACK_STATUS_FIELD: STATUS_DONE,
            REFUND_GATEWAY_FIELD: str(gateway or "").strip()[:140],
            WRITEBACK_AT_FIELD: now_datetime(),
            WRITEBACK_ERROR_FIELD: (
                f"Unconfirmed attempt resolved as PAID by {who} after checking "
                f"the order in Shopify.{detail}"
            )[:1000],
        })
        message = f"Recorded as refunded in Shopify ({refund_gid})."
    else:
        _set_state(refund_name, **{
            REFUND_GID_FIELD: "",
            WRITEBACK_STATUS_FIELD: "",
            WRITEBACK_ERROR_FIELD: (
                f"Unconfirmed attempt resolved as NOT PAID by {who} after "
                f"checking the order in Shopify; cleared for another attempt."
                f"{detail}"
            )[:1000],
        })
        message = "Cleared. The refund can be sent to Shopify again."

    frappe.db.commit()  # nosemgrep: frappe-manual-commit — a money decision must persist immediately
    frappe.logger().info(
        f"Shopify: unverified write-back on {refund_name} resolved as "
        f"{resolution} by {who}"
    )
    return {"ok": True, "message": message, "resolution": resolution,
            "refund_request": refund_name, "contract_version": CONTRACT_VERSION}


# ── Whitelisted endpoints (form button / client status) ───────────────────────

@frappe.whitelist()
def writeback_now(refund_name: str) -> dict:
    """
    Write one refund back on demand — the form's button.

    Runs inline rather than enqueued so the user gets the real outcome back
    instead of an optimistic "queued"; one refund is two GraphQL calls, well
    inside a web request.  Requires submit permission on the document, because
    this moves money — this is the HTTP door, and the only entry point that
    checks; write_back_refund itself defers authorisation to its caller.
    """
    # Availability before permission: has_permission on a DocType this site does
    # not have raises, and "you lack permission" is the wrong diagnosis for "this
    # app is inert here".  write_back_refund returns the proper refusal, and it
    # touches nothing on the way to it.
    if not _has_writeback_fields():
        return write_back_refund(refund_name, triggered_by="manual")

    frappe.has_permission(REFUND_REQUEST, "submit", doc=refund_name, throw=True)
    return write_back_refund(refund_name, triggered_by="manual")


@frappe.whitelist()
def refund_targets_now(refund_name: str) -> dict:
    """
    What Shopify says is refundable on this refund's order.  Read-only.

    Exists because `REF-00207` refused with `no_refundable_transactions` on
    production and there was no way to find out why from inside ERPNext: the
    message did not distinguish "no transactions at all" from "every row
    disqualified", and the response was discarded.  Answering that needed a
    Shopify admin login for `electrobotic-in`, which is itself an open item.

    **It cannot pay anybody.** It runs the same `RefundTargets` query the payout
    runs and stops there — no mutation, no writes to the Refund Request, not
    even a claim.  `tests/test_refund_targets_diagnostic.py` asserts all of
    that, including by reading this function's own source for the mutation
    constant, because the risk is a later edit rather than this one.

    Deliberately **not** gated on `enable_refund_writeback`. Gating the one safe
    call on the dangerous switch would mean the diagnosis was only available
    while the payout was armed, which is the mistake
    `REFUND-REPORT-CONTRACT.md` §7 refuses for the report path. Gated on Shopify
    Settings write instead — the same permission `backfill_now` uses, for the
    same reason: it reveals store data and spends an Admin API call.

    :return: {"ok", "reason_code", "message", "amount", "refundable_total",
              "transactions": [...], "would_refuse_with", "shopify_order_id",
              "shopify_order_name", "shopify_store"}
    """
    frappe.has_permission("Shopify Settings", "write", throw=True)

    out = {"ok": False, "reason_code": "", "message": "", "amount": 0.0,
           "refundable_total": 0.0, "transactions": [], "would_refuse_with": "",
           "shopify_order_id": "", "shopify_order_name": "", "shopify_store": ""}

    if not _has_writeback_fields():
        out["reason_code"] = "not_installed"
        out["message"] = _NOT_MIGRATED_REASON
        return out

    row = frappe.db.get_value(
        REFUND_REQUEST, refund_name,
        ["sales_order", "net_refund_amount"], as_dict=True,
    )
    if not row:
        out["reason_code"] = "refund_request_missing"
        out["message"] = f"{REFUND_REQUEST} {refund_name} does not exist."
        return out

    out["amount"] = flt(row.get("net_refund_amount"))

    sales_order = str(row.get("sales_order") or "").strip()
    order_row = (frappe.db.get_value(
        "Sales Order", sales_order, ["shopify_order_id", "shopify_store"], as_dict=True
    ) or {}) if sales_order else {}
    out["shopify_order_id"] = str(order_row.get("shopify_order_id") or "").strip()
    out["shopify_store"] = str(order_row.get("shopify_store") or "").strip()

    if not out["shopify_order_id"]:
        out["reason_code"] = REASON_NOT_OURS
        out["message"] = (
            "This refund has no Shopify order behind it, so there is nothing to "
            "ask Shopify about."
        )
        return out

    # require_enabled=False for the reason in the docstring: this is the read a
    # person needs precisely while the payout is switched off.
    settings = _settings_for_store(out["shopify_store"], require_enabled=False)
    if not settings:
        out["reason_code"] = "writeback_unavailable_for_store"
        out["message"] = (
            f"No enabled Shopify Settings for {out['shopify_store'] or 'this store'}."
        )
        return out

    if not has_admin_api_credentials(settings):
        out["reason_code"] = "no_api_credentials"
        out["message"] = (
            f"{settings.name} has no Admin API credentials, so Shopify cannot "
            f"be asked what is refundable."
        )
        return out

    try:
        data = execute(
            settings,
            _REFUND_TARGETS_QUERY,
            {"orderId": gid("Order", out["shopify_order_id"])},
            operation="RefundTargets",
        )
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        out["reason_code"] = "query_failed"
        out["message"] = f"Could not read the order from Shopify: {exc}"
        return out

    order = (data or {}).get("order")
    if not order:
        out["reason_code"] = "shopify_order_not_found"
        out["message"] = (
            f"Shopify order {out['shopify_order_id']} was not found, or the "
            f"token cannot see it."
        )
        return out

    nodes = order.get("transactions")
    out["shopify_order_name"] = str(order.get("name") or "")
    out["transactions"] = transaction_summary(nodes)
    # A number, not a formatted string: this is read by a caller and by the
    # form, and `_money` returns text for messages.
    out["refundable_total"] = flt(
        _money(sum(_headroom(p) for p in refundable_parents(nodes)))
    )

    # The verdict the payout would reach, from the same function it would use —
    # so this can never say "fine" about a refund that would then refuse.
    plan = plan_refund(nodes, out["amount"])
    out["would_refuse_with"] = plan["problem_code"]
    out["message"] = plan["problem"] or (
        f"{_money(_paise(out['refundable_total']))} is refundable on this order "
        f"and the refund is {_money(_paise(out['amount']))} — this would send."
    )
    out["ok"] = True
    return out


@frappe.whitelist()
def get_refund_writeback_status(refund_name: str) -> dict:
    """
    Everything the Refund Request form needs to render its write-back banner and
    decide whether to show the button.  Read-only.
    """
    if not _has_writeback_fields():
        return {"is_shopify": False, "migrated": False}

    eligibility = check_eligibility(refund_name)
    row = frappe.db.get_value(
        REFUND_REQUEST, refund_name,
        [WRITEBACK_STATUS_FIELD, WRITEBACK_ERROR_FIELD, WRITEBACK_AT_FIELD,
         REFUND_GATEWAY_FIELD],
        as_dict=True,
    ) or {}

    return {
        # True only when we actually determined it.  A guard that returned before
        # the Sales Order lookup used to leave this false, which read as "not a
        # Shopify order" for orders that plainly were — check payout_owner, not
        # this, when the answer decides where money goes.
        "is_shopify": eligibility["payout_owner"] == OWNER_SHOPIFY,
        "migrated": True,
        "shopify_order_id": eligibility["shopify_order_id"],
        "shopify_store": eligibility["shopify_store"],
        "status": eligibility["status"],
        "refund_gid": eligibility["refund_gid"],
        "gateway": row.get(REFUND_GATEWAY_FIELD) or "",
        "written_back_at": row.get(WRITEBACK_AT_FIELD),
        "error": row.get(WRITEBACK_ERROR_FIELD) or "",
        "amount": eligibility["amount"],
        "can_write_back": eligibility["ok"],
        "reason": eligibility["reason"],
        "reason_code": eligibility["reason_code"],
        **_ownership(eligibility),
        "contract_version": CONTRACT_VERSION,
    }
