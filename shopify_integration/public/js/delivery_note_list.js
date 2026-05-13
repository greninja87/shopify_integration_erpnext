frappe.listview_settings['Delivery Note'] = frappe.listview_settings['Delivery Note'] || {};

// Cached once per page load:
//   null  = not yet loaded (first render uses ERPNext default)
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
                // Refresh so rows re-render with the correct indicator.
                // Only needed when SI is active — if false, ERPNext defaults
                // are already showing correctly on the first render.
                if (was_unset && _shopify_si_active) {
                    listview.refresh();
                }
            }
        });
    },

    // get_indicator runs for every rendered row.
    // Non-Shopify DNs: return nothing → ERPNext default indicator.
    // Shopify DNs with SI disabled: return nothing → ERPNext default indicator.
    // Shopify DNs with SI enabled: show billing-aware status so users can
    // distinguish "needs an invoice" from "invoice already created".
    get_indicator: function(doc) {
        if (!doc.shopify_order_id || !_shopify_si_active) return;

        if (flt(doc.per_billed || 0) >= 100) {
            return [__("Completed"), "green", "per_billed,=,100"];
        }
        return [__("To Bill"), "orange", "per_billed,<,100"];
    }
});
