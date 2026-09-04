"""
add_refund_writeback_fields.py

Creates the Shopify refund write-back custom fields on installs that already
exist:

    Refund Request  shopify_writeback_status / _refund_gid / _refund_gateway /
                    _writeback_at / _writeback_error

after_install only runs on a fresh install, so without this patch `bench
migrate` would leave the fields missing and every write-back attempt would
refuse with "Refund Request is missing the Shopify write-back fields".

Refund Request belongs to payment_portals.  The creator returns without doing
anything when that DocType is absent, so this patch is a no-op on a site that
does not have that app — which is the point: shopify_integration must stay
installable without it.

Note on the field count: frappe.get_meta counts custom fields, so Refund
Request's field count changes once these install.  docs/HANDOFF.md in
payment_portals uses "field count 50" as a live deploy marker; after this ships
it reads 57, not 50.  Update that marker rather than reading it as a broken
deploy.

Idempotent: create_or_update_custom_field() creates what is absent and corrects
what is present, without touching stored values.
"""

import frappe

from shopify_integration.install import create_refund_request_writeback_custom_fields


def execute():
    if not frappe.db.exists("DocType", "Refund Request"):
        return  # payment_portals not installed on this site — nothing to patch

    create_refund_request_writeback_custom_fields()
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — patch runs outside request lifecycle
