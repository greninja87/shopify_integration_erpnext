"""
customer.py — Customer matching and creation for Shopify orders.

Customer name source priority:
  1. billing_address.name  (most reliable — what customer typed at checkout)
  2. customer.first_name + customer.last_name  (Shopify profile — can be stale/wrong)
  3. email → "Shopify Customer {id}"

Matching priority:
  1. Shopify Customer ID (fastest if already synced)
  2. Phone number (primary unique identifier)
  3. Email address
  4. Create new customer

Address behaviour:
  - find_or_create_address_for_order() is the single entry point for all address
    work, called for both new and repeat customers.  It matches existing addresses
    by content (address_line1 + city + pincode, case-insensitive) so repeat orders
    from the same customer with the same address reuse the existing record, while
    orders with a new address get a new record with a unique title.
  - Billing address created/found per order; marked is_primary_address=1 only on
    first creation for that customer.
  - Shipping address created/found per order separately when different from billing.
  - When billing == shipping, billing address is marked is_shipping_address=1.
"""

import frappe


def get_or_create_customer(
    shopify_customer: dict,
    billing_address: dict,
    shipping_address: dict,
    settings,
    gstin: str = None,
    gst_legal_name: str = None,
    gst_customer_type: str = "Individual",
) -> str:
    """
    Find existing ERPNext customer or create one.
    Returns the customer name (ERPNext Customer docname).

    :param shopify_customer:  The 'customer' dict from Shopify order JSON
    :param billing_address:   order.billing_address dict
    :param shipping_address:  order.shipping_address dict
    :param settings:          Shopify Settings document
    :param gstin:             Validated GSTIN (if present — triggers B2B flow)
    :param gst_legal_name:    GST-registered legal name (overrides Shopify name)
    :param gst_customer_type: "Individual" or "Company" derived from IC portal
                              constitution_of_business (Proprietorship/HUF →
                              Individual, everything else → Company)
    """
    if not shopify_customer:
        return _get_default_customer(settings)

    # ── Extract identity fields ────────────────────────────────────────────────
    phone = _clean_phone(
        shopify_customer.get("phone") or
        billing_address.get("phone") or
        (shopify_customer.get("default_address") or {}).get("phone") or ""
    )
    email = (
        shopify_customer.get("email") or
        shopify_customer.get("contact_email") or ""
    )
    shopify_id = str(shopify_customer.get("id", ""))

    # Person name for Contact — always from the Shopify customer profile.
    # For B2B (Company) orders the customer_name is the GST legal name, but the
    # Contact person should be the individual who actually placed the order.
    profile_first       = (shopify_customer.get("first_name") or "").strip()
    profile_last        = (shopify_customer.get("last_name") or "").strip()
    shopify_person_name = f"{profile_first} {profile_last}".strip()

    # ── 0. GST match — highest priority for B2B orders ────────────────────────
    # If an Address with this GSTIN is already in ERPNext and links to a Customer,
    # return that customer immediately.  This correctly handles repeat B2B orders
    # from the same GST-registered company.
    if gstin and frappe.db.has_column("Address", "gstin"):
        gst_addr = frappe.db.get_value(
            "Address", {"gstin": gstin, "disabled": ["!=", 1]}, "name"
        )
        if gst_addr:
            linked = frappe.db.get_value(
                "Dynamic Link",
                {
                    "parenttype": "Address",
                    "parent":     gst_addr,
                    "link_doctype": "Customer",
                },
                "link_name",
            )
            if linked:
                _update_shopify_fields(linked, shopify_id, phone, email)
                return linked

    # ── GST B2B path — skip individual matching entirely ─────────────────────
    # When a GSTIN is present the customer represents a company, not the individual
    # who placed the order.  Matching by phone/email would return an existing B2C
    # individual record (wrong type, wrong name).  Pass 0 above already handled
    # the "repeat B2B customer" case via GSTIN → Address → Customer lookup.
    # Reaching here with gst_legal_name means: no existing GSTIN match → create
    # a fresh company customer with Shopify's contact info and shipping address.
    if gst_legal_name:
        return _create_customer(
            full_name=gst_legal_name,
            phone=phone,
            email=email,
            shopify_id=shopify_id,
            billing_address=billing_address,
            shipping_address=shipping_address,
            settings=settings,
            contact_person_name=shopify_person_name,
            customer_type=gst_customer_type,
        )

    # ── B2C path — name from Shopify, Individual type ────────────────────────
    billing_name  = (billing_address or {}).get("name", "").strip()
    full_name     = (
        billing_name or shopify_person_name or email or
        f"Shopify Customer {shopify_id}"
    )

    # ── 1. Match by Shopify Customer ID ───────────────────────────────────────
    if shopify_id:
        existing = frappe.db.get_value(
            "Customer", {"shopify_customer_id": shopify_id}, "name"
        )
        if existing:
            return existing

    # ── 2. Match by phone ─────────────────────────────────────────────────────
    if phone:
        existing = frappe.db.get_value(
            "Customer", {"shopify_phone": phone}, "name"
        )
        if existing:
            _update_shopify_fields(existing, shopify_id, phone, email)
            return existing

        existing = frappe.db.get_value(
            "Customer", {"mobile_no": phone}, "name"
        )
        if existing:
            _update_shopify_fields(existing, shopify_id, phone, email)
            return existing

    # ── 3. Match by email ─────────────────────────────────────────────────────
    if email:
        existing = frappe.db.get_value(
            "Customer", {"shopify_email": email}, "name"
        )
        if existing:
            _update_shopify_fields(existing, shopify_id, phone, email)
            return existing

        existing = frappe.db.get_value(
            "Customer", {"email_id": email}, "name"
        )
        if existing:
            _update_shopify_fields(existing, shopify_id, phone, email)
            return existing

    # ── 4. Create new individual customer ─────────────────────────────────────
    return _create_customer(
        full_name=full_name,
        phone=phone,
        email=email,
        shopify_id=shopify_id,
        billing_address=billing_address,
        shipping_address=shipping_address,
        settings=settings,
        contact_person_name=shopify_person_name,
    )


