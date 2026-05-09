"""
gst.py — GST-based billing address resolution for Shopify orders.

When a Shopify order contains a GSTIN at a configured field path
(e.g. billing_address.company), this module:

  1. Validates the value against the standard 15-character GSTIN pattern.
  2. Provides get_gst_legal_name() so sales_order.py can fetch the GST-registered
     company name BEFORE customer creation, allowing get_or_create_customer() to
     create the ERPNext Customer with the correct name, type (Company), shipping
     address, and contact info in one pass.
  3. Provides resolve_billing_from_gstin() to find or create the GST-registered
     billing address and link it to the already-created customer.

Address resolution priority:
  Pass 1 — Local ERPNext Address.gstin match  (no external call)
  Pass 2 — India Compliance portal fetch       (requires IC + GSP credentials)
  Pass 3 — Fallback: None                      caller keeps Shopify billing address

The shipping address is NEVER touched — it always comes from Shopify.
Customer creation is NEVER done here — that is customer.py's responsibility.
"""

import re
import frappe

# Standard GSTIN: 2-digit state + 10-char PAN + entity number + Z + check digit
_GSTIN_RE = re.compile(
    r"^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}Z[A-Z\d]{1}$"
)

# Maps the first 2 digits of a GSTIN to the Indian state/UT name.
_STATE_CODE_MAP = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana",
    "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
    "13": "Nagaland", "14": "Manipur", "15": "Mizoram",
    "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman and Diu", "26": "Dadra and Nagar Haveli", "27": "Maharashtra",
    "28": "Andhra Pradesh", "29": "Karnataka", "30": "Goa",
    "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu",
    "34": "Puducherry", "35": "Andaman and Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh (New)",
    "38": "Ladakh", "97": "Other Territory", "99": "Centre Jurisdiction",
}


# ── Public API ─────────────────────────────────────────────────────────────────

def extract_gstin(order: dict, settings) -> str | None:
    """
    Extract a GSTIN from the Shopify order at the path configured in
    settings.gst_field_path.  Returns the GSTIN (uppercase) or None.

    Non-GSTIN values at the path (e.g. a plain company name) are silently
    ignored — only strings matching the 15-character GSTIN format are returned.
    """
    path = (settings.get("gst_field_path") or "").strip()
    if not path:
        return None

    from shopify_integration.utils.sales_order import _get_nested_value
    raw = _get_nested_value(order, path)
    if not raw or not isinstance(raw, str):
        return None

    candidate = raw.strip().upper()
    if _GSTIN_RE.match(candidate):
        return candidate
    return None


def get_gst_customer_info(gstin: str) -> dict:
    """
    Return the GST-registered name and ERPNext customer_type for a GSTIN.

    customer_type is derived from constitution_of_business the same way
    India Compliance's Customer form JS does it:
      • "Proprietorship" / "Hindu Undivided Family" → "Individual"
      • Everything else (Pvt Ltd, LLP, Partnership, Trust, etc.) → "Company"

    Returns:
        {
            "legal_name":    str | None,    # GST-registered business name
            "customer_type": "Individual" | "Company",
        }
    Defaults to {"legal_name": None, "customer_type": "Individual"} when
    IC is not installed / portal unavailable / GSTIN not found.

    Called BEFORE customer creation so the customer is created with the
    correct name AND type in one pass.

    NOTE: The fast-path (local Address already exists) only returns the name;
    customer_type from it doesn't matter because get_or_create_customer() step 0
    would have already returned the existing customer before reaching the
    creation path.  The portal path is what matters for new customers.
    """
    # Fast path — existing local Address already has the title
    if frappe.db.has_column("Address", "gstin"):
        addr = frappe.db.get_value(
            "Address", {"gstin": gstin, "disabled": ["!=", 1]}, "address_title"
        )
        if addr:
            # customer_type is irrelevant here (existing customer returned by step 0)
            return {"legal_name": addr, "customer_type": "Individual"}

    # IC portal fetch — use constitution_of_business for customer_type
    portal_data = _fetch_from_ic_portal(gstin)
    if portal_data:
        return {
            "legal_name":    portal_data.get("business_name") or None,
            "customer_type": _constitution_to_customer_type(
                portal_data.get("constitution_of_business") or ""
            ),
        }

    return {"legal_name": None, "customer_type": "Individual"}


def resolve_billing_from_gstin(gstin: str, customer_name: str) -> str | None:
    """
    Find or create the GST-registered billing address for a GSTIN and link
    it to the given ERPNext Customer.

    The customer is assumed to already exist (created by get_or_create_customer).
    This function only handles the billing address — shipping is never touched.

    Returns the ERPNext Address name, or None if unavailable.
    """
    # Pass 1 — local ERPNext Address lookup (no external call)
    if frappe.db.has_column("Address", "gstin"):
        addr_name = frappe.db.get_value(
            "Address", {"gstin": gstin, "disabled": ["!=", 1]}, "name"
        )
        if addr_name:
            # Ensure this address is also linked to the current customer
            _ensure_address_linked(addr_name, customer_name)
            return addr_name

    # Pass 2 — India Compliance portal fetch
    portal_data = _fetch_from_ic_portal(gstin)
    if portal_data:
        return _create_gst_address(gstin, portal_data, customer_name)

    return None


