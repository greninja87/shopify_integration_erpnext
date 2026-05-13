frappe.listview_settings['Delivery Note'] = frappe.listview_settings['Delivery Note'] || {};

// Capture ERPNext's built-in get_indicator before we override it so we can
// delegate non-Shopify rows back to it (preserves "To Bill" / "Completed"
// for manually created Delivery Notes).
const _erpnext_dn_indicator = frappe.listview_settings['Delivery Note'].get_indicator;

// Cached once per page load:
//   null  = not yet loaded (first render delegates to ERPNext default)
//   false = no Shopify Settings with enable_sales_invoice = 1
//   true  = at least one store has SI enabled
let _shopify_si_active = null;

Object.assign(frappe.listview_settings['Delivery Note'], {
    add_fields: ["shopify_order_id", "per_billed"],

    onload: function(listview) {
        // Single call to check if any Shopify store has Sales Invoice enabled.
        // Result is cached so subsequent list refreshes don't re-query.
        frappe.call({
            method: 'shopify_integration.utils.sales_invoice.is_sales_invoice_enabled',
            callback: function(r) {
                const was_unset = (_shopify_si_active === null);
                _shopify_si_active = !!(r.message);
                if (was_unset && _shopify_si_active) {
                    listview.refresh();
                }
            }
        });
    },

    get_indicator: function(doc) {
        // Non-Shopify DN or SI not enabled → delegate to ERPNext's built-in
        // indicator so "To Bill" / "Completed" still works for manual DNs.
        if (!doc.shopify_order_id || !_shopify_si_active) {
            return _erpnext_dn_indicator ? _erpnext_dn_indicator(doc) : undefined;
        }

        // Shopify DN with SI enabled: show billing-aware status.
        if (flt(doc.per_billed || 0) >= 100) {
            return [__("Completed"), "green", "per_billed,=,100"];
        }
        return [__("To Bill"), "orange", "per_billed,<,100"];
    }
});
