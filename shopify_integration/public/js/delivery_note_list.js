frappe.listview_settings['Delivery Note'] = frappe.listview_settings['Delivery Note'] || {};

Object.assign(frappe.listview_settings['Delivery Note'], {
    add_fields: ["shopify_order_id"],

    // get_indicator runs during row render — reliable across all Frappe versions.
    // For non-Shopify DNs (shopify_order_id absent) we return nothing so Frappe
    // falls back to its default status indicator (To Deliver, Completed, etc.).
    get_indicator: function(doc) {
        if (doc.shopify_order_id) {
            return [__("Shopify"), "green", "shopify_order_id,=," + doc.shopify_order_id];
        }
    }
});
