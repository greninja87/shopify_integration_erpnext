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
Cashfree payment stays with the Cashfree API.  The refusing half of that guard
lives in payment_portals and is not this app's to write.  Until it exists,
enable_refund_writeback must stay off.

Snapmint is not known to work this way at all: there has never been a Snapmint
refund event in production, and #6518's ₹12,999 went out by NEFT.  Nothing here
distinguishes a Snapmint order from an OCC one — the parent transaction reads
"manual" for both — so that remains an open question rather than a guard.

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

# A gateway of "manual" means Shopify recorded a payment nobody charged.
_MANUAL_GATEWAY = "manual"

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


def gateway_moves_money(gateway) -> bool:
    """
    Whether SHOPIFY itself charges a gateway back for this refund.

    False for "manual" and for blank/None; True otherwise.  Blank reads as
    manual because an absent gateway is not evidence of a real one.

    Read the name narrowly.  This is NOT "did the customer get paid" — on this
    store the answer to that is yes either way, because the Cashfree-OCC app
    bridges a "manual" Shopify refund into a real Cashfree refund (see the
    module docstring).  What this distinguishes is whether the money leaves
    through a gateway Shopify itself holds, or through the OCC app afterwards.
    Never surface it to a user as "money moved" / "no money moved".
    """
    return (str(gateway or "").strip().lower() or _MANUAL_GATEWAY) != _MANUAL_GATEWAY


