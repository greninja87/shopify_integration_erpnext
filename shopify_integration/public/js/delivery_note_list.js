frappe.listview_settings['Delivery Note'] = frappe.listview_settings['Delivery Note'] || {};

Object.assign(frappe.listview_settings['Delivery Note'], {
    add_fields: ["shopify_order_id"],

    refresh: function (listview) {
        // Inject small green dots BEFORE the DN number in the ID column
        setTimeout(function () {
            (listview.data || []).forEach(function (d) {
                if (!d.shopify_order_id) return;

                // Target the ID column: div.list-row-col.name > span > a[data-filter]
                let $idLink = listview.$result.find(
                    '.list-row-col.name a[data-filter="name,=,' + d.name + '"]'
                ).first();

                if (!$idLink.length) return;

                // Check if dot is already injected
                let $parent = $idLink.parent();
                if ($parent.find('.shopify-indicator-dot').length) return;

                // Insert dot BEFORE the link inside the ID column
                $idLink.before(
                    '<span class="shopify-indicator-dot" ' +
                    'style="display:inline-block;width:7px;height:7px;' +
                    'border-radius:50%;background:#10b981;margin-right:5px;' +
                    'vertical-align:middle;box-shadow:0 0 2px rgba(0,0,0,0.15);' +
                    'flex-shrink:0;" ' +
                    'title="Shopify Order"></span>'
                );
            });
        }, 200);
    }
});
