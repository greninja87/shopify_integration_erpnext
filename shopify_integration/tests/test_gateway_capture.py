"""
test_gateway_capture.py — tests for the write path: idempotency, blank handling,
what gets written, and what must NOT be touched.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_gateway_capture -v

Transactions are injected via capture_gateway_reference(transactions=[...]), so
no HTTP happens here.  shopify_api's throttling and 429 retry are exercised
separately in test_shopify_api.py.
"""

import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.utils import gateway_reference as gr  # noqa: E402

PAYU_TXNID = "rkdkuLhOZPiHLp9XVygf0ASij"

SALE_TXN = {
    "id": 8811,
    "kind": "sale",
    "status": "success",
    "created_at": "2026-08-20T14:12:03+05:30",
    "gateway": "Cards, UPI, NB by PayU India",
    "authorization": PAYU_TXNID,
}


def _seed_pe(name="PE-2026-00042", reference_no="#6428", gateway_reference=""):
    frappe_stub.set_doc("Payment Entry", name, {
        "name": name,
        "reference_no": reference_no,
        "custom_gateway_reference": gateway_reference,
        "custom_gateway_name": "",
    })
    return name


class _OfflineCase(unittest.TestCase):
    """
    Base case that keeps the suite off the network.

    capture_gateway_reference() fetches the order when a higher-ranked source
    could live in its note_attributes, which is every backfill-shaped call here.
    These tests stub get_order_transactions but not get_order, so without this
    the real client runs: a rate-limit sleep and a DNS lookup per test, and a
    suite that fails differently depending on whether the machine is online.

    Returning {} is the honest default — an order with nothing in it.  A test
    that cares what the order holds overrides gr.get_order itself.
    """

    def setUp(self):
        frappe_stub.reset()
        self._real_get_order = gr.get_order
        gr.get_order = lambda settings, order_id: {}
        self.addCleanup(setattr, gr, "get_order", self._real_get_order)


