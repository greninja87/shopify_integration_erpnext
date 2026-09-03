"""
test_customer_gst_dedup.py — one shopper must map to one ERPNext Customer,
whether or not that particular order carried a GSTIN.

Scenario this pins (live incident, store notdrones.myshopify.com):

    Shopify orders #6267 and #6268 were placed 2.5 minutes apart by the same
    Shopify customer (id 9414355484777, phone 9358424160,
    mittal.achin@gmail.com).  #6267 carried no GSTIN, #6268 carried
    05ANMPM2656H1ZU.  ERPNext ended up with TWO customers:

        NOT-CUS-4455  Achin Mittal              (Individual, from #6267)
        NOT-CUS-4456  M/S Shivam Army Traders   (Company,    from #6268)

    Cause: get_or_create_customer() short-circuited to _create_customer() as
    soon as gst_legal_name was set, skipping the shopify-id / phone / email
    matching entirely.  The GSTIN identifies the *tax entity*, not the person —
    a proprietor ordering once personally and once for the firm is still one
    shopper.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_customer_gst_dedup -v
"""

import unittest
from unittest import mock

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.utils import customer as c  # noqa: E402


GSTIN      = "05ANMPM2656H1ZU"
LEGAL      = "M/S Shivam Army Traders"
SHOPIFY_ID = "9414355484777"
PHONE      = "9358424160"
EMAIL      = "mittal.achin@gmail.com"


def shopper(shopify_id=SHOPIFY_ID, phone=PHONE, email=EMAIL):
    return {
        "id":         shopify_id,
        "phone":      phone,
        "email":      email,
        "first_name": "Achin",
        "last_name":  "Mittal",
    }


class FakeSettings:
    customer_group = "Individual"
    territory      = "India"

    def get(self, key, default=None):
        return {"customer_naming_series": ""}.get(key, default)


def seed_b2c_customer(name="NOT-CUS-4455", **overrides):
    """The Individual customer created by the earlier non-GST order."""
    values = {
        "customer_name":       "Achin Mittal",
        "customer_type":       "Individual",
        "shopify_customer_id": SHOPIFY_ID,
        "shopify_phone":       PHONE,
        "shopify_email":       EMAIL,
        "mobile_no":           PHONE,
        "email_id":            EMAIL,
    }
    values.update(overrides)
    frappe_stub.set_doc("Customer", name, values)
    return name


def get_all_returning(address_gstins=(), linked_addresses=()):
    """A frappe.get_all dispatcher: Dynamic Link rows, then Address rows."""
    def _get_all(doctype, filters=None, fields=None, pluck=None, **kwargs):
        if doctype == "Dynamic Link":
            return list(linked_addresses)
        if doctype == "Address":
            if pluck == "gstin":
                return list(address_gstins)
            return []
        return []
    return _get_all


