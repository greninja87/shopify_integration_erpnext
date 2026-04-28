import frappe
from frappe.model.document import Document


class ShopifySettings(Document):
    def validate(self):
        # Normalize shop domain — strip protocol and trailing slash
        if self.shop_domain:
            self.shop_domain = (
                self.shop_domain
                .replace("https://", "")
                .replace("http://", "")
                .rstrip("/")
                .lower()
            )

        if self.enable_sync and not self.webhook_secret:
            frappe.msgprint(
                "Warning: Webhook secret is empty. It is strongly recommended to set a "
                "webhook secret to verify incoming Shopify webhooks.",
                indicator="orange"
            )

        # Payment Entry config sanity checks — block save if any selected
        # Bank / Cash account is a group account or wrong type.
        if self.get("enable_payment_entry"):
            self._validate_payment_accounts()

        # Gateway mapping rows: each row must have at least one matching key
        self._validate_gateway_mapping_rows()

        # Field mapping: block sensitive ERPNext fields from being overwritten
        self._validate_field_mappings()

    def _validate_payment_accounts(self):
        """Refuse to save if any configured Bank / Cash account is:
            - a group account (is_group = 1)
            - disabled
            - not of account_type Bank or Cash
        """
        accounts_to_check = []
        if self.get("default_bank_account"):
            accounts_to_check.append(("Default Bank / Cash Account", self.default_bank_account))

        for row in (self.get("payment_gateway_mapping") or []):
            if row.get("bank_account"):
                label = (
                    f"Gateway Mapping row #{row.idx} "
                    f"({row.get('tag_contains') or row.get('shopify_gateway') or 'unnamed'})"
                )
                accounts_to_check.append((label, row.bank_account))

        for label, acc_name in accounts_to_check:
            acc = frappe.db.get_value(
                "Account",
                acc_name,
                ["is_group", "account_type", "disabled"],
                as_dict=True,
            )
            if not acc:
                frappe.throw(f"{label}: Account '{acc_name}' does not exist.")
            if acc.is_group:
                frappe.throw(
                    f"{label}: '{acc_name}' is a <b>group account</b>. "
                    f"Pick a leaf Bank or Cash account instead — group accounts "
                    f"cannot receive Payment Entries."
                )
            if acc.disabled:
                frappe.throw(f"{label}: '{acc_name}' is disabled.")
            if (acc.account_type or "") not in ("Bank", "Cash"):
                frappe.throw(
                    f"{label}: '{acc_name}' has account_type "
                    f"'{acc.account_type or 'blank'}' — must be Bank or Cash."
                )


    def _validate_gateway_mapping_rows(self):
        """Each gateway mapping row must have either shopify_gateway or tag_contains
        so it can actually match an order.  A row with neither field is a no-op and
        is almost certainly a data entry mistake."""
        for row in (self.get("payment_gateway_mapping") or []):
            if not row.get("shopify_gateway") and not row.get("tag_contains"):
                frappe.throw(
                    f"Payment Gateway Mapping row #{row.idx}: "
                    f"set either <b>Shopify Gateway</b> or <b>Tag Contains</b> — "
                    f"a row with neither value will never match any order.",
                    title="Gateway Mapping Incomplete"
                )

    def _validate_field_mappings(self):
        """
        Validate field mappings:
          - Hard-block system fields that control document identity / state.
          - Warn (but allow) fields that the integration already sets internally,
            so the user knows they are intentionally overriding automation logic.

        Any other standard or custom ERPNext field is permitted.
        """
        # These fields are managed by Frappe/ERPNext internals.  Writing to them
        # from external data would corrupt documents or bypass security checks.
        _SYSTEM = frozenset({
            "name", "owner", "creation", "modified", "modified_by",
            "docstatus", "parent", "parenttype", "parentfield", "idx",
            "workflow_state", "naming_series",
        })

        # These fields are already set by this integration.  Mapping to them is
        # allowed but the user should know they will override the automatic value.
        _INTEGRATION_CONTROLLED = frozenset({
            "customer", "company", "shopify_order_id", "shopify_store",
            "po_no", "transaction_date", "delivery_date",
            "payment_terms_template", "set_warehouse",
            "selling_price_list", "currency",
        })

        warned = False
        for mapping in (self.get("field_mapping") or []):
            field = (mapping.get("erpnext_field") or "").strip()
            field_lower = field.lower()

            if field_lower in _SYSTEM:
                frappe.throw(
                    f"Field Mapping row #{mapping.idx}: "
                    f"<b>{field}</b> is a system-controlled field and cannot be "
                    f"overwritten by Shopify data. "
                    f"System fields that control document identity, ownership, or "
                    f"state are blocked: <code>"
                    + "</code>, <code>".join(sorted(_SYSTEM))
                    + "</code>.",
                    title="System Field — Cannot Map"
                )

            if field_lower in _INTEGRATION_CONTROLLED and not warned:
                frappe.msgprint(
                    f"Field Mapping row #{mapping.idx}: "
                    f"<b>{field}</b> is already set automatically by the Shopify "
                    f"integration. Your mapping will overwrite the auto-generated "
                    f"value — make sure this is intentional.",
                    indicator="orange",
                    title="Overriding Integration-Controlled Field",
                    alert=True,
                )
                warned = True  # show at most once per save to avoid spam


def get_settings_for_store(shop_domain: str):
    """
    Look up the Shopify Settings record for a given shop domain.
    Called from api.py to route webhooks to the correct store config.
    """
    if not shop_domain:
        return None

    normalized = shop_domain.lower().strip()

    name = frappe.db.get_value(
        "Shopify Settings",
        {"shop_domain": normalized, "enable_sync": 1},
        "name"
    )

    if not name:
        frappe.log_error(
            f"No active Shopify Settings found for domain: {normalized}",
            "Shopify: Unknown Store"
        )
        return None

    return frappe.get_doc("Shopify Settings", name)


@frappe.whitelist()
def get_naming_series(doctype: str) -> str:
    """
    Return the naming series options for a given DocType.
    Called by the Shopify Settings client script to populate Select fields.

    :param doctype: e.g. 'Sales Order' or 'Customer'
    :return: newline-separated series options string
    """
    try:
        meta = frappe.get_meta(doctype)
        field = meta.get_field("naming_series")
        if field and field.options:
            return field.options  # Already newline-separated
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Shopify: Could not fetch naming series for {doctype}")
    return ""
