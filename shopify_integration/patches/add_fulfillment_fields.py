"""
add_fulfillment_fields.py

Creates the Shopify fulfillment custom fields on installs that already exist:

    Delivery Note   custom_shopify_fulfillment_status / _id / _at / _error
    Sales Order Item custom_shopify_line_item_id

after_install only runs on a fresh install, so without this patch `bench
migrate` would leave the fields missing and every fulfillment attempt would
refuse with "Delivery Note is missing the Shopify fulfillment fields".

Idempotent: create_or_update_custom_field() creates what is absent and corrects
what is present, without touching stored values.

Note on historical orders: custom_shopify_line_item_id is only populated for
orders synced after this patch.  Older Sales Orders keep it blank and fulfil via
SKU matching, which is correct whenever a SKU appears once on the order.  There
is deliberately no backfill — Shopify line item ids would have to be re-fetched
per order, and SKU matching already covers the realistic cases.
"""

import frappe

from shopify_integration.install import (
    create_delivery_note_fulfillment_custom_fields,
    create_sales_order_item_custom_fields,
)


def execute():
    if not frappe.db.exists("DocType", "Delivery Note"):
        return  # ERPNext not installed on this site — nothing to patch

    create_sales_order_item_custom_fields()
    create_delivery_note_fulfillment_custom_fields()
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — patch runs outside request lifecycle
