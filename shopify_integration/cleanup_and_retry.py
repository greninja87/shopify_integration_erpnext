import frappe

def cleanup_all():
    print("--- Starting Brute Force Cleanup (All Records) ---")
    
    # Order matters: Delete dependencies first (Payments -> Invoices/Deliveries -> Orders -> Customers)
    doctypes_to_clear = [
        'Payment Entry',
        'Sales Invoice',
        'Delivery Note',
        'Sales Order',
        'Customer'
    ]

    for doctype in doctypes_to_clear:
        print(f"Cleaning up all {doctype}s...")
        records = frappe.get_all(doctype, pluck='name')
        for docname in records:
            if frappe.db.exists(doctype, docname):
                try:
                    doc = frappe.get_doc(doctype, docname)
                    # If document is submitted, cancel it first
                    # This automatically reverses and handles GL Entries
                    if getattr(doc, 'docstatus', 0) == 1:
                        doc.cancel()
                    frappe.delete_doc(doctype, docname, force=1, ignore_permissions=True)
                    print(f"Deleted {doctype}: {docname}")
                except Exception as e:
                    print(f"Failed to delete {doctype} {docname}: {str(e)}")

    print("Cleaning up all Addresses and Contacts...")
    for dt in ['Address', 'Contact']:
        records = frappe.get_all(dt, pluck='name')
        for r in records:
            try:
                frappe.delete_doc(dt, r, force=1, ignore_permissions=True)
                print(f"Deleted {dt}: {r}")
            except Exception:
                pass

    print("--- Resetting and Retrying Shopify Logs ---")
    logs = frappe.get_all('Shopify Log', filters={'status': ['in', ['Processed', 'Failed']]}, pluck='name')
    for log_id in logs:
        frappe.db.set_value('Shopify Log', log_id, {
            'erpnext_sales_order': '',
            'status': 'Skipped',
            'error_message': 'Reset for testing.'
        })
        print(f"Reset Shopify Log: {log_id}")

    frappe.db.commit()  # nosemgrep: frappe-manual-commit — dev utility script run via bench console
    print("--- Cleanup Complete, Starting Retries ---")

    try:
        from shopify_integration.shopify_integration.doctype.shopify_log.shopify_log import retry_order
        for log_id in logs:
            try:
                print(f"Retrying log {log_id}...")
                res = retry_order(log_id)
                print(f"Retry Successful for {log_id}: {res}")
            except Exception as e:
                print(f"Retry Failed for {log_id}: {str(e)}")
    except Exception as e:
        print(f"Failed to load retry_order function: {e}")

if __name__ == "__main__":
    cleanup_all()
