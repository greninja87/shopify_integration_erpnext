"""
payment_entry.py — Create ERPNext Payment Entry from a Shopify order.

Trigger:
    Called from sales_order.py after the SO has been submitted, when
    Shopify Settings → Payment Entry → Enable Payment Entry Creation is on
    and the order's financial_status is 'paid' or 'partially_paid'.

Key Shopify payload fields we rely on:
    total_price               — the total amount owed for the order
    total_outstanding         — amount still unpaid (0 when fully paid)
    payment_gateway_names[]   — e.g. ["Cashfree Payments"], ["manual"]
    gateway                   — deprecated singular fallback
    processed_at / created_at — used as the Payment Entry reference_date

Amount paid = total_price − total_outstanding.

Gateway matching (first match wins):
  1. Tag Contains (higher priority): case-insensitive substring match of a row's
     tag_contains value against the order's 'tags' field. Needed because many
     Indian merchants' Cashfree / Razorpay / PayU integrations show
     payment_gateway_names == ["manual"] but tag the order "CASHFREE - UPI" etc.
  2. Shopify Gateway: case-insensitive exact match against payment_gateway_names[0].
  3. Fallback: default Mode of Payment + Bank Account from Shopify Settings.

Failures here NEVER block SO creation — the caller wraps this in try/except.
"""

import difflib

import frappe
from frappe.utils import flt, nowdate, getdate
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

_SIMILARITY_THRESHOLD = 0.6  # minimum ratio for fuzzy gateway name match


# ── Public entry point ─────────────────────────────────────────────────────────

