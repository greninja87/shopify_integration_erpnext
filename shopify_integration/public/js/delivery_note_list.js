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

// Same three-state cache for fulfillment, so the bulk action is only added
// when at least one store actually has fulfillment turned on.
let _shopify_fulfillment_active = null;

Object.assign(frappe.listview_settings['Delivery Note'], {
    // status and is_return are needed to detect Return / Return Issued / Closed
    add_fields: [
        "shopify_order_id", "per_billed", "status", "is_return",
        "custom_shopify_fulfillment_id", "custom_shopify_fulfillment_status",
    ],

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

        // Bulk "Fulfil in Shopify" action, added only when some store has
        // fulfillment enabled — otherwise the menu stays exactly as ERPNext
        // ships it.
        frappe.call({
            method: 'shopify_integration.utils.fulfillment.is_fulfillment_enabled',
            callback: function(r) {
                _shopify_fulfillment_active = !!(r.message);
                if (_shopify_fulfillment_active) {
                    shopify_add_bulk_fulfil_action(listview);
                }
            }
        });
    },

    get_indicator: function(doc) {
        // ── Non-Shopify DN or SI not enabled ──────────────────────────────────
        // Delegate entirely to ERPNext's original indicator — zero interference.
        if (!doc.shopify_order_id || !_shopify_si_active) {
            return _erpnext_dn_indicator ? _erpnext_dn_indicator(doc) : undefined;
        }

        // ── Shopify DN with SI enabled ─────────────────────────────────────────
        // For special lifecycle statuses delegate to ERPNext so the correct
        // label and colour is shown (Return → gray, Return Issued → grey,
        // Closed → green). These are not billing states — "Shopify" label
        // would be misleading here.
        //
        // Return DNs (is_return = 1) never go through the auto-SI billing flow —
        // they are handled as Credit Notes via the refunds/create webhook. So a
        // return DN must always show its native ERPNext status (To Bill,
        // Completed, Return, etc.), never the billing-aware "Shopify" label.
        // Note: a sales-return DN's status is usually "To Bill" or "Completed",
        // not literally "Return", so we must guard on is_return alone here.
        if (cint(doc.is_return) === 1) {
            return _erpnext_dn_indicator ? _erpnext_dn_indicator(doc) : undefined;
        }
        if (doc.status === 'Closed' || doc.status === 'Return Issued') {
            return _erpnext_dn_indicator ? _erpnext_dn_indicator(doc) : undefined;
        }

        // ── Normal Shopify DN: billing-aware "Shopify" indicator ───────────────
        // Orange = not yet billed  (needs invoice)
        // Yellow = partially billed
        // Green  = fully billed / completed
        const billed = flt(doc.per_billed || 0);
        if (billed >= 100) {
            return [__('Shopify'), 'green',  'shopify_order_id,is,set'];
        }
        if (billed > 0) {
            return [__('Shopify'), 'yellow', 'shopify_order_id,is,set'];
        }
        return     [__('Shopify'), 'orange', 'shopify_order_id,is,set'];
    }
});


// ── Bulk "Fulfil in Shopify" ──────────────────────────────────────────────────

function shopify_add_bulk_fulfil_action(listview) {
    listview.page.add_actions_menu_item(__('Fulfil in Shopify'), function() {
        const selected = listview.get_checked_items();
        if (!selected.length) {
            frappe.msgprint(__('Select one or more Delivery Notes first.'));
            return;
        }

        // Filter client-side to what can plausibly be fulfilled, and tell the
        // user exactly what is being skipped rather than silently dropping it.
        // The server re-checks every one of these — this is courtesy, not trust.
        const eligible = [];
        const skipped = { draft: 0, returns: 0, already: 0, not_shopify: 0 };

        selected.forEach(function(doc) {
            if (!doc.shopify_order_id)                  { skipped.not_shopify++; return; }
            if (cint(doc.is_return))                    { skipped.returns++;     return; }
            if (cint(doc.docstatus) !== 1)              { skipped.draft++;       return; }
            if (doc.custom_shopify_fulfillment_id)      { skipped.already++;     return; }
            eligible.push(doc.name);
        });

        const notes = [];
        if (skipped.already)      notes.push(__('{0} already fulfilled', [skipped.already]));
        if (skipped.draft)        notes.push(__('{0} not submitted', [skipped.draft]));
        if (skipped.returns)      notes.push(__('{0} return note(s)', [skipped.returns]));
        if (skipped.not_shopify)  notes.push(__('{0} not from Shopify', [skipped.not_shopify]));

        if (!eligible.length) {
            frappe.msgprint({
                title: __('Nothing to Fulfil'),
                message: notes.length
                    ? __('Skipped: {0}.', [notes.join(', ')])
                    : __('None of the selected Delivery Notes can be fulfilled.'),
                indicator: 'orange'
            });
            return;
        }

        let body = '<p>' + __('Mark {0} order(s) as fulfilled in Shopify?', [eligible.length]) + '</p>';
        if (notes.length) {
            body += '<p class="text-muted">' + __('Skipped: {0}.', [notes.join(', ')]) + '</p>';
        }
        body += '<p class="text-muted">'
             + __("Runs in the background, paced to Shopify's rate limit. Customer shipping emails follow each store's own setting.")
             + '</p>';

        frappe.confirm(body, function() {
            frappe.xcall(
                'shopify_integration.utils.fulfillment.fulfil_bulk',
                { dn_names: eligible }
            ).then(function(result) {
                frappe.show_alert({
                    message: (result && result.message) || __('Queued'),
                    indicator: 'blue'
                });
                listview.clear_checked_items();
            });
        });
    }, true);
}