# ── Internal helpers ───────────────────────────────────────────────────────────

def _ensure_address_linked(addr_name: str, customer_name: str):
    """Add a Dynamic Link from Address to Customer if not already present.

    Uses the Document layer (get_doc → append → save) so Frappe correctly
    auto-generates a `name` for the child row.  frappe.db.insert() bypasses
    naming and would leave the row with a NULL name, causing a DB error.
    """
    # Fast-path: check before loading the full doc
    exists = frappe.db.exists(
        "Dynamic Link",
        {
            "parenttype":   "Address",
            "parent":       addr_name,
            "link_doctype": "Customer",
            "link_name":    customer_name,
        },
    )
    if exists:
        return

    addr_doc = frappe.get_doc("Address", addr_name)
    # Double-check in the loaded doc to guard against concurrent inserts
    for link in addr_doc.get("links") or []:
        if link.link_doctype == "Customer" and link.link_name == customer_name:
            return

    addr_doc.append("links", {
        "link_doctype": "Customer",
        "link_name":    customer_name,
    })
    addr_doc.flags.ignore_permissions = True
    addr_doc.save()


def _fetch_from_ic_portal(gstin: str) -> dict | None:
    """
    Attempt to fetch taxpayer details from India Compliance's GST portal API.

    IC (v2+) exposes _get_gstin_info(gstin, throw_error=False) which returns a
    frappe._dict with keys: gstin, business_name, gst_category, status,
    permanent_address (dict), all_addresses (list).

    Returns the response dict or None if IC is not installed / not configured /
    call fails.
    """
    try:
        from india_compliance.gst_india.utils.gstin_info import _get_gstin_info
        result = _get_gstin_info(gstin, throw_error=False)
        if result and isinstance(result, dict) and result.get("business_name"):
            return result
    except Exception:
        pass
    return None


def _create_gst_address(gstin: str, portal_data: dict, customer_name: str) -> str | None:
    """
    Create an ERPNext Address from India Compliance portal data, stamp the
    GSTIN on it, and link it to the Customer.  Returns the new Address name.

    IC v2+ portal_data shape:
        business_name     — legal / trade name (title-cased by IC)
        permanent_address — dict with address_line1, address_line2, city,
                            state, pincode, country

    If the address already exists (duplicate gstin check), returns the
    existing address name instead of creating a duplicate.
    """
    # Guard: re-check local in case of concurrent calls
    existing = frappe.db.get_value("Address", {"gstin": gstin}, "name")
    if existing:
        _ensure_address_linked(existing, customer_name)
        return existing

    try:
        legal_name = portal_data.get("business_name") or customer_name

        perm    = portal_data.get("permanent_address") or {}
        line1   = perm.get("address_line1") or "As per GST records"
        line2   = perm.get("address_line2") or ""
        city    = perm.get("city") or ""
        state   = perm.get("state") or _state_from_gstin(gstin)
        pincode = str(perm.get("pincode") or "")

        addr = frappe.get_doc({
            "doctype":       "Address",
            "address_title": legal_name,
            "address_type":  "Billing",
            "address_line1": line1,
            "address_line2": line2,
            "city":          city,
            "state":         state,
            "pincode":       pincode,
            "country":       "India",
            "gstin":         gstin,
            "links": [{
                "link_doctype": "Customer",
                "link_name":    customer_name,
            }],
        })
        addr.flags.ignore_permissions = True
        addr.insert()
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — runs in background job; address must persist before SO links it
        return addr.name

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: GST Address creation failed — {gstin}",
        )
        return None


def _state_from_gstin(gstin: str) -> str:
    """Derive the Indian state name from the first 2 digits of the GSTIN."""
    return _STATE_CODE_MAP.get(gstin[:2], "")


# Sole proprietors and HUFs register individually; all other constitution types
# (Pvt Ltd, LLP, Partnership, Trust, etc.) represent organisations.
_INDIVIDUAL_CONSTITUTIONS = frozenset({
    "proprietorship",
    "hindu undivided family",
    "huf",
    "individual",
})


def _constitution_to_customer_type(constitution: str) -> str:
    """
    Map GST portal constitution_of_business → ERPNext customer_type.

    Mirrors the mapping India Compliance uses when you enter a GSTIN on
    the Customer form: Proprietorship / HUF → Individual, everything else
    (Private Limited, LLP, Partnership, Trust, etc.) → Company.

    Falls back to "Company" for any unrecognised string because in practice
    the vast majority of B2B GST registrations are corporate entities.
    """
    if not constitution:
        return "Company"   # safe default for unknown constitution
    return "Individual" if constitution.strip().lower() in _INDIVIDUAL_CONSTITUTIONS else "Company"