class CustomerGstDedup(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()

    def _run(self, **kwargs):
        defaults = dict(
            shopify_customer=shopper(),
            billing_address={},
            shipping_address={},
            settings=FakeSettings(),
            gstin=GSTIN,
            gst_legal_name=LEGAL,
            gst_customer_type="Company",
        )
        defaults.update(kwargs)
        return c.get_or_create_customer(**defaults)

    @staticmethod
    def _customer_writes(name=None):
        merged = {}
        for doctype, docname, values, _ in frappe_stub.WRITES:
            if doctype == "Customer" and (name is None or docname == name):
                merged.update(values)
        return merged

    @staticmethod
    def _customers_created():
        return [d for d in frappe_stub.INSERTS if d.get("doctype") == "Customer"]

    # ── The incident ──────────────────────────────────────────────────────────

    def test_gst_order_reuses_customer_matched_by_shopify_id(self):
        seed_b2c_customer()
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self.assertEqual(self._run(), "NOT-CUS-4455")
        self.assertEqual(
            self._customers_created(), [],
            "a GSTIN on a repeat order must not spawn a second Customer",
        )

    def test_gst_order_reuses_customer_matched_by_phone(self):
        seed_b2c_customer(shopify_customer_id="")
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self.assertEqual(
                self._run(shopify_customer=shopper(shopify_id="")), "NOT-CUS-4455"
            )
        self.assertEqual(self._customers_created(), [])

    def test_gst_order_reuses_customer_matched_by_email(self):
        seed_b2c_customer(shopify_customer_id="", shopify_phone="", mobile_no="")
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self.assertEqual(
                self._run(shopify_customer=shopper(shopify_id="", phone="")),
                "NOT-CUS-4455",
            )
        self.assertEqual(self._customers_created(), [])

    # ── What the reused record must be upgraded to ────────────────────────────

    def test_reused_customer_takes_gst_legal_name_and_type(self):
        """The tax invoice party name must be the GST-registered legal name."""
        seed_b2c_customer()
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self._run()
        written = self._customer_writes("NOT-CUS-4455")
        self.assertEqual(written.get("customer_name"), LEGAL)
        self.assertEqual(written.get("customer_type"), "Company")

    def test_reused_customer_keeps_name_when_it_already_has_another_gstin(self):
        """Two GSTINs behind one Shopify login must not flip-flop the name."""
        seed_b2c_customer(customer_name="Existing Traders Pvt Ltd", customer_type="Company")
        dispatcher = get_all_returning(
            address_gstins=["24AAJCN9870H1Z0"], linked_addresses=["Existing-Billing"]
        )
        with mock.patch.object(c.frappe, "get_all", dispatcher):
            self.assertEqual(self._run(), "NOT-CUS-4455")
        renames = [
            v for dt, n, v, _ in frappe_stub.WRITES
            if dt == "Customer" and "customer_name" in v
        ]
        self.assertEqual(renames, [], "must not rename a customer that already has a GSTIN")

    def test_shopify_sync_fields_are_stamped_on_the_reused_customer(self):
        seed_b2c_customer(shopify_customer_id="")
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self._run()
        self.assertEqual(
            self._customer_writes().get("shopify_customer_id"), SHOPIFY_ID
        )

    def test_reused_customer_keeps_its_existing_addresses(self):
        """Reusing a customer must not create or re-flag any Address here.

        The GST-registered address is added by gst.resolve_billing_from_gstin()
        *after* this function returns, so that it is linked to whichever customer
        was resolved.  If customer.py also created an address on this path the
        shopper would end up with a stray duplicate of their personal address.
        """
        seed_b2c_customer()
        billing = {
            "name": "Achin Mittal", "address1": "Flat No 102 Anand Indulgence Apartment",
            "city": "Dehradun", "province": "Uttarakhand", "zip": "248001",
            "country_name": "India", "phone": PHONE,
        }
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self.assertEqual(
                self._run(billing_address=billing, shipping_address=billing),
                "NOT-CUS-4455",
            )
        self.assertEqual(
            [d for d in frappe_stub.INSERTS if d.get("doctype") == "Address"], []
        )
        self.assertEqual(
            [(dt, n) for dt, n, _, _ in frappe_stub.WRITES if dt == "Address"], []
        )

    # ── Paths that must not regress ───────────────────────────────────────────

    def test_first_ever_b2b_order_still_creates_a_company_customer(self):
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self._run()
        created = self._customers_created()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["customer_name"], LEGAL)
        self.assertEqual(created[0]["customer_type"], "Company")

    def test_existing_gstin_address_still_wins(self):
        """Pass 0 (GSTIN -> Address -> Customer) is unchanged."""
        seed_b2c_customer("NOT-CUS-9999")
        frappe_stub.set_doc("Address", "Shivam-Billing", {"gstin": GSTIN, "disabled": 0})
        frappe_stub.set_doc("Dynamic Link", "DL-1", {
            "parenttype":   "Address",
            "parent":       "Shivam-Billing",
            "link_doctype": "Customer",
            "link_name":    "NOT-CUS-9999",
        })
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self.assertEqual(self._run(), "NOT-CUS-9999")
        self.assertEqual(self._customers_created(), [])

    # ── Living with the duplicates the old bug already created ────────────────

    def test_disabled_duplicate_is_never_matched(self):
        """Disabling the stale record is the remediation — it must stick.

        ERPNext refuses a disabled customer on a Sales Order, so matching one
        would fail the whole order sync with "Customer is disabled".
        """
        seed_b2c_customer("NOT-CUS-4455", disabled=1, creation="2026-08-10 18:11:54")
        seed_b2c_customer(
            "NOT-CUS-4456", customer_name=LEGAL, customer_type="Company",
            disabled=0, creation="2026-08-10 18:14:28",
        )
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self.assertEqual(self._run(), "NOT-CUS-4456")
        self.assertEqual(self._customers_created(), [])

    def test_disabled_customer_on_the_gst_address_falls_through(self):
        """Pass 0 must not hand back a disabled record either."""
        seed_b2c_customer("NOT-CUS-4455", disabled=1, creation="2026-08-10 18:11:54")
        seed_b2c_customer(
            "NOT-CUS-4456", customer_name=LEGAL, customer_type="Company",
            disabled=0, creation="2026-08-10 18:14:28",
        )
        frappe_stub.set_doc("Address", "Shivam-Billing", {"gstin": GSTIN, "disabled": 0})
        frappe_stub.set_doc("Dynamic Link", "DL-1", {
            "parenttype":   "Address",
            "parent":       "Shivam-Billing",
            "link_doctype": "Customer",
            "link_name":    "NOT-CUS-4455",
        })
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self.assertEqual(self._run(), "NOT-CUS-4456")

    def test_two_enabled_duplicates_resolve_to_the_newest(self):
        """Deterministic, so a shopper cannot bounce between records per order.

        For a pair left by the old bug the later record is the GST-registered
        one, which is the record actually in use.
        """
        seed_b2c_customer("NOT-CUS-4333", customer_name="MB TRADING",
                          creation="2026-08-04 13:28:00")
        seed_b2c_customer("NOT-CUS-4722", customer_name="Mb Trading",
                          customer_type="Company", creation="2026-08-27 15:27:00")
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self.assertEqual(
                self._run(gstin=None, gst_legal_name=None), "NOT-CUS-4722"
            )

    def test_b2c_order_matching_is_unchanged(self):
        seed_b2c_customer()
        with mock.patch.object(c.frappe, "get_all", get_all_returning()):
            self.assertEqual(
                self._run(gstin=None, gst_legal_name=None, gst_customer_type="Individual"),
                "NOT-CUS-4455",
            )
        self.assertEqual(self._customers_created(), [])


if __name__ == "__main__":
    unittest.main()