class TestCaptureGatewayReference(_OfflineCase):
    def setUp(self):
        super().setUp()

    def test_writes_reference_and_gateway(self):
        pe = _seed_pe()
        result = gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(),
                                             transactions=[SALE_TXN])

        self.assertEqual(result, PAYU_TXNID)
        stored = frappe_stub.get_doc_values("Payment Entry", pe)
        self.assertEqual(stored["custom_gateway_reference"], PAYU_TXNID)
        self.assertEqual(stored["custom_gateway_name"], "Cards, UPI, NB by PayU India")

    def test_reference_is_25_chars(self):
        pe = _seed_pe()
        gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(),
                                     transactions=[SALE_TXN])
        self.assertEqual(len(frappe_stub.get_doc_values("Payment Entry", pe)["custom_gateway_reference"]), 25)

    def test_reference_no_is_never_touched(self):
        """The acceptance criterion: reference_no unchanged everywhere."""
        pe = _seed_pe(reference_no="#6428")
        gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(),
                                     transactions=[SALE_TXN])

        self.assertEqual(frappe_stub.get_doc_values("Payment Entry", pe)["reference_no"], "#6428")
        for _doctype, _name, values, _kwargs in frappe_stub.WRITES:
            self.assertNotIn("reference_no", values)
            self.assertNotIn("reference_date", values)

    def test_write_does_not_bump_modified(self):
        """update_modified=False keeps the PE free of document churn."""
        pe = _seed_pe()
        gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(),
                                     transactions=[SALE_TXN])

        self.assertEqual(len(frappe_stub.WRITES), 1)
        _doctype, _name, _values, kwargs = frappe_stub.WRITES[0]
        self.assertIs(kwargs.get("update_modified"), False)

    def test_only_the_two_gateway_fields_are_written(self):
        pe = _seed_pe()
        gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(),
                                     transactions=[SALE_TXN])

        _doctype, _name, values, _kwargs = frappe_stub.WRITES[0]
        self.assertEqual(set(values), {"custom_gateway_reference", "custom_gateway_name"})

    def test_idempotent_when_already_set(self):
        pe = _seed_pe(gateway_reference="already-there")
        result = gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(),
                                              transactions=[SALE_TXN])

        self.assertEqual(result, "already-there")
        self.assertEqual(frappe_stub.WRITES, [], "must not rewrite an existing reference")

    def test_rerun_is_a_no_op(self):
        pe = _seed_pe()
        settings = frappe_stub.FakeSettings()
        gr.capture_gateway_reference(pe, 6428, settings=settings, transactions=[SALE_TXN])
        gr.capture_gateway_reference(pe, 6428, settings=settings, transactions=[SALE_TXN])
        self.assertEqual(len(frappe_stub.WRITES), 1)

    def test_blank_reference_writes_nothing_and_logs(self):
        """Never write a placeholder."""
        pe = _seed_pe()
        txn = dict(SALE_TXN, authorization=None, receipt={})
        result = gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(),
                                              transactions=[txn])

        self.assertEqual(result, "")
        self.assertEqual(frappe_stub.WRITES, [])
        self.assertEqual(frappe_stub.get_doc_values("Payment Entry", pe)["custom_gateway_reference"], "")
        self.assertTrue(any("Gateway Reference Empty" in title for _msg, title in frappe_stub.ERRORS))

    def test_no_eligible_transaction_writes_nothing_and_logs(self):
        pe = _seed_pe()
        txns = [dict(SALE_TXN, kind="authorization"), dict(SALE_TXN, status="failure")]
        result = gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(),
                                              transactions=txns)

        self.assertEqual(result, "")
        self.assertEqual(frappe_stub.WRITES, [])
        self.assertTrue(any("Gateway Reference Not Found" in title for _msg, title in frappe_stub.ERRORS))

    def test_empty_transaction_list_writes_nothing(self):
        pe = _seed_pe()
        self.assertEqual(
            gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(), transactions=[]),
            "",
        )
        self.assertEqual(frappe_stub.WRITES, [])

    def test_missing_order_id_logs_and_returns_blank(self):
        pe = _seed_pe()
        result = gr.capture_gateway_reference(pe, "", settings=frappe_stub.FakeSettings(),
                                              transactions=[SALE_TXN])
        self.assertEqual(result, "")
        self.assertTrue(any("No Order ID" in title for _msg, title in frappe_stub.ERRORS))

    def test_missing_pe_name_returns_blank(self):
        self.assertEqual(gr.capture_gateway_reference("", 6428), "")

    def test_missing_custom_field_degrades_quietly(self):
        """An un-migrated site must log, not crash every order."""
        pe = _seed_pe()
        frappe_stub.META_FIELDS["Payment Entry"] = {"reference_no"}

        result = gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(),
                                              transactions=[SALE_TXN])
        self.assertEqual(result, "")
        self.assertEqual(frappe_stub.WRITES, [])
        self.assertTrue(any("Field Missing" in title for _msg, title in frappe_stub.ERRORS))

    def test_gateway_field_absent_still_writes_reference(self):
        pe = _seed_pe()
        frappe_stub.META_FIELDS["Payment Entry"] = {"custom_gateway_reference", "reference_no"}

        result = gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(),
                                              transactions=[SALE_TXN])
        self.assertEqual(result, PAYU_TXNID)
        _doctype, _name, values, _kwargs = frappe_stub.WRITES[0]
        self.assertEqual(set(values), {"custom_gateway_reference"})

    def test_no_token_configured_is_silent_when_fetching(self):
        """
        A store without an Admin API token has the feature switched off — no
        write, and no Error Log noise on every single order.
        """
        pe = _seed_pe()
        settings = frappe_stub.FakeSettings(token="")
        result = gr.capture_gateway_reference(pe, 6428, settings=settings)  # transactions=None → would fetch

        self.assertEqual(result, "")
        self.assertEqual(frappe_stub.WRITES, [])
        self.assertEqual(frappe_stub.ERRORS, [])

    def test_earliest_of_several_successful_transactions_wins(self):
        pe = _seed_pe()
        txns = [
            dict(SALE_TXN, id=2, created_at="2026-08-20T16:00:00+05:30", authorization="later-one"),
            dict(SALE_TXN, id=1, created_at="2026-08-20T14:00:00+05:30", authorization=PAYU_TXNID),
        ]
        self.assertEqual(
            gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings(), transactions=txns),
            PAYU_TXNID,
        )

    def test_api_error_is_swallowed(self):
        """Failure must not break order sync."""
        pe = _seed_pe()

        def boom(*a, **k):
            raise gr.ShopifyAPIError("500 from Shopify", 500)

        original = gr.get_order_transactions
        gr.get_order_transactions = boom
        try:
            result = gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings())
        finally:
            gr.get_order_transactions = original

        self.assertEqual(result, "")
        self.assertEqual(frappe_stub.WRITES, [])
        self.assertTrue(any("API Error" in title for _msg, title in frappe_stub.ERRORS))

    def test_unexpected_exception_is_swallowed(self):
        pe = _seed_pe()

        def boom(*a, **k):
            raise RuntimeError("something unforeseen")

        original = gr.get_order_transactions
        gr.get_order_transactions = boom
        try:
            result = gr.capture_gateway_reference(pe, 6428, settings=frappe_stub.FakeSettings())
        finally:
            gr.get_order_transactions = original

        self.assertEqual(result, "")
        self.assertTrue(any("Gateway Reference Failed" in title for _msg, title in frappe_stub.ERRORS))


