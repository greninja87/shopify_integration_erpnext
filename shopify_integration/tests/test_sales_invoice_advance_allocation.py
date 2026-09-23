"""
test_sales_invoice_advance_allocation.py — guards against FIFO advance sweep.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_sales_invoice_advance_allocation -v

Background: both SI-creation paths set allocate_advances_automatically = 1 so
a Payment Entry the integration created gets linked to the invoice on submit.
Left at that, ERPNext's set_advances() also treats every OTHER unallocated
advance on the customer's account as fair game (include_unallocated=True by
default) and sweeps them in oldest-first — so a same-customer order created
later can silently claim a payment that belongs to an earlier, still-unpaid
order. only_include_allocated_payments = 1 turns that off (see the incident
notes beside both call sites in sales_invoice.py).

erpnext is not installed in this environment (no bench). Both SI-creation
functions import erpnext's make_sales_invoice mapper lazily, inside the
function body, so this stubs that import target into sys.modules before
calling in — the same technique frappe_stub.py uses for `frappe` itself.
"""

import sys
import types
import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

import frappe  # noqa: E402

from shopify_integration.tests.frappe_stub import FakeSettings  # noqa: E402


class FakeDoc:
    """Minimal stand-in for a submitted ERPNext document (SO or DN)."""

    def __init__(self, **values):
        self._values = values

    def __getattr__(self, key):
        try:
            return self._values[key]
        except KeyError:
            raise AttributeError(key)

    def get(self, key, default=None):
        return self._values.get(key, default)


class FakeSI:
    """Minimal stand-in for the Sales Invoice doc erpnext's mapper returns."""

    def __init__(self):
        self.items = []
        self.flags = types.SimpleNamespace(ignore_permissions=False)
        self.docstatus = 0
        self.name = "SINV-0001"

    def get(self, key, default=None):
        return getattr(self, key, default)

    def run_method(self, *a, **k):
        pass

    def insert(self, *a, **k):
        self.docstatus = 0

    def submit(self, *a, **k):
        self.docstatus = 1

    def reload(self):
        pass


def _install_fake_mapper(dotted_path: str, fn):
    """Register `fn` as the module attribute a lazy `from X import Y` resolves."""
    module_path, attr = dotted_path.rsplit(".", 1)
    module = types.ModuleType(module_path)
    setattr(module, attr, fn)
    sys.modules[module_path] = module
    # Every parent package must also exist for `import erpnext.x.y.z` to resolve.
    parts = module_path.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            pkg = types.ModuleType(parent)
            pkg.__path__ = []
            sys.modules[parent] = pkg


class TestSalesInvoiceFromSO(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()
        self.fake_si = FakeSI()
        _install_fake_mapper(
            "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
            lambda so_name: self.fake_si,
        )
        from shopify_integration.utils import sales_invoice as si_mod
        self.si_mod = si_mod

    def test_restricts_advance_allocation_to_this_order(self):
        so = FakeDoc(name="SO-0001", company="Test Company", grand_total=1000)
        settings = FakeSettings()

        self.si_mod.create_sales_invoice_from_so(so, settings)

        self.assertEqual(self.fake_si.allocate_advances_automatically, 1)
        self.assertEqual(
            self.fake_si.only_include_allocated_payments, 1,
            "without this, ERPNext sweeps every unallocated advance on the "
            "customer's account onto this invoice, oldest first — not just "
            "the Payment Entry created for this order",
        )


class TestSalesInvoiceFromDN(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()
        self.fake_si = FakeSI()
        _install_fake_mapper(
            "erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice",
            lambda dn_name: self.fake_si,
        )
        from shopify_integration.utils import sales_invoice as si_mod
        self.si_mod = si_mod

    def test_restricts_advance_allocation_to_this_order(self):
        settings = FakeSettings()

        self.si_mod.create_sales_invoice_from_dn("DN-0001", settings)

        self.assertEqual(self.fake_si.allocate_advances_automatically, 1)
        self.assertEqual(
            self.fake_si.only_include_allocated_payments, 1,
            "without this, ERPNext sweeps every unallocated advance on the "
            "customer's account onto this invoice, oldest first — not just "
            "the Payment Entry created for this order",
        )


if __name__ == "__main__":
    unittest.main()
