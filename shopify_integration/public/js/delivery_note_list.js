frappe.listview_settings['Delivery Note'] = frappe.listview_settings['Delivery Note'] || {};

// Capture ERPNext's built-in get_indicator BEFORE we override it so we can
// delegate back to it for non-Shopify DNs and special statuses (Return,
// Return Issued, Closed) without changing ERPNext's default behaviour at all.
const _erpnext_dn_indicator = frappe.listview_settings['Delivery Note'].get_indicator;

// Cached once per page load:
//   null  = not yet fetched (first render falls back to ERPNext default)
//   false = no Shopify store has enable_sales_invoice = 1
//   true  = at least one store has SI enabled → show Shopify indicators
let _shopify_si_active = null;

Object.assign(frappe.listview_settings['Delivery Note'], {
    add_fields: ["shopify_order_id", "shopify_fulfillment_status", "per_billed", "status", "is_return"],

    onload: function(listview) {
        // Single server call — checks if any Shopify store has SI enabled.
        // Cached so repeated list refreshes don't re-query.
        frappe.call({
            method: 'shopify_integration.utils.sales_invoice.is_sales_invoice_enabled',
            callback: function(r) {
                const was_unset = (_shopify_si_active === null);
                _shopify_si_active = !!(r.message);
                // Refresh only when SI is active so Shopify indicators render.
                // When false, ERPNext defaults already show correctly.
                if (was_unset && _shopify_si_active) {
                    listview.refresh();
                }
            }
        });
    },

    get_indicator: function(doc) {
        if (!doc.shopify_order_id) {
            return _erpnext_dn_indicator ? _erpnext_dn_indicator(doc) : undefined;
        }

        const f_status = doc.shopify_fulfillment_status || '';

        // If it failed fulfillment sync, always show red to grab attention
        if (f_status === 'Failed') {
            return [__('Sync Failed'), 'red',  'shopify_fulfillment_status,=,Failed'];
        }

        // ── Shopify DN with SI enabled ─────────────────────────────────────────
        if (_shopify_si_active) {
            // For special lifecycle statuses delegate to ERPNext
            if (cint(doc.is_return) === 1 && doc.status === 'Return') {
                return _erpnext_dn_indicator ? _erpnext_dn_indicator(doc) : undefined;
            }
            if (doc.status === 'Closed' || doc.status === 'Return Issued') {
                return _erpnext_dn_indicator ? _erpnext_dn_indicator(doc) : undefined;
            }

            // ── Normal Shopify DN: billing-aware "Shopify" indicator ───────────────
            const billed = flt(doc.per_billed || 0);
            if (billed >= 100) {
                return [__('Shopify'), 'green',  'shopify_order_id,is,set'];
            }
            if (billed > 0) {
                return [__('Shopify'), 'yellow', 'shopify_order_id,is,set'];
            }
            return [__('Shopify'), 'orange', 'shopify_order_id,is,set'];
        }

        // ── SI not enabled: use fulfillment status ──────────────────────────────
        if (f_status === 'Fulfilled') {
            return [__('Fulfilled'), 'blue',   'shopify_fulfillment_status,=,Fulfilled'];
        }
        if (f_status === 'Partially Fulfilled') {
            return [__('Partial'), 'orange',   'shopify_fulfillment_status,=,Partially Fulfilled'];
        }

        // Pending / Skipped / not yet pushed — generic "Shopify" green badge
        return [__('Shopify'), 'green', 'shopify_order_id,=,' + doc.shopify_order_id];
    }
});