def plan_refund(nodes, amount) -> dict:
    """
    Allocate `amount` across refundable parents, capped per parent by its
    maximumRefundableV2.

    :return: {"transactions": [{"parentId", "kind", "gateway", "amount"}],
              "gateways": [gateway names actually allocated, in order],
              "moves_money": bool, "allocated": float, "problem": str | None}

    `moves_money` is True when ANY allocated parent's gateway is one Shopify
    charges back itself — see gateway_moves_money for how narrowly to read that.
    A parent that got no allocation does not count, however real its gateway.

    Allocates nothing and sets `problem` when the parents' combined headroom is
    short of `amount`: a partial Shopify record is worse than none, because it
    looks settled and is not.  The message names both figures, since the only
    useful next question is "short by how much".
    """
    def empty(problem):
        return {"transactions": [], "gateways": [], "moves_money": False,
                "allocated": 0.0, "problem": problem}

    wanted = _paise(amount)
    parents = refundable_parents(nodes)
    available = sum(_headroom(p) for p in parents)

    if wanted <= 0:
        return empty(
            f"The refund amount is {_money(wanted)} — there is nothing to record "
            f"in Shopify."
        )

    if available < wanted:
        return empty(
            f"Shopify shows only {_money(available)} still refundable on this "
            f"order, but the refund is {_money(wanted)}. Nothing was sent — a "
            f"partial refund in Shopify would look settled when it is not."
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
        "moves_money": any(gateway_moves_money(g) for g in gateways),
        "allocated": flt(_money(wanted - remaining)),
        "problem": None,
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


def idempotency_key(refund_name: str, amount) -> str:
    """A key that changes when the refund changes, so a retry cannot double up."""
    return f"{refund_name}:{_money(_paise(amount))}"


def build_refund_mutation(key: str = "") -> str:
    """
    The refundCreate document, with the @idempotent directive only when asked.

    refundCreate accepts `@idempotent(key: "…")`, but that could not be
    confirmed against the configured API version, and an unknown directive is a
    query-level error — which execute() raises on, so it would fail *every*
    write-back rather than degrade.  It is therefore off unless a caller passes
    a key, and the stored-GID guard plus the worker claim carry idempotency on
    their own.  Turn it on once a live response has confirmed it.

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

def check_eligibility(refund_name: str, settings=None) -> dict:
    """
    Can this Refund Request be written back to Shopify right now?

    Returns {"ok", "reason", "settings", "shopify_order_id", "amount", "note",
             "status", "refund_gid"}.  Read-only — safe to call from the client
    on every form refresh.

    Every guard in the brief's §6 lives here, and every one returns a reason
    rather than raising.  A payout path that throws on an edge case is worse
    than one that refuses.
    """
    out = {"ok": False, "reason": "", "settings": None, "shopify_order_id": "",
           "amount": 0.0, "note": "", "status": "", "refund_gid": ""}

    if not _has_writeback_fields():
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
        out["reason"] = f"{REFUND_REQUEST} {refund_name} does not exist."
        return out

    out["status"] = row.get(WRITEBACK_STATUS_FIELD) or ""
    out["refund_gid"] = (row.get(REFUND_GID_FIELD) or "").strip()
    out["amount"] = flt(row.get("net_refund_amount"))
    out["note"] = str(row.get("reason_note") or "")

    # Idempotency first, and deliberately before everything else: it is the one
    # guard that must hold even if the document has since been edited into a
    # state the other guards would reject.
    if out["refund_gid"]:
        out["reason"] = (
            f"Already written back to Shopify as {out['refund_gid']}. No further "
            f"refund is ever sent for this document."
        )
        return out

    if cint(row.get("docstatus")) != 1:
        out["reason"] = "Only a submitted Refund Request is written back."
        return out

    if (row.get("status") or "") != REFUND_BOOKED_STATUS:
        out["reason"] = (
            f"Refund status is '{row.get('status') or 'blank'}', not "
            f"'{REFUND_BOOKED_STATUS}' — Shopify is told once ERPNext has booked "
            f"and paid the refund, not before."
        )
        return out

    if (row.get("refund_channel") or "") == CHANNEL_FROM_SHOPIFY:
        out["reason"] = (
            f"Refund channel is '{CHANNEL_FROM_SHOPIFY}' — this refund was made "
            f"in Shopify already, so writing it back would refund it twice."
        )
        return out

    if out["amount"] <= 0:
        out["reason"] = (
            f"Net refund to customer is {out['amount']:.2f} — there is nothing "
            f"to send."
        )
        return out

    sales_order = str(row.get("sales_order") or "").strip()
    if not sales_order:
        out["reason"] = "No Sales Order on this refund, so no Shopify order to refund."
        return out

    order = frappe.db.get_value(
        "Sales Order", sales_order, ["shopify_order_id", "shopify_store"], as_dict=True
    ) or {}
    shopify_order_id = str(order.get("shopify_order_id") or "").strip()
    if not shopify_order_id:
        out["reason"] = (
            f"Sales Order {sales_order} has no Shopify order id — this is a "
            f"payment link or a direct gateway payment, and its refund does not "
            f"go through Shopify."
        )
        return out
    out["shopify_order_id"] = shopify_order_id

    shop_domain = str(order.get("shopify_store") or "").strip()
    settings = settings or _settings_for_store(shop_domain)
    if not settings:
        out["reason"] = (
            f"No enabled Shopify Settings for store '{shop_domain or '?'}' with "
            f"Refund Write-Back switched on. Nothing was sent."
        )
        return out

    if not has_admin_api_credentials(settings):
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


@frappe.whitelist()
def write_back_refund(refund_name: str, triggered_by: str = "manual") -> dict:
    """
    Tell Shopify about one booked ERPNext refund.

    **This pays the customer.**  A successful refundCreate on these orders is
    bridged into a real Cashfree refund by the Cashfree-OCC app, so treat it as
    a payout and not as bookkeeping.  Nothing calls this automatically: there is
    no doc_events hook, deliberately, because deciding to pay somebody belongs
    with whatever owns the refund's money path, not with a save handler here.
    Today the only caller is the form button; a dispatcher on the payment_portals
    side is the intended one.

    Idempotent and safe to call twice: shopify_refund_gid being set is a hard
    stop, and the worker claim stops two callers racing.  Never raises — every
    outcome comes back as a result dict so the caller can log it.

    Whitelisted, so it checks submit permission on the document itself rather
    than trusting the caller to have done it.  It deliberately takes no
    `settings` argument: the store is resolved from the refund's own Sales Order,
    so an HTTP caller cannot aim this at a different store's credentials.

    :return: {"ok", "status", "refund_gid", "gateway", "message",
              "refund_request"}
    """
    def result(ok, status, message, refund_gid="", gateway=""):
        return {"ok": ok, "status": status, "message": message,
                "refund_gid": refund_gid, "gateway": gateway,
                "refund_request": refund_name}

    # Availability first.  On a site without payment_portals — or one where the
    # patch has not run — frappe.has_permission on a missing DocType fails, and
    # "you lack permission" would be the wrong diagnosis for "this app is inert
    # here".
    if not _has_writeback_fields():
        return result(False, STATUS_SKIPPED, _NOT_MIGRATED_REASON)

    # Refused before anything else is read or written: an unauthorised caller
    # must not be able to pay a customer, nor to leave a mark on the document
    # saying they tried.  Checked without throw so the never-raises contract
    # holds.
    try:
        permitted = frappe.has_permission(REFUND_REQUEST, "submit", doc=refund_name)
    except Exception:
        permitted = False
    if not permitted:
        return result(False, STATUS_SKIPPED,
                      f"You do not have submit permission on {REFUND_REQUEST} "
                      f"{refund_name}, so no refund was sent.")

    try:
        eligibility = check_eligibility(refund_name)
        if not eligibility["ok"]:
            # An already-written refund keeps its Done status and its GID; every
            # other refusal is recorded as a Skip with its reason.
            if eligibility["refund_gid"]:
                return result(False, STATUS_DONE, eligibility["reason"],
                              refund_gid=eligibility["refund_gid"])
            _record_skip(refund_name, eligibility["reason"])
            return result(False, STATUS_SKIPPED, eligibility["reason"])

        settings = eligibility["settings"]
        shopify_order_id = eligibility["shopify_order_id"]
        amount = eligibility["amount"]
        note = eligibility["note"]

        if not _claim(refund_name):
            return result(False, STATUS_PENDING,
                          "Another process is already writing this refund back to "
                          "Shopify.")
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"Shopify: Refund Write-Back Setup Failed — {refund_name}"
        )
        return result(False, "", "Could not start the write-back; see the Error Log.")

    def fail(message):
        _release_claim(refund_name, STATUS_FAILED, message)
        _log(settings, refund_name, shopify_order_id, "Failed", message)
        return result(False, STATUS_FAILED, message)

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
            return fail(
                f"Shopify order {shopify_order_id} not found — it may have been "
                f"deleted, or the token cannot see it. Nothing was refunded."
            )

        plan = plan_refund(order.get("transactions"), amount)
        if plan["problem"]:
            return fail(plan["problem"])

        payload = build_refund_input(
            order_gid,
            plan,
            note,
            notify=bool(cint(settings.get("notify_customer_on_refund"))),
            fallback_note=f"Refund {refund_name}",
        )
        if not payload:
            return fail(
                "Nothing could be allocated to a refundable transaction on "
                "this order."
            )

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
            # HTTP 200, no errors, no userErrors, no refund.  Failed, never Done.
            return fail(
                "Shopify accepted the request but returned no refund — nothing "
                "was refunded. Treat this as a failure, not a success, and check "
                "the order in Shopify before retrying."
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
        return result(True, STATUS_DONE, message,
                      refund_gid=refund_gid, gateway=", ".join(gateways))

    except ShopifyUserError as exc:
        return fail(str(exc))
    except ShopifyAPIError as exc:
        return fail(str(exc))
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"Shopify: Refund Write-Back Failed — {refund_name}"
        )
        return fail("Unexpected error during the write-back; see the Error Log.")


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


# ── Whitelisted endpoints (form button / client status) ───────────────────────

@frappe.whitelist()
def writeback_now(refund_name: str) -> dict:
    """
    Write one refund back on demand — the form's button.

    Runs inline rather than enqueued so the user gets the real outcome back
    instead of an optimistic "queued"; one refund is two GraphQL calls, well
    inside a web request.  Requires submit permission on the document, because
    this moves money.
    """
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
        "is_shopify": bool(eligibility["shopify_order_id"]),
        "migrated": True,
        "shopify_order_id": eligibility["shopify_order_id"],
        "status": eligibility["status"],
        "refund_gid": eligibility["refund_gid"],
        "gateway": row.get(REFUND_GATEWAY_FIELD) or "",
        "written_back_at": row.get(WRITEBACK_AT_FIELD),
        "error": row.get(WRITEBACK_ERROR_FIELD) or "",
        "amount": eligibility["amount"],
        "can_write_back": eligibility["ok"],
        "reason": eligibility["reason"],
    }