def create_payment_entry_from_shopify(so, order: dict, settings) -> str:
    """
    :param so:       Submitted ERPNext Sales Order document
    :param order:    Shopify order payload (dict)
    :param settings: Shopify Settings document
    :return:         Payment Entry name, or "" if nothing was created
    """
    # 1. Amount paid (paisa-exact, derived from Shopify totals)
    amount_paid = _get_amount_paid(order, so)
    if amount_paid <= 0:
        return ""

    # so_outstanding is used only to:
    #   a) skip PE creation if the SO is already fully paid (advance_paid covers it)
    #   b) set ref_total on the PE reference row so ERPNext's validation
    #      (allocated_amount <= outstanding_amount) does not fire
    # It does NOT cap amount_paid — Shopify's total_price is the source of truth
    # for how much was actually collected.
    so_outstanding = flt(so.grand_total) - flt(so.advance_paid or 0)
    if so_outstanding <= 0:
        return ""

    # 2. Resolve gateway → Mode of Payment + Bank Account
    mode_of_payment, bank_account, gateway = _resolve_gateway_mapping(order, settings)

    # Bank account is mandatory — MOP is optional.
    if not bank_account:
        frappe.log_error(
            f"Shopify Payment Entry skipped for {so.name}: "
            f"no Bank / Cash Account configured for gateway '{gateway}'. "
            f"Add a row in Shopify Settings → Payment Entry → Gateway Mapping, "
            f"or set the Default Bank / Cash Account.",
            "Shopify: Payment Entry Skipped (No Account)"
        )
        return ""

    # Safety guard — refuse group accounts and non-Bank/Cash account types.
    # A Payment Entry posted to a group account is a disaster to reverse.
    acc = frappe.db.get_value(
        "Account",
        bank_account,
        ["is_group", "account_type", "disabled"],
        as_dict=True,
    )
    if not acc:
        frappe.log_error(
            f"Shopify Payment Entry skipped for {so.name}: "
            f"configured Bank / Cash Account '{bank_account}' does not exist. "
            f"Fix Shopify Settings → Payment Entry mapping for gateway '{gateway}'.",
            "Shopify: Payment Entry Skipped (Invalid Account)"
        )
        return ""
    if acc.is_group:
        frappe.log_error(
            f"Shopify Payment Entry skipped for {so.name}: "
            f"'{bank_account}' is a GROUP account (gateway '{gateway}'). "
            f"Group accounts cannot receive payments. Pick a leaf Bank / Cash account "
            f"in Shopify Settings → Payment Entry → Gateway Mapping.",
            "Shopify: Payment Entry Skipped (Group Account)"
        )
        return ""
    if acc.disabled:
        frappe.log_error(
            f"Shopify Payment Entry skipped for {so.name}: "
            f"'{bank_account}' is disabled (gateway '{gateway}').",
            "Shopify: Payment Entry Skipped (Disabled Account)"
        )
        return ""
    if (acc.account_type or "") not in ("Bank", "Cash"):
        frappe.log_error(
            f"Shopify Payment Entry skipped for {so.name}: "
            f"'{bank_account}' has account_type '{acc.account_type or 'blank'}' — "
            f"must be Bank or Cash (gateway '{gateway}').",
            "Shopify: Payment Entry Skipped (Wrong Account Type)"
        )
        return ""

    # 3. Build base PE via ERPNext helper (handles party, party_account, etc.)
    #    NOTE: get_payment_entry() sets paid_amount = SO outstanding and may produce
    #    multiple references rows summing to the full SO total.  We immediately
    #    fix both the top-level amounts AND the references child table below.
    pe = get_payment_entry(
        dt="Sales Order",
        dn=so.name,
        party_amount=amount_paid,
        bank_account=bank_account,
    )

    # 4. Override paid-to + mode of payment + reference from Shopify data
    if mode_of_payment:
        pe.mode_of_payment = mode_of_payment
    pe.paid_to         = bank_account
    pe.paid_amount     = amount_paid
    pe.received_amount = amount_paid

    # ── Create the PE as an unallocated advance (no SO reference) ────────────
    #
    # We intentionally do NOT link this PE to the Sales Order.  The Sales
    # Invoice is created immediately after the PE in our "After Payment Entry"
    # flow, and it uses allocate_advances_automatically = 1 to pull in this PE
    # at submit time.  ERPNext's advance-allocation engine finds PEs by
    # querying unallocated_amount > 0 for the customer — that only works when
    # the PE has no prior reference consuming its unallocated amount.
    #
    # If we linked the PE to the SO (as the old code did), unallocated_amount
    # would be 0 after submit, and the SI would find nothing to allocate.  The
    # result was an SI with outstanding = grand_total and no reconciliation.
    #
    # Without a SO reference the SO's advance_paid stays at 0, but since the
    # SI is created in the same request, the SO's billing_status transitions
    # to "Fully Billed" when the SI is submitted — the brief window where
    # advance_paid = 0 on the SO is never visible to the user.
    # 
    # FIX: We now keep the references to the Sales Order so that the Payment
    # Entry correctly shows as linked to the SO. The Sales Invoice will pull 
    # these advances automatically from the SO.
    pe.set("deductions", [])    # wipe any bridge/write-off rows ERPNext injected

    # Recompute totals:
    #   total_allocated_amount = 0  (no references)
    #   unallocated_amount     = paid_amount  (SI will consume this)
    #   difference_amount      = paid_amount - received_amount = 0 (same currency)
    pe.set_amounts()
    pe.difference_amount = 0

    pe.reference_no   = (order.get("name") or order.get("order_number") or str(order.get("id", "")))[:140]
    pe.reference_date = _get_order_date(order)

    # Carry Shopify context forward in remarks for audit
    pe.remarks = (
        f"Shopify payment via {gateway or 'Shopify'} — order {pe.reference_no}. "
        f"Auto-created from webhook."
    )

    # 5. Apply cost center from Shopify Settings if configured
    if settings.get("cost_center"):
        pe.cost_center = settings.cost_center
        # deductions were cleared above; apply cost_center to any surviving rows
        for d in (pe.get("deductions") or []):
            d.cost_center = settings.cost_center

    # Make sure the PE lands in the same company as the SO
    pe.company = so.company

    # Apply store-specific naming series if configured
    if settings.get("pe_naming_series"):
        pe.naming_series = settings.pe_naming_series

    # 6. Insert + (optionally) submit
    pe.flags.ignore_permissions = True
    pe.insert()

    if settings.get("auto_submit_payment_entry"):
        pe.submit()

    frappe.db.commit()  # nosemgrep: frappe-manual-commit — runs in background job; PE must persist before SI creation
    return pe.name


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_amount_paid(order: dict, so) -> float:
    """
    How much has been paid, per Shopify.

        amount_paid = total_price − total_outstanding

    Notes on Shopify fields:
      * total_outstanding is a top-level string (e.g. "0.00") in modern webhooks.
      * When financial_status == 'paid', total_outstanding is "0.00".
      * When financial_status == 'partially_paid', total_outstanding is the
        unpaid balance.
      * Shopify's total_price is the source of truth — we do NOT cap against
        so.grand_total, which can differ by a paisa due to GST split-rounding.
    """
    financial_status = (order.get("financial_status") or "").lower()
    total_price      = flt(order.get("total_price"))
    outstanding      = flt(order.get("total_outstanding"))

    # Primary path — read directly from Shopify's own fields.
    # amount_paid = total_price − total_outstanding.
    # We take this at face value: it is what Shopify actually collected,
    # and has nothing to do with ERPNext's grand_total (which can differ
    # by a paisa due to GST split-rounding).  No capping against grand_total.
    if total_price > 0:
        paid = round(total_price - outstanding, 2)
        if paid > 0:
            return paid

    # Fallback — paid but Shopify total_price field is missing/zero
    if financial_status == "paid":
        return flt(so.grand_total)

    # Fallback — partially_paid but total_outstanding field is missing:
    # sum the successful capture/sale transactions from the payload
    if financial_status == "partially_paid":
        txns = order.get("transactions") or []
        paid = round(sum(
            flt(t.get("amount"))
            for t in txns
            if (t.get("kind") in ("sale", "capture")
                and (t.get("status") or "").lower() == "success")
        ), 2)
        if paid > 0:
            return paid

    return 0.0


