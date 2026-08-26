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


class TestCaptureGatewayReference(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()

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


class TestCaptureForOrder(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()

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


class TestBackfill(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()

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
        self.assertIn("so.shopify_store = %(store)s", query)
        self.assertEqual(captured["params"]["store"], "s.myshopify.com")
        self.assertEqual(captured["params"]["limit"], 50)

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
