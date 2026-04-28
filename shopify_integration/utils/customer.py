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
  - Billing address always created
  - Shipping address created separately if different from billing
  - Billing address marked is_primary_address = 1 on customer
  - Shipping address marked is_shipping_address = 1 on customer
"""

import frappe


def get_or_create_customer(
    shopify_customer: dict,
    billing_address: dict,
    shipping_address: dict,
    settings,
    gstin: str = None,
    gst_legal_name: str = None,
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
):
    """Create a new ERPNext Customer from Shopify data."""
    customer_doc = {
        "doctype":        "Customer",
        "customer_name":  full_name,
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

    # Create addresses
    billing_addr_name  = None
    shipping_addr_name = None

    if billing_address:
        billing_addr_name = _create_address(
            customer_name=customer.name,
            address=billing_address,
            address_type="Billing",
            is_primary=True,
            is_shipping=False
        )

    # Create shipping address only if different from billing
    if shipping_address and _addresses_are_different(billing_address, shipping_address):
        shipping_addr_name = _create_address(
            customer_name=customer.name,
            address=shipping_address,
            address_type="Shipping",
            is_primary=False,
            is_shipping=True
        )
    elif billing_address and not shipping_addr_name:
        # Mark the billing address also as shipping address
        if billing_addr_name:
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
    Create an ERPNext Address linked to the customer.
    Returns the address name or "" on failure.
    """
    try:
        # Build unique title to avoid duplicates
        suffix = "Billing" if address_type == "Billing" else "Shipping"
        addr_title = f"{customer_name}-{suffix}"

        # Check if this address already exists to avoid duplicate insert
        if frappe.db.exists("Address", {"address_title": addr_title}):
            return frappe.db.get_value("Address", {"address_title": addr_title}, "name")

        # Shopify sends province = full state name (e.g. "Gujarat"),
        # province_code = abbreviated code (e.g. "GJ").
        # India Compliance reads `gst_state` (full name) to determine
        # intra-state (CGST+SGST) vs inter-state (IGST) for B2C orders.
        state_name = address.get("province", "")

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


def _addresses_are_different(addr1: dict, addr2: dict) -> bool:
    """Return True if two Shopify address dicts are meaningfully different."""
    if not addr1 or not addr2:
        return bool(addr2)
    compare_keys = ["address1", "address2", "city", "zip", "province_code", "country_code"]
    for key in compare_keys:
        if (addr1.get(key) or "").strip() != (addr2.get(key) or "").strip():
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
