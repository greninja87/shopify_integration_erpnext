"""
add_payment_entry_gateway_fields.py

Creates the Payment Entry gateway-reference custom fields on installs that
already exist — after_install only runs on a fresh install, so without this
patch `bench migrate` would leave custom_gateway_reference missing and every
order sync would log "Gateway Reference Field Missing".

Idempotent: create_or_update_custom_field() creates what is absent and corrects
what is present, without touching stored values.
"""

import frappe

from shopify_integration.install import create_payment_entry_custom_fields


def execute():
    if not frappe.db.exists("DocType", "Payment Entry"):
        return  # ERPNext not installed on this site — nothing to patch

    create_payment_entry_custom_fields()
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — patch runs outside request lifecycle