def _create_customer(
    full_name, phone, email, shopify_id,
    billing_address, shipping_address, settings,
    contact_person_name="",
    customer_type="Individual",
):
    """Create a new ERPNext Customer from Shopify data.

    customer_type is "Individual" for B2C and most B2B Proprietorship/HUF registrations,
    "Company" for Pvt Ltd / LLP / Partnership / Trust etc.  Derived from IC portal's
    constitution_of_business — mirrors what ERPNext's Customer form does when you
    manually enter a GSTIN.  Defaults to "Individual" (safe fallback when IC is not
    available or GSTIN is absent).
    """
    customer_doc = {
        "doctype":        "Customer",
        "customer_name":  full_name,
        "customer_type":  customer_type,
        "customer_group": settings.customer_group or "All Customer Groups",
        "territory":      settings.territory or "All Territories",
        "mobile_no":      phone,
        "email_id":       email,
        # Shopify custom fields
        "shopify_customer_id": shopify_id,
        "shopify_phone":       phone,
        "shopify_email":       email,
    }
    # Apply customer naming series if configured
    customer_naming_series = settings.get("customer_naming_series") or ""
    if customer_naming_series:
        customer_doc["naming_series"] = customer_naming_series

    customer = frappe.get_doc(customer_doc)
    customer.flags.ignore_permissions = True
    customer.insert()
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — runs in background job; customer must persist before SO creation

    # Create addresses via the unified find-or-create path so the same dedup
    # logic applies whether this is a new or repeat customer.
    billing_addr_name  = None
    shipping_addr_name = None

    if billing_address:
        billing_addr_name = find_or_create_address_for_order(
            customer_name=customer.name,
            shopify_address=billing_address,
            address_type="Billing",
            is_primary=True,
            is_shipping=False,
        )

    if shipping_address and addresses_are_different(billing_address, shipping_address):
        shipping_addr_name = find_or_create_address_for_order(
            customer_name=customer.name,
            shopify_address=shipping_address,
            address_type="Shipping",
            is_primary=False,
            is_shipping=True,
        )
    elif billing_addr_name:
        # billing == shipping: mark the billing address as shipping too
        frappe.db.set_value("Address", billing_addr_name, "is_shipping_address", 1)

    # Create contact — use the Shopify customer's first+last name (passed in as
    # contact_person_name) so the Contact shows the real person, not the company
    # or GST legal name.  Fall back to billing_address.name, then full_name.
    _contact_name = (
        contact_person_name or
        (billing_address or {}).get("name", "").strip() or
        full_name
    )
    _create_contact(customer.name, _contact_name, phone, email)

    return customer.name