def _resolve_gateway_mapping(order: dict, settings):
    """
    Walk the configured gateway mapping rows and match against the order's gateway.
    Falls back to settings.default_mode_of_payment / settings.default_bank_account.

    Matching runs in two passes — the first match wins:

      Pass 1 (higher priority) — Tag Contains match
        Some integrations (Cashfree, Razorpay, PayU via custom Shopify apps)
        register orders with payment_gateway_names == ["manual"] and put the
        actual gateway name in the order 'tags' field, e.g. "CASHFREE - UPI".
        For these merchants, tag-based matching is the only reliable signal.
        Case-insensitive substring match against order.tags.

      Pass 2 — Shopify Gateway exact match
        Case-insensitive exact match against payment_gateway_names[0]
        (with 'gateway' as a deprecated fallback).

      Pass 2.5 — Similarity-based gateway match (fallback when exact match fails)
        Uses difflib.SequenceMatcher (threshold 0.6) plus containment detection
        ("cashfree" ⊆ "cashfree payments").  Only reached when no tag OR exact
        gateway row matched — never modifies the tag-based logic.

      Fallback — settings.default_mode_of_payment / settings.default_bank_account.

    Returns (mode_of_payment, bank_account, gateway_name_seen).
    """
    gateway_names = order.get("payment_gateway_names") or []
    primary = gateway_names[0] if gateway_names else ""
    gateway = (primary or order.get("gateway") or "").strip()
    gateway_key = gateway.lower()

    tags_raw = order.get("tags") or ""
    tags_key = tags_raw.lower()

    rows = settings.get("payment_gateway_mapping") or []

    # ── Pass 1: tag-based matching (higher priority) ──────────────────────
    if tags_key:
        for row in rows:
            tag_needle = (row.get("tag_contains") or "").strip().lower()
            if tag_needle and tag_needle in tags_key:
                return (
                    row.get("mode_of_payment") or "",
                    row.get("bank_account") or "",
                    gateway or f"tag:{row.get('tag_contains')}",
                )

    # ── Pass 2: gateway-name exact match ──────────────────────────────────
    if gateway_key:
        for row in rows:
            row_gateway = (row.get("shopify_gateway") or "").strip().lower()
            if row_gateway and row_gateway == gateway_key:
                return (
                    row.get("mode_of_payment") or "",
                    row.get("bank_account") or "",
                    gateway,
                )

    # ── Pass 2.5: similarity-based fallback (tag AND exact both failed) ──────
    # Only runs when there was a gateway string to compare — never when
    # gateway_key is empty, so it cannot silently swallow unrelated orders.
    if gateway_key:
        best_ratio = 0.0
        best_row   = None
        for row in rows:
            row_gateway = (row.get("shopify_gateway") or "").strip().lower()
            if not row_gateway:
                continue
            ratio = _gateway_similarity(gateway_key, row_gateway)
            if ratio > best_ratio:
                best_ratio = ratio
                best_row   = row
        if best_row and best_ratio >= _SIMILARITY_THRESHOLD:
            return (
                best_row.get("mode_of_payment") or "",
                best_row.get("bank_account") or "",
                gateway,
            )

    # No explicit match — fall back to default
    return (
        settings.get("default_mode_of_payment") or "",
        settings.get("default_bank_account") or "",
        gateway,
    )



def _get_order_date(order: dict) -> str:
    """Pick a usable date for Payment Entry reference_date."""
    created_at = order.get("processed_at") or order.get("created_at") or ""
    if created_at:
        try:
            return getdate(created_at[:10]).strftime("%Y-%m-%d")
        except Exception:
            pass
    return nowdate()


def _gateway_similarity(a: str, b: str) -> float:
    """
    Similarity score [0, 1] between two lowercase gateway name strings.

    Containment shortcut: if either string is a substring of the other
    (e.g. "cashfree" ⊆ "cashfree payments") we return 0.9 so it comfortably
    clears the threshold without requiring a full sequence match.
    """
    if a in b or b in a:
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()
