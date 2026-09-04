// refund_request.js — Shopify refund write-back on the Refund Request form.
//
// Refund Request belongs to payment_portals.  This file only ever adds; it
// renders nothing at all when the refund is not a Shopify one, or when
// payment_portals is installed without the write-back fields migrated.
//
// The button is the ONLY trigger for the write-back.  There is deliberately no
// doc_events hook: a successful refundCreate pays the customer (the Cashfree-OCC
// app bridges it into a real Cashfree refund), and deciding to pay somebody
// belongs with a person or with whatever owns the refund's money path — not with
// a save handler in this app.  So the confirmation below is not a formality.

frappe.ui.form.on('Refund Request', {
    refresh: function(frm) {
        if (frm.doc.docstatus !== 1) return;

        frappe.xcall(
            'shopify_integration.utils.refund.get_refund_writeback_status',
            { refund_name: frm.doc.name }
        ).then(function(info) {
            if (!info || !info.is_shopify) return;
            shopify_refund_indicator(frm, info);
            shopify_refund_button(frm, info);
            shopify_refund_message(frm, info);
        }).catch(() => null);
    }
});


function shopify_refund_indicator(frm, info) {
    const colours = {
        'Done': 'green',
        'Pending': 'blue',
        'Failed': 'red',
        'Skipped': 'grey',
        // Not 'red'. Red reads as "it failed, try again", and trying again is
        // the one thing that must not happen here.
        'Unverified': 'orange'
    };

    if (info.status) {
        frm.dashboard.add_indicator(
            __('Shopify Refund: {0}', [__(info.status)]),
            colours[info.status] || 'grey'
        );
        return;
    }

    // Submitted Shopify refund, nothing sent yet.  Nothing will send it on its
    // own, so say that rather than leaving it looking queued.
    frm.dashboard.add_indicator(__('Shopify: not written back'), 'orange');
}


function shopify_refund_button(frm, info) {
    // A GID means Shopify has already refunded this; the server refuses anyway,
    // so don't offer a button that cannot work.
    if (info.refund_gid || !info.can_write_back) return;

    // Belt and braces. can_write_back is already false for an unconfirmed
    // attempt, but this state is the one where a stray retry pays the customer
    // twice, so it is refused here by name as well as by eligibility.
    if (info.status === 'Unverified') return;

    const label = (info.status === 'Failed')
        ? __('Retry Shopify Refund')
        : __('Refund in Shopify');

    frm.add_custom_button(label, function() {
        shopify_confirm_and_write_back(frm, info);
    }, __('Shopify'));
}


function shopify_refund_message(frm, info) {
    const messages = [];

    if (info.status === 'Unverified') {
        messages.push({
            text: '<b>' + __('Shopify Refund: outcome unknown') + '</b><br>'
                  + __('The refund request reached Shopify and the result could not be confirmed, so the customer may already have been paid. Do not retry it. Open the Shopify order, then record what you find.')
                  + '<br>' + frappe.utils.escape_html(info.error || ''),
            colour: 'orange'
        });
        frm.dashboard.clear_headline();
        frm.dashboard.set_headline_alert(
            '<div style="padding-right:40px;display:block;line-height:1.5;">'
            + messages[0].text + '</div>',
            'orange'
        );
        return;
    }

    if (info.refund_gid) {
        let done = '<b>' + __('Shopify Refund') + ':</b> '
            + __('Sent as {0}.', [frappe.utils.escape_html(info.refund_gid)]);
        if (info.gateway) {
            done += ' ' + __('Gateway: {0}.', [frappe.utils.escape_html(info.gateway)]);
        }
        messages.push({ text: done, colour: 'green' });
    }

    if (info.error) {
        messages.push({
            text: '<b>' + __('Shopify Refund') + ':</b> '
                  + frappe.utils.escape_html(info.error),
            colour: info.status === 'Failed' ? 'red' : 'orange'
        });
    }

    if (!messages.length) return;

    const rank = { green: 1, orange: 2, red: 3 };
    const colour = messages.reduce(
        (worst, m) => (rank[m.colour] > rank[worst] ? m.colour : worst),
        messages[0].colour
    );

    frm.dashboard.clear_headline();
    frm.dashboard.set_headline_alert(
        '<div style="padding-right:40px;display:block;line-height:1.5;">'
        + messages.map(m => m.text).join('<br>')
        + '</div>',
        colour
    );
}


function shopify_confirm_and_write_back(frm, info) {
    // This is a payout, not a record.  The dialog says so in those words,
    // because "write back to Shopify" sounds like bookkeeping and is not.
    // Refund Request has no currency field — its Currency fields are in company
    // currency, which is what format_currency falls back to.
    const amount = format_currency(info.amount);

    let warning = '<p>' + __('This asks Shopify to refund {0} on order {1}.',
        [amount, frappe.utils.escape_html(info.shopify_order_id)]) + '</p>'
        + '<p><b>' + __('This pays the customer.') + '</b> '
        + __('It is a payment instruction, not a record of one. Do not send it if the refund has already been paid by another route.')
        + '</p>';

    frappe.confirm(
        warning,
        function() {
            frappe.dom.freeze(__('Refunding in Shopify…'));
            frappe.xcall(
                'shopify_integration.utils.refund.writeback_now',
                { refund_name: frm.doc.name }
            ).then(function(result) {
                frappe.dom.unfreeze();
                if (result && result.ok) {
                    frappe.show_alert({
                        message: __('Refunded in Shopify'),
                        indicator: 'green'
                    });
                } else if (result && result.possibly_paid) {
                    // "Not sent" would be a lie here, and acting on it — trying
                    // again — is what pays the customer twice.
                    frappe.msgprint({
                        title: __('Refund May Have Been Sent'),
                        message: '<p><b>'
                            + __('Do not retry this refund.')
                            + '</b> '
                            + __('The request reached Shopify and the outcome could not be confirmed, so the customer may already have been paid. Check the Shopify order and record what you find.')
                            + '</p><p>'
                            + frappe.utils.escape_html(result.message || '')
                            + '</p>',
                        indicator: 'orange'
                    });
                } else {
                    frappe.msgprint({
                        title: __('Refund Not Sent'),
                        message: frappe.utils.escape_html(
                            (result && result.message) || __('Unknown error')
                        ),
                        indicator: 'red'
                    });
                }
                frm.reload_doc();
            }).catch(function() {
                frappe.dom.unfreeze();
            });
        }
    );
}