def _create_address(
    customer_name: str,
    address: dict,
    address_type: str,
    is_primary: bool,
    is_shipping: bool
) -> str:
    """
    Create a new ERPNext Address linked to the customer and return its name.
    Title is unique (sequential suffix) so a customer may have multiple
    billing or shipping addresses without collision.
    Returns "" on failure.
    """
    try:
        suffix     = "Billing" if address_type == "Billing" else "Shipping"
        addr_title = _unique_address_title(customer_name, suffix)

        # Shopify sends province = full state name (e.g. "Gujarat"),
        # province_code = abbreviated code (e.g. "GJ").
        # India Compliance reads `gst_state` (full name) to determine
        # intra-state (CGST+SGST) vs inter-state (IGST) for B2C orders.
        # Normalise Shopify province to the exact name IC expects.
        state_name = _normalise_gst_state(address.get("province", ""))

        addr_doc = frappe.get_doc({
            "doctype":           "Address",
            "address_title":     addr_title,
            "address_type":      address_type,
            "address_line1":     address.get("address1", ""),
            "address_line2":     address.get("address2", "") or "",
            "city":              address.get("city", ""),
            "state":             state_name,
            "pincode":           address.get("zip", ""),
            "country":           address.get("country_name") or address.get("country", ""),
            "phone":             _clean_phone(address.get("phone", "")),
            "is_primary_address":  1 if is_primary else 0,
            "is_shipping_address": 1 if is_shipping else 0,
            # India Compliance field — used for GST state determination
            "gst_state":         state_name,
            "links": [{
                "link_doctype": "Customer",
                "link_name":    customer_name
            }]
        })
        addr_doc.flags.ignore_permissions = True
        addr_doc.insert()

        # Mark the billing address as primary on the customer
        if is_primary:
            frappe.db.set_value("Customer", customer_name, "customer_primary_address", addr_doc.name)

        return addr_doc.name

    except Exception:
        frappe.log_error(frappe.get_traceback(), "Shopify: Address Creation Failed")
        return ""


def find_or_create_address_for_order(
    customer_name: str,
    shopify_address: dict,
    address_type: str,
    is_primary: bool = False,
    is_shipping: bool = False,
) -> str:
    """
    Single entry point for all per-order address work (new and repeat customers).

    1. Fetches every Address linked to `customer_name`.
    2. Compares each against `shopify_address` using normalised field matching
       (address_line1 + city + pincode, case-insensitive).
    3. Match found  → returns existing address name unchanged.
    4. No match     → creates a new Address with a unique title and returns it.

    Returns "" if shopify_address is empty/has no street line, or on creation failure.
    """
    if not shopify_address or not (shopify_address.get("address1") or "").strip():
        return ""

    # Fetch all addresses already linked to this customer.
    linked = frappe.get_all(
        "Dynamic Link",
        filters={
            "link_doctype": "Customer",
            "link_name":    customer_name,
            "parenttype":   "Address",
        },
        pluck="parent",
    )

    if linked:
        existing = frappe.get_all(
            "Address",
            filters={"name": ["in", linked]},
            fields=["name", "address_line1", "city", "pincode"],
        )
        for addr in existing:
            if _shopify_matches_erpnext_address(shopify_address, addr):
                return addr["name"]

    # No matching address found — create a new one.
    return _create_address(
        customer_name=customer_name,
        address=shopify_address,
        address_type=address_type,
        is_primary=is_primary,
        is_shipping=is_shipping,
    )


def _shopify_matches_erpnext_address(shopify_addr: dict, erpnext_addr: dict) -> bool:
    """
    Return True if a Shopify address dict matches an ERPNext Address record
    (supplied as a dict with keys address_line1, city, pincode).

    Matches on address_line1 + city + pincode, all normalised (strip + lower).
    Falls back to address_line1 + city when either side has no pincode.
    Returns False when the street line is missing on either side.
    """
    def n(v):
        return (v or "").strip().lower()

    s_line1 = n(shopify_addr.get("address1"))
    s_city  = n(shopify_addr.get("city"))
    s_zip   = n(shopify_addr.get("zip"))

    e_line1 = n(erpnext_addr.get("address_line1"))
    e_city  = n(erpnext_addr.get("city"))
    e_zip   = n(erpnext_addr.get("pincode"))

    if not s_line1 or not e_line1:
        return False

    if s_zip and e_zip:
        return s_line1 == e_line1 and s_city == e_city and s_zip == e_zip

    # One side is missing pincode — match on street + city only.
    return s_line1 == e_line1 and s_city == e_city


def _unique_address_title(customer_name: str, suffix: str) -> str:
    """
    Return the next available address_title of the form
    '{customer_name}-{suffix}', '{customer_name}-{suffix}-2', etc.
    Scans sequentially so every customer can have an unlimited number of
    billing/shipping addresses without collision.
    """
    base = f"{customer_name}-{suffix}"
    if not frappe.db.exists("Address", {"address_title": base}):
        return base
    counter = 2
    while True:
        candidate = f"{base}-{counter}"
        if not frappe.db.exists("Address", {"address_title": candidate}):
            return candidate
        counter += 1