class TestCaptureForOrder(_OfflineCase):
    def setUp(self):
        super().setUp()

    def test_reads_order_id_from_payload(self):
        pe = _seed_pe()
        original = gr.get_order_transactions
        gr.get_order_transactions = lambda settings, order_id: (
            [SALE_TXN] if str(order_id) == "6428" else []
        )
        try:
            result = gr.capture_for_order(pe, {"id": 6428, "name": "#6428"},
                                          frappe_stub.FakeSettings())
        finally:
            gr.get_order_transactions = original
        self.assertEqual(result, PAYU_TXNID)

    def test_blank_pe_name_short_circuits(self):
        self.assertEqual(gr.capture_for_order("", {"id": 6428}, frappe_stub.FakeSettings()), "")
        self.assertEqual(frappe_stub.WRITES, [])


class TestBackfill(_OfflineCase):
    def setUp(self):
        super().setUp()

    def _pending_rows(self, rows):
        frappe_stub.SQL_RESULTS["FROM `tabPayment Entry` pe"] = rows

    def test_dry_run_writes_nothing(self):
        _seed_pe("PE-0001")
        self._pending_rows([
            {"pe_name": "PE-0001", "shopify_order_id": "6428",
             "shopify_store": "notdrones.myshopify.com"},
        ])

        result = gr.backfill_gateway_references(limit=10, dry_run=1)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(frappe_stub.WRITES, [])
        self.assertEqual(result["entries"][0]["payment_entry"], "PE-0001")

    def test_aborts_when_field_missing(self):
        frappe_stub.META_FIELDS["Payment Entry"] = {"reference_no"}
        result = gr.backfill_gateway_references(limit=10)

        self.assertIn("aborted", result)
        self.assertEqual(result["scanned"], 0)
        self.assertEqual(frappe_stub.WRITES, [])

    def test_populates_pending_entries(self):
        _seed_pe("PE-0001")
        _seed_pe("PE-0002")
        self._pending_rows([
            {"pe_name": "PE-0001", "shopify_order_id": "6428", "shopify_store": "s.myshopify.com"},
            {"pe_name": "PE-0002", "shopify_order_id": "6429", "shopify_store": "s.myshopify.com"},
        ])
        frappe_stub.set_doc("Shopify Settings", "Test Store", {"shop_domain": "s.myshopify.com"})

        original_settings = gr._settings_for_domain
        original_fetch    = gr.get_order_transactions
        gr._settings_for_domain  = lambda domain: frappe_stub.FakeSettings(shop_domain=domain)
        gr.get_order_transactions = lambda settings, order_id: [
            dict(SALE_TXN, authorization=f"txn-{order_id}")
        ]
        try:
            result = gr.backfill_gateway_references(limit=10)
        finally:
            gr._settings_for_domain   = original_settings
            gr.get_order_transactions = original_fetch

        self.assertEqual(result["scanned"], 2)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["no_reference"], 0)
        self.assertEqual(
            frappe_stub.get_doc_values("Payment Entry", "PE-0001")["custom_gateway_reference"],
            "txn-6428",
        )
        self.assertEqual(
            frappe_stub.get_doc_values("Payment Entry", "PE-0002")["custom_gateway_reference"],
            "txn-6429",
        )

    def test_counts_entries_with_no_reference_separately(self):
        _seed_pe("PE-0001")
        self._pending_rows([
            {"pe_name": "PE-0001", "shopify_order_id": "6428", "shopify_store": "s.myshopify.com"},
        ])

        original_settings = gr._settings_for_domain
        original_fetch    = gr.get_order_transactions
        gr._settings_for_domain   = lambda domain: frappe_stub.FakeSettings(shop_domain=domain)
        gr.get_order_transactions = lambda settings, order_id: [
            dict(SALE_TXN, authorization=None, receipt={})
        ]
        try:
            result = gr.backfill_gateway_references(limit=10)
        finally:
            gr._settings_for_domain   = original_settings
            gr.get_order_transactions = original_fetch

        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["no_reference"], 1)
        self.assertEqual(frappe_stub.WRITES, [])

    def test_store_without_token_is_reported_as_failed_not_written(self):
        _seed_pe("PE-0001")
        self._pending_rows([
            {"pe_name": "PE-0001", "shopify_order_id": "6428", "shopify_store": "s.myshopify.com"},
        ])

        original_settings = gr._settings_for_domain
        gr._settings_for_domain = lambda domain: frappe_stub.FakeSettings(shop_domain=domain, token="")
        try:
            result = gr.backfill_gateway_references(limit=10)
        finally:
            gr._settings_for_domain = original_settings

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(frappe_stub.WRITES, [])

    def test_unresolvable_store_is_failed_not_crash(self):
        _seed_pe("PE-0001")
        self._pending_rows([
            {"pe_name": "PE-0001", "shopify_order_id": "6428", "shopify_store": "gone.myshopify.com"},
        ])

        original_settings = gr._settings_for_domain
        gr._settings_for_domain = lambda domain: None
        try:
            result = gr.backfill_gateway_references(limit=10)
        finally:
            gr._settings_for_domain = original_settings

        self.assertEqual(result["failed"], 1)
        self.assertEqual(frappe_stub.WRITES, [])

    def test_empty_queue_is_a_clean_no_op(self):
        result = gr.backfill_gateway_references(limit=10)
        self.assertEqual(result["scanned"], 0)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(frappe_stub.WRITES, [])

    def test_query_orders_oldest_first_and_excludes_cancelled(self):
        """The SQL contract the backfill depends on."""
        captured = {}

        def spy_sql(query, params=None, as_dict=False, **kwargs):
            captured["query"] = " ".join(query.split())
            captured["params"] = params
            return []

        import frappe
        original = frappe.db.sql
        frappe.db.sql = spy_sql
        try:
            gr._pending_payment_entries(store="s.myshopify.com", limit=50)
        finally:
            frappe.db.sql = original

        query = captured["query"]
        self.assertIn("ORDER BY pe.creation ASC", query)
        self.assertIn("pe.docstatus != 2", query)
        self.assertIn("IFNULL(pe.custom_gateway_reference, '') = ''", query)
        # The store filter applies to the UNIONed subquery, aliased `src`, which
        # carries shopify_store from whichever route resolved the order.
        self.assertIn("src.shopify_store = %(store)s", query)
        self.assertEqual(captured["params"]["store"], "s.myshopify.com")
        self.assertEqual(captured["params"]["limit"], 50)

    def _captured_pending_query(self, **kwargs):
        captured = {}

        def spy_sql(query, params=None, as_dict=False, **_kw):
            captured["query"] = " ".join(query.split())
            captured["params"] = params
            return []

        import frappe
        original = frappe.db.sql
        frappe.db.sql = spy_sql
        try:
            gr._pending_payment_entries(**kwargs)
        finally:
            frappe.db.sql = original
        return captured

    def test_query_also_resolves_orders_through_payment_entry_reference_no(self):
        """A PE whose reference row was re-pointed to a Sales Invoice must still
        be reachable.

        ERPNext's advance allocation moves the reference from the Sales Order to
        the Sales Invoice on invoice submit, after which the Sales-Order-reference
        route finds nothing. PE.reference_no still holds the Shopify order name,
        so it is matched against Sales Order.po_no as a second route.
        """
        query = self._captured_pending_query(limit=10)["query"]
        self.assertIn("UNION", query)
        self.assertIn("so.po_no = pe2.reference_no", query)
        # Amend chains reuse a po_no; only the live order may match.
        self.assertIn("so.docstatus != 2", query)
        # The original Sales Order route is still there.
        self.assertIn("per.reference_doctype = 'Sales Order'", query)

    def test_query_reports_how_many_orders_a_payment_entry_resolves_to(self):
        """Consolidated payments must be detectable, not silently collapsed."""
        query = self._captured_pending_query(limit=10)["query"]
        self.assertIn("COUNT(DISTINCT src.shopify_order_id) AS order_count", query)

    def test_query_scopes_to_from_date_when_given(self):
        captured = self._captured_pending_query(limit=10, from_date="2026-08-01")
        self.assertIn("pe.posting_date >= %(from_date)s", captured["query"])
        self.assertEqual(captured["params"]["from_date"], "2026-08-01")

    def test_query_omits_from_date_filter_when_not_given(self):
        captured = self._captured_pending_query(limit=10)
        self.assertNotIn("posting_date", captured["query"])
        self.assertNotIn("from_date", captured["params"])

    def _captured_with_gateway_accounts(self, accounts, **kwargs):
        """Run _pending_payment_entries with _gateway_bank_accounts() stubbed.

        _gateway_bank_accounts issues its own two SELECTs before the main query,
        so the spy answers those from `accounts` and captures the last call.
        """
        captured = {}
        calls = {"n": 0}

        def spy_sql(query, params=None, as_dict=False, **_kw):
            calls["n"] += 1
            if "tabShopify Payment Gateway Mapping" in query:
                return [{"bank_account": a} for a in accounts]
            if "tabShopify Settings" in query:
                return []
            captured["query"] = " ".join(query.split())
            captured["params"] = params
            return []

        import frappe
        original = frappe.db.sql
        frappe.db.sql = spy_sql
        try:
            gr._pending_payment_entries(**kwargs)
        finally:
            frappe.db.sql = original
        return captured

    def test_invoice_route_is_restricted_to_configured_gateway_accounts(self):
        """Manual gateway payments reference only an invoice and carry a typed
        note in reference_no, so the invoice route is the only one that reaches
        them -- but it must not claim payments that merely landed on a Shopify
        order's invoice from a bank transfer or COD remittance.
        """
        accounts = ["CashFree A/C - NDIPL", "PayU Payments Private Limited - NDIPL"]
        captured = self._captured_with_gateway_accounts(accounts, limit=10)
        query = captured["query"]
        self.assertIn("per3.reference_doctype = 'Sales Invoice'", query)
        self.assertIn("so.po_no = si.po_no", query)
        self.assertIn("pe3.paid_to IN %(gw_accounts)s", query)
        self.assertEqual(captured["params"]["gw_accounts"], accounts)
        # amend chains reuse po_no; only the live order may match
        self.assertIn("so.docstatus != 2", query)

    def test_invoice_route_is_omitted_entirely_when_no_gateway_account_configured(self):
        """An empty allowlist must match nothing, never everything."""
        captured = self._captured_with_gateway_accounts([], limit=10)
        query = captured["query"]
        self.assertNotIn("Sales Invoice", query)
        self.assertNotIn("gw_accounts", query)
        self.assertNotIn("gw_accounts", captured["params"])
        # the two original routes survive
        self.assertIn("per.reference_doctype = 'Sales Order'", query)
        self.assertIn("so.po_no = pe2.reference_no", query)

    def test_gateway_accounts_come_from_settings_not_hardcoded(self):
        """Read from the Gateway Mapping rows plus each store's default."""
        seen = []

        def spy_sql(query, params=None, as_dict=False, **_kw):
            seen.append(query)
            if "tabShopify Payment Gateway Mapping" in query:
                return [{"bank_account": "CashFree A/C - NDIPL"}]
            if "tabShopify Settings" in query:
                return [{"default_bank_account": "PayU Payments Private Limited - NDIPL"}]
            return []

        import frappe
        original = frappe.db.sql
        frappe.db.sql = spy_sql
        try:
            accounts = gr._gateway_bank_accounts()
        finally:
            frappe.db.sql = original

        self.assertEqual(accounts,
            ["CashFree A/C - NDIPL", "PayU Payments Private Limited - NDIPL"])
        self.assertTrue(any("tabShopify Payment Gateway Mapping" in q for q in seen))
        self.assertTrue(any("tabShopify Settings" in q for q in seen))

    def test_query_omits_store_filter_when_not_given(self):
        captured = {}

        def spy_sql(query, params=None, as_dict=False, **kwargs):
            captured["query"] = " ".join(query.split())
            captured["params"] = params
            return []

        import frappe
        original = frappe.db.sql
        frappe.db.sql = spy_sql
        try:
            gr._pending_payment_entries(limit=10)
        finally:
            frappe.db.sql = original

        self.assertNotIn("so.shopify_store = %(store)s", captured["query"])
        self.assertNotIn("store", captured["params"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ── Split-gateway orders ──────────────────────────────────────────

CASHFREE_ACCOUNT = "CashFree A/C - NDIPL"
PAYU_ACCOUNT     = "PayU Payments Private Limited - NDIPL"

# Order #6138 as it actually arrived: Cashfree's app wrote pg_order_id and
# tagged the order, then the balance was paid through PayU.
SPLIT_ORDER = {
    "name": "#6138",
    "tags": "CASHFREE - PARTIAL COD, CASHFREE - UPI",
    "payment_gateway_names": ["Cashfree Payments"],
    "note_attributes": [
        {"name": "pg_order_id", "value": "notdrones.myshopify.com_zqfakyojtt"},
    ],
}

CASHFREE_TXN = {
    "id": 9001, "kind": "sale", "status": "success",
    "created_at": "2026-08-02T20:52:14+05:30",
    "gateway": "Cashfree Payments",
    "authorization": "6149667879",
}
PAYU_TXN = {
    "id": 9002, "kind": "sale", "status": "success",
    "created_at": "2026-08-02T22:26:51+05:30",   # LATER than the Cashfree one
    "gateway": "Cards, UPI, NB by PayU India",
    "authorization": "rd21z3yx4fqDJwEHu8h5ExYvS",
}


def _seed_pe_with_account(name, account, reference_no="CF"):
    frappe_stub.set_doc("Payment Entry", name, {
        "name": name,
        "reference_no": reference_no,
        "paid_to": account,
        "custom_gateway_reference": "",
        "custom_gateway_name": "",
    })
    return name


class TestGatewayFamily(unittest.TestCase):
    """The portal names itself differently in every place it appears."""

    def setUp(self):
        frappe_stub.reset()

    def test_the_many_spellings_of_one_portal_collapse(self):
        for value in ("Cashfree Payments", "CASHFREE - UPI", "Cashfree",
                      "CashFree A/C - NDIPL", "cashfree"):
            self.assertEqual(gr._gateway_family(value), "cashfree", value)
        for value in ("Cards, UPI, NB by PayU India", "PAYu",
                      "PayU Payments Private Limited - NDIPL"):
            self.assertEqual(gr._gateway_family(value), "payu", value)
        self.assertEqual(gr._gateway_family("snapmint, snapmint_75726098"), "snapmint")

    def test_unrecognised_and_empty_are_blank_not_guessed(self):
        for value in ("", None, "manual", "HDFC Bank - NDIPL"):
            self.assertEqual(gr._gateway_family(value), "")

    def test_account_gateway_comes_from_the_mapping_row(self):
        frappe_stub.SQL_RESULTS["tabShopify Payment Gateway Mapping"] = [
            {"shopify_gateway": "PAYu", "tag_contains": "payu"},
        ]
        self.assertEqual(gr._gateway_for_account("Weirdly Renamed A/C - X"), "payu")

    def test_account_gateway_falls_back_to_the_account_name(self):
        """No mapping row: these accounts are named after the portal anyway."""
        self.assertEqual(gr._gateway_for_account(PAYU_ACCOUNT), "payu")
        self.assertEqual(gr._gateway_for_account(CASHFREE_ACCOUNT), "cashfree")

    def test_no_account_means_no_preference(self):
        self.assertEqual(gr._gateway_for_account(""), "")
        self.assertEqual(gr._gateway_for_account(None), "")


class TestTransactionPreference(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()

    def test_preferred_gateway_wins_over_the_earliest(self):
        """Each payment must take the transaction that funded IT."""
        picked = gr.select_gateway_transaction(
            [CASHFREE_TXN, PAYU_TXN], prefer_gateway=PAYU_ACCOUNT)
        self.assertEqual(picked["id"], 9002)

    def test_without_a_preference_the_earliest_still_wins(self):
        picked = gr.select_gateway_transaction([PAYU_TXN, CASHFREE_TXN])
        self.assertEqual(picked["id"], 9001)

    def test_an_unmatched_preference_is_ignored_not_fatal(self):
        """A preference that matches nothing must not lose a usable reference."""
        picked = gr.select_gateway_transaction(
            [CASHFREE_TXN, PAYU_TXN], prefer_gateway="Razorpay A/C")
        self.assertEqual(picked["id"], 9001)

    def test_preference_cannot_resurrect_a_failed_transaction(self):
        failed_payu = dict(PAYU_TXN, status="failure")
        picked = gr.select_gateway_transaction(
            [CASHFREE_TXN, failed_payu], prefer_gateway=PAYU_ACCOUNT)
        self.assertEqual(picked["id"], 9001)


class TestSkipOrderSources(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()

    def test_order_level_source_is_dropped_when_asked(self):
        ref, source = gr._resolve_reference(PAYU_TXN, SPLIT_ORDER, skip_order_sources=True)
        self.assertEqual(ref, "rd21z3yx4fqDJwEHu8h5ExYvS")
        self.assertEqual(source, "transaction.authorization")

    def test_order_level_source_still_outranks_by_default(self):
        ref, source = gr._resolve_reference(PAYU_TXN, SPLIT_ORDER)
        self.assertEqual(ref, "notdrones.myshopify.com_zqfakyojtt")
        self.assertEqual(source, "note_attributes.pg_order_id")


class TestSplitGatewayOrder(_OfflineCase):
    """Order #6138: Cashfree took 1,999.80 and PayU took the 7,999.20 balance.

    Both Payment Entries were taking Cashfree's order-level pg_order_id, so the
    PayU payment carried a key that joined to Cashfree's settlement row.
    """

    def setUp(self):
        super().setUp()
        gr.get_order = lambda settings, order_id: SPLIT_ORDER

    def test_payu_payment_takes_the_payu_transaction_reference(self):
        pe = _seed_pe_with_account("PE-PAYU", PAYU_ACCOUNT)
        result = gr.capture_gateway_reference(
            pe, 7768405639273, settings=frappe_stub.FakeSettings(),
            transactions=[CASHFREE_TXN, PAYU_TXN])

        self.assertEqual(result, "rd21z3yx4fqDJwEHu8h5ExYvS")
        stored = frappe_stub.get_doc_values("Payment Entry", pe)
        self.assertEqual(stored["custom_gateway_reference"], "rd21z3yx4fqDJwEHu8h5ExYvS")
        self.assertNotEqual(stored["custom_gateway_reference"],
                            "notdrones.myshopify.com_zqfakyojtt")

    def test_payu_payment_is_not_labelled_with_the_orders_cashfree_tags(self):
        pe = _seed_pe_with_account("PE-PAYU-NAME", PAYU_ACCOUNT)
        gr.capture_gateway_reference(
            pe, 7768405639273, settings=frappe_stub.FakeSettings(),
            transactions=[CASHFREE_TXN, PAYU_TXN])

        name = frappe_stub.get_doc_values("Payment Entry", pe)["custom_gateway_name"]
        self.assertNotIn("CASHFREE", name.upper())
        self.assertIn("payu", name.lower())

    def test_cashfree_payment_on_the_same_order_is_unchanged(self):
        """The regression guard: same-gateway payments keep the order-level key.

        Every reference already captured came down this path, so it must not
        move.
        """
        pe = _seed_pe_with_account("PE-CF", CASHFREE_ACCOUNT)
        result = gr.capture_gateway_reference(
            pe, 7768405639273, settings=frappe_stub.FakeSettings(),
            transactions=[CASHFREE_TXN, PAYU_TXN])

        self.assertEqual(result, "notdrones.myshopify.com_zqfakyojtt")
        stored = frappe_stub.get_doc_values("Payment Entry", pe)
        self.assertEqual(stored["custom_gateway_name"],
                         "CASHFREE - PARTIAL COD, CASHFREE - UPI")

    def test_a_payment_with_no_account_behaves_as_before(self):
        """No account to key on: nothing is inferred, the order-level rule holds."""
        pe = _seed_pe_with_account("PE-NOACCT", "")
        result = gr.capture_gateway_reference(
            pe, 7768405639273, settings=frappe_stub.FakeSettings(),
            transactions=[CASHFREE_TXN, PAYU_TXN])
        self.assertEqual(result, "notdrones.myshopify.com_zqfakyojtt")

    def test_single_gateway_order_never_reaches_for_transactions(self):
        """Cashfree orders must still cost one request, not two."""
        calls = []
        real = gr.get_order_transactions
        gr.get_order_transactions = lambda s, o: (calls.append(o) or [CASHFREE_TXN])
        self.addCleanup(setattr, gr, "get_order_transactions", real)

        pe = _seed_pe_with_account("PE-CF-ONE", CASHFREE_ACCOUNT)
        gr.capture_gateway_reference(pe, 7768405639273,
                                     settings=frappe_stub.FakeSettings())
        self.assertEqual(calls, [])
