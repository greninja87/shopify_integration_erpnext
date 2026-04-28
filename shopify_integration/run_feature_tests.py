import frappe
from shopify_integration.shopify_integration.doctype.shopify_log.shopify_log import retry_order
from shopify_integration.shopify_integration.cleanup_and_retry import cleanup_all

def run_feature_tests():
    print("=== Starting Feature Combination Tests ===")
    
    # Get the first active Shopify Settings
    settings_name = frappe.db.get_value("Shopify Settings", {"enable_sync": 1}, "name")
    if not settings_name:
        print("No active Shopify Settings found. Please configure one first.")
        return
    
    settings = frappe.get_doc("Shopify Settings", settings_name)
    
    # Get all logs that were successfully processed before (so we know they have valid payloads)
    logs = frappe.get_all("Shopify Log", filters={"shopify_order_id": ["!=", ""]}, limit=5, pluck="name")
    if not logs:
        print("No Shopify Logs found to test with.")
        return

    test_cases = [
        {
            "name": "Case 1: PE Enabled, SI Enabled (After PE)",
            "enable_payment_entry": 1,
            "enable_sales_invoice": 1,
            "sales_invoice_trigger": "After Payment Entry",
            "auto_submit_payment_entry": 1,
            "auto_submit_sales_invoice": 1
        },
        {
            "name": "Case 2: PE Disabled, SI Enabled (After PE)",
            "enable_payment_entry": 0,
            "enable_sales_invoice": 1,
            "sales_invoice_trigger": "After Payment Entry",
            "auto_submit_payment_entry": 0,
            "auto_submit_sales_invoice": 1
        },
        {
            "name": "Case 3: PE Enabled, SI Enabled (After Delivery Note)",
            "enable_payment_entry": 1,
            "enable_sales_invoice": 1,
            "sales_invoice_trigger": "After Delivery Note",
            "auto_submit_payment_entry": 1,
            "auto_submit_sales_invoice": 1
        },
        {
            "name": "Case 4: PE Enabled, SI Disabled",
            "enable_payment_entry": 1,
            "enable_sales_invoice": 0,
            "sales_invoice_trigger": "After Payment Entry",
            "auto_submit_payment_entry": 1,
            "auto_submit_sales_invoice": 0
        }
    ]

    for i, test in enumerate(test_cases):
        if i >= len(logs): break
        log_id = logs[i]
        
        print(f"\n--- Running {test['name']} using Log {log_id} ---")
        
        # 1. Update Settings
        frappe.db.set_value("Shopify Settings", settings_name, {
            "enable_payment_entry": test["enable_payment_entry"],
            "enable_sales_invoice": test["enable_sales_invoice"],
            "sales_invoice_trigger": test["sales_invoice_trigger"],
            "auto_submit_payment_entry": test["auto_submit_payment_entry"],
            "auto_submit_sales_invoice": test["auto_submit_sales_invoice"]
        })
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — test utility; settings must persist before retry_order call
        
        # 2. Reset Log
        frappe.db.set_value("Shopify Log", log_id, {
            "status": "Skipped",
            "erpnext_sales_order": "",
            "error_message": f"Testing {test['name']}"
        })
        frappe.db.commit()  # nosemgrep: frappe-manual-commit — test utility; log reset must persist before retry
        
        # 3. Process
        try:
            # We call the background processing logic here
            # But since we want to see results immediately, we call retry_order directly
            res = retry_order(log_id)
            so_name = res.get("sales_order")
            print(f"Result: {res}")
            
            if so_name:
                # Verify PE
                pe_count = frappe.db.count("Payment Entry Reference", {"reference_name": so_name})
                print(f"Payment Entries linked to SO: {pe_count}")
                
                # Verify SI
                si_count = frappe.db.count("Sales Invoice Item", {"sales_order": so_name})
                print(f"Sales Invoices linked to SO: {si_count}")
                
                # Check for duplicate contacts
                customer = frappe.db.get_value("Sales Order", so_name, "customer")
                contacts = frappe.get_all("Dynamic Link", filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Contact"})
                print(f"Contacts for Customer {customer}: {len(contacts)}")
                
        except Exception as e:
            print(f"Error during test: {str(e)}")
            print(frappe.get_traceback())

    print("\n=== Feature Tests Complete ===")

if __name__ == "__main__":
    run_feature_tests()