def addresses_are_different(addr1: dict, addr2: dict) -> bool:
    """Return True if two Shopify address dicts are meaningfully different."""
    if not addr1 or not addr2:
        return bool(addr2)
    compare_keys = ["address1", "address2", "city", "zip", "province_code", "country_code"]
    for key in compare_keys:
        if (addr1.get(key) or "").strip().lower() != (addr2.get(key) or "").strip().lower():
            return True
    return False


def _update_shopify_fields(customer_name: str, shopify_id: str, phone: str, email: str):
    """Update Shopify sync fields on an existing customer."""
    updates = {}
    if shopify_id:
        updates["shopify_customer_id"] = shopify_id
    if phone:
        updates["shopify_phone"] = phone
    if email:
        updates["shopify_email"] = email
    if updates:
        frappe.db.set_value("Customer", customer_name, updates)


def _clean_phone(phone: str) -> str:
    """Normalize phone — strip country code prefix, spaces, dashes."""
    if not phone:
        return ""
    import re
    cleaned = re.sub(r"[\s\-\(\)\.]+", "", phone)
    # Remove leading +91 or 91 for Indian numbers, keep 10-digit
    if cleaned.startswith("+91") and len(cleaned) == 13:
        cleaned = cleaned[3:]
    elif cleaned.startswith("91") and len(cleaned) == 12:
        cleaned = cleaned[2:]
    return cleaned


def _create_contact(customer_name: str, contact_name: str, phone: str, email: str):
    """Create an ERPNext Contact linked to the customer."""
    try:
        if not contact_name:
            return

        # Split on first space for first/last
        parts = contact_name.strip().split(" ", 1)
        first = parts[0]
        last  = parts[1] if len(parts) > 1 else ""

        # Check if ERPNext automatically created a contact when the Customer was inserted
        # (ERPNext creates a contact if mobile_no and email_id are set on the Customer)
        existing_contacts = frappe.get_all(
            "Dynamic Link",
            filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Contact"},
            pluck="parent"
        )
        if existing_contacts:
            # Contact auto-created by ERPNext using Customer Name. Update its name to the real person.
            contact_doc = frappe.get_doc("Contact", existing_contacts[0])
            contact_doc.first_name = first
            contact_doc.last_name = last
            contact_doc.save(ignore_permissions=True)
            frappe.db.set_value("Customer", customer_name, "customer_primary_contact", contact_doc.name)
            return

        contact = frappe.get_doc({
            "doctype":    "Contact",
            "first_name": first,
            "last_name":  last,
            "links": [{"link_doctype": "Customer", "link_name": customer_name}],
        })
        if phone:
            contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1})
        if email:
            contact.append("email_ids", {"email_id": email, "is_primary": 1})

        contact.flags.ignore_permissions = True
        contact.insert()

        # Set as the primary contact shown on the Customer form
        frappe.db.set_value("Customer", customer_name, "customer_primary_contact", contact.name)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Shopify: Contact Creation Failed")


def _normalise_gst_state(province: str) -> str:
    """
    Map Shopify's province name to the exact string India Compliance stores in
    the `gst_state` field.  IC derives intra-state vs inter-state by comparing
    the customer address gst_state with the company's GSTIN state prefix, so a
    mismatch silently causes the wrong tax template (IGST vs CGST+SGST).

    Most state names are identical in Shopify and IC; only the edge cases where
    the Shopify name diverges from the IC-registered canonical name are listed.
    """
    _PROVINCE_TO_GST_STATE = {
        # Shopify sends the old undivided state name for the UT merger
        "dadra and nagar haveli and daman and diu": "Dadra and Nagar Haveli",
        "dadra & nagar haveli and daman & diu":     "Dadra and Nagar Haveli",
        # Shopify may send abbreviated conjunctions
        "jammu & kashmir":                          "Jammu and Kashmir",
        "andaman & nicobar islands":                "Andaman and Nicobar Islands",
        "andaman and nicobar":                      "Andaman and Nicobar Islands",
        "d & nh":                                   "Dadra and Nagar Haveli",
        "d&nh":                                     "Dadra and Nagar Haveli",
    }
    if not province:
        return ""
    return _PROVINCE_TO_GST_STATE.get(province.strip().lower(), province.strip())


def _get_default_customer(settings) -> str:
    """Return or create a generic walk-in customer for orders with no customer data."""
    default_name = "Shopify Walk-in Customer"
    if not frappe.db.exists("Customer", default_name):
        customer = frappe.get_doc({
            "doctype":        "Customer",
            "customer_name":  default_name,
            "customer_type":  "Individual",
            "customer_group": settings.customer_group or "All Customer Groups",
            "territory":      settings.territory or "All Territories",
        })
        customer.flags.ignore_permissions = True
        customer.insert()
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — runs in background job; contact must persist before SO creation
    return default_name
