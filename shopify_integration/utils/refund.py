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

That refusing gate lives in payment_portals (portal_channel_blocked, third gate,
commit 1ed0e8b) and is not this app's to write.  Note what it does and does not
do: it **refuses** a Payment Portal refund on a Shopify order, pointing the
reader at Manual Portal Refund.  It does not delegate to this module, and no
dispatcher exists yet — so the only thing that calls write_back_refund is
writeback_now, the whitelisted endpoint behind the form button.  See
REFUND-DISPATCH-CONTRACT.md for the handshake that would change that, and for
why write_back_refund itself is deliberately not whitelisted.

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
CONTRACT_VERSION = 3

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
        "wrong_refund_status",
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
        "setup_failed",
    }),
    OUTCOME_FAILED_UNKNOWN: frozenset({
        "transport_error_after_send",
        "response_unverifiable",
        "unverified_previous_attempt",
    }),
    OUTCOME_IN_PROGRESS: frozenset({"claimed_elsewhere"}),
}

# The Refund Request status that means booked and paid.  Only then is ERPNext
# sure enough to tell Shopify.
REFUND_BOOKED_STATUS = "Completed"

# A refund that came *from* Shopify is already recorded there; writing it back
# would duplicate it.
CHANNEL_FROM_SHOPIFY = "Manual Portal Refund"

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
        return empty(
            f"No transaction on this order can take a refund — every row is a "
            f"refund, a void, unsuccessful, or already fully refunded. The "
            f"refund is {_money(wanted)}.",
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
    a key, and the stored-GID guard plus the worker claim carry idempotency on
    their own.  Turn it on once a live response has confirmed it.

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

    if (row.get("status") or "") != REFUND_BOOKED_STATUS:
        out["reason_code"] = "wrong_refund_status"
        out["reason"] = (
            f"Refund status is '{row.get('status') or 'blank'}', not "
            f"'{REFUND_BOOKED_STATUS}' — Shopify is told once ERPNext has booked "
            f"and paid the refund, not before."
        )
        return out

    if (row.get("refund_channel") or "") == CHANNEL_FROM_SHOPIFY:
        out["reason_code"] = "channel_is_manual_portal_refund"
        out["reason"] = (
            f"Refund channel is '{CHANNEL_FROM_SHOPIFY}' — this refund was made "
            f"in Shopify already, so writing it back would refund it twice."
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
    a payout and not as bookkeeping.  Nothing calls this automatically: there is
    no doc_events hook, deliberately, because deciding to pay somebody belongs
    with whatever owns the refund's money path, not with a save handler here.
    Today the only caller is the form button, via writeback_now; a dispatcher on
    the payment_portals side is the intended one.

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

    def fail_unsent(message, reason_code):
        """Nothing left this process, or Shopify explicitly declined it.  Nobody
        was paid, and a retry cannot double-pay."""
        _release_claim(refund_name, STATUS_FAILED, message)
        _log(settings, refund_name, shopify_order_id, "Failed", message)
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
            # check_eligibility has already refused amount <= 0, so only the two
            # headroom codes can reach here and both belong to failed_unsent.
            # Pinned rather than assumed: a code from the wrong outcome would
            # tell payment_portals to branch on something this outcome never
            # carries.
            problem_code = plan["problem_code"]
            if problem_code not in REASON_CODES[OUTCOME_FAILED_UNSENT]:
                problem_code = "insufficient_refundable"
            return fail_unsent(plan["problem"], problem_code)

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

        sent = True
        data = execute(
            settings,
            build_refund_mutation(),
            payload,
            operation="refundCreate",
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
        return fail_unsent(str(exc), "rejected_by_shopify")

    except ShopifyAPIError as exc:
        # Which phase raised decides whether the customer might have been paid.
        # Before the mutation was posted, nothing can have happened.  After, it
        # can — execute() retries internally, so a lost response on any attempt
        # may be hiding a refund that went through.
        if not sent:
            return fail_unsent(str(exc), "query_failed")
        if getattr(exc, "status_code", None) in (401, 403):
            # Rejected at the auth layer, before the document ran.  This one is
            # safe to call unsent, and saying so keeps a mis-scoped token from
            # parking refunds in Unverified where a person has to clear each one.
            return fail_unsent(str(exc), "not_authorised")
        return fail_unknown(str(exc), "transport_error_after_send")

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


# ── Clearing an unconfirmed attempt ───────────────────────────────────────────

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
    let the refunds/create webhook build a second Credit Note.

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
        refund_gid = str(shopify_refund_gid or "").strip()
        if not refund_gid:
            return refuse(
                "Recording this as paid needs the Shopify refund id (the GID "
                "from the order's refund in Shopify). Without it the "
                "credit-note guard cannot recognise the refund and the "
                "refunds/create webhook would create a second Credit Note."
            )
        _set_state(refund_name, **{
            REFUND_GID_FIELD: gid("Refund", refund_gid),
            WRITEBACK_STATUS_FIELD: STATUS_DONE,
            REFUND_GATEWAY_FIELD: str(gateway or "").strip()[:140],
            WRITEBACK_AT_FIELD: now_datetime(),
            WRITEBACK_ERROR_FIELD: (
                f"Unconfirmed attempt resolved as PAID by {who} after checking "
                f"the order in Shopify.{detail}"
            )[:1000],
        })
        message = f"Recorded as refunded in Shopify ({gid('Refund', refund_gid)})."
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
