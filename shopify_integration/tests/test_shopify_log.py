"""
test_shopify_log.py — tests for Shopify Log payload correction.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_shopify_log -v

The point of these tests is that a correction can never quietly become a
different order, and that the original payload is never overwritten — the log
has to stay usable as the record of what Shopify actually sent.
"""

import json
import unittest

from shopify_integration.tests import frappe_stub

frappe_stub.install()

import frappe  # noqa: E402  (the stub)

from shopify_integration.shopify_integration.doctype.shopify_log import (  # noqa: E402
    shopify_log as sl,
)

ORDER_ID = "6428"


def make_log(payload=None, corrected=None, order_id=ORDER_ID):
    """A dict standing in for a Shopify Log doc (both expose .get())."""
    return {
        "name": "SHLOG-0001",
        "shopify_order_id": order_id,
        "payload": json.dumps(payload) if payload is not None else "",
        "corrected_payload": json.dumps(corrected) if corrected is not None else "",
    }


class DictDoc(dict):
    """dict with .get() — matches how the code reads a Frappe doc."""


# ── Which payload gets replayed ───────────────────────────────────────────────

class TestGetEffectivePayload(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()

    def test_original_when_no_correction(self):
        log = DictDoc(make_log(payload={"id": ORDER_ID, "zip": "400001"}))
        self.assertEqual(json.loads(sl.get_effective_payload(log))["zip"], "400001")

    def test_correction_wins(self):
        log = DictDoc(make_log(
            payload={"id": ORDER_ID, "zip": "999999"},
            corrected={"id": ORDER_ID, "zip": "400001"},
        ))
        self.assertEqual(json.loads(sl.get_effective_payload(log))["zip"], "400001")

    def test_whitespace_only_correction_is_ignored(self):
        log = DictDoc({
            "shopify_order_id": ORDER_ID,
            "payload": '{"id": "6428"}',
            "corrected_payload": "   \n  ",
        })
        self.assertEqual(json.loads(sl.get_effective_payload(log))["id"], ORDER_ID)

    def test_empty_everywhere(self):
        self.assertEqual(sl.get_effective_payload(DictDoc(make_log())), "")

    def test_missing_keys_do_not_crash(self):
        self.assertEqual(sl.get_effective_payload(DictDoc({})), "")


# ── Parsing ───────────────────────────────────────────────────────────────────

class TestParsePayload(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()

    def test_valid_object(self):
        self.assertEqual(sl._parse_payload('{"id": 1}'), {"id": 1})

    def test_invalid_json_raises(self):
        with self.assertRaises(frappe.ValidationError) as ctx:
            sl._parse_payload("{not json", "Corrected Payload")
        self.assertIn("Corrected Payload", str(ctx.exception))

    def test_json_array_is_rejected(self):
        """A list would break every order_data.get(...) downstream."""
        with self.assertRaises(frappe.ValidationError):
            sl._parse_payload('[{"id": 1}]')

    def test_json_scalar_is_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            sl._parse_payload('"just a string"')

    def test_empty_string_raises(self):
        with self.assertRaises(frappe.ValidationError):
            sl._parse_payload("")


# ── The guard that matters most ───────────────────────────────────────────────

class TestAssertSameOrder(unittest.TestCase):
    """
    A correction must not be able to move a log onto a different Shopify order.
    Without this, one mistyped digit creates a Sales Order against the wrong
    customer while the log still claims to be about this one.
    """

    def setUp(self):
        frappe_stub.reset()

    def test_matching_id_passes(self):
        log = DictDoc(make_log())
        sl._assert_same_order(log, {"id": ORDER_ID})          # str
        sl._assert_same_order(log, {"id": int(ORDER_ID)})     # int, same value

    def test_different_id_raises(self):
        log = DictDoc(make_log())
        with self.assertRaises(frappe.ValidationError) as ctx:
            sl._assert_same_order(log, {"id": "9999"})
        self.assertIn("9999", str(ctx.exception))
        self.assertIn(ORDER_ID, str(ctx.exception))

    def test_dropped_id_raises(self):
        """Losing `id` breaks duplicate detection for every future webhook."""
        log = DictDoc(make_log())
        with self.assertRaises(frappe.ValidationError) as ctx:
            sl._assert_same_order(log, {"zip": "400001"})
        self.assertIn("id", str(ctx.exception))

    def test_blank_id_raises(self):
        log = DictDoc(make_log())
        with self.assertRaises(frappe.ValidationError):
            sl._assert_same_order(log, {"id": "   "})

    def test_log_without_order_id_is_not_checked(self):
        """Early-failure logs may have no order id column; retry still guards."""
        log = DictDoc(make_log(order_id=""))
        sl._assert_same_order(log, {"id": "anything"})
        sl._assert_same_order(log, {})


# ── Storing a correction ──────────────────────────────────────────────────────

class TestStoreCorrection(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()
        frappe_stub.set_doc("Shopify Log", "SHLOG-0001", make_log(
            payload={"id": ORDER_ID, "shipping_address": {"zip": "999999"}},
        ))

    def _stored(self):
        return frappe_stub.get_doc_values("Shopify Log", "SHLOG-0001")

    def test_writes_correction_and_attribution(self):
        sl._store_correction(
            "SHLOG-0001",
            json.dumps({"id": ORDER_ID, "shipping_address": {"zip": "400001"}}),
            "Wrong pincode from customer",
            "Manually edited",
        )
        stored = self._stored()
        self.assertEqual(
            json.loads(stored["corrected_payload"])["shipping_address"]["zip"],
            "400001",
        )
        self.assertEqual(stored["correction_reason"], "Wrong pincode from customer")
        self.assertEqual(stored["corrected_by"], "Administrator")
        self.assertEqual(stored["payload_correction_status"], "Manually edited")
        self.assertIsNotNone(stored["corrected_at"])

    def test_original_payload_is_never_touched(self):
        """The whole point: the log stays the record of what Shopify sent."""
        before = self._stored()["payload"]
        sl._store_correction(
            "SHLOG-0001",
            json.dumps({"id": ORDER_ID, "shipping_address": {"zip": "400001"}}),
            "fix", "Manually edited",
        )
        self.assertEqual(self._stored()["payload"], before)
        self.assertEqual(json.loads(before)["shipping_address"]["zip"], "999999")
        for _dt, _name, values, _kw in frappe_stub.WRITES:
            self.assertNotIn("payload", values)

    def test_stored_json_is_normalised(self):
        sl._store_correction(
            "SHLOG-0001", '{"id":"6428","a":1}', "fix", "Manually edited"
        )
        stored = self._stored()["corrected_payload"]
        self.assertIn("\n", stored, "should be pretty-printed for a human to read")
        self.assertEqual(json.loads(stored)["a"], 1)

    def test_invalid_json_writes_nothing(self):
        with self.assertRaises(frappe.ValidationError):
            sl._store_correction("SHLOG-0001", "{broken", "fix", "Manually edited")
        self.assertEqual(frappe_stub.WRITES, [])

    def test_wrong_order_id_writes_nothing(self):
        with self.assertRaises(frappe.ValidationError):
            sl._store_correction(
                "SHLOG-0001", json.dumps({"id": "1234"}), "fix", "Manually edited"
            )
        self.assertEqual(frappe_stub.WRITES, [])

    def test_reason_is_truncated(self):
        sl._store_correction(
            "SHLOG-0001", json.dumps({"id": ORDER_ID}), "x" * 900, "Manually edited"
        )
        self.assertEqual(len(self._stored()["correction_reason"]), 500)


class TestSaveCorrectedPayload(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()
        frappe_stub.set_doc("Shopify Log", "SHLOG-0001",
                            make_log(payload={"id": ORDER_ID}))

    def test_empty_payload_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            sl.save_corrected_payload("SHLOG-0001", "   ")
        self.assertEqual(frappe_stub.WRITES, [])

    def test_default_reason_is_recorded(self):
        sl.save_corrected_payload("SHLOG-0001", json.dumps({"id": ORDER_ID}))
        stored = frappe_stub.get_doc_values("Shopify Log", "SHLOG-0001")
        self.assertTrue(stored["correction_reason"])


class TestClearCorrection(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()
        frappe_stub.set_doc("Shopify Log", "SHLOG-0001", make_log(
            payload={"id": ORDER_ID, "zip": "999999"},
            corrected={"id": ORDER_ID, "zip": "400001"},
        ))

    def test_clears_all_correction_fields(self):
        sl.clear_corrected_payload("SHLOG-0001")
        stored = frappe_stub.get_doc_values("Shopify Log", "SHLOG-0001")
        self.assertEqual(stored["corrected_payload"], "")
        self.assertEqual(stored["correction_reason"], "")
        self.assertIsNone(stored["corrected_by"])
        self.assertEqual(stored["payload_correction_status"], "")

    def test_original_survives_the_clear(self):
        sl.clear_corrected_payload("SHLOG-0001")
        stored = frappe_stub.get_doc_values("Shopify Log", "SHLOG-0001")
        self.assertEqual(json.loads(stored["payload"])["zip"], "999999")

    def test_retry_falls_back_to_original_after_clearing(self):
        sl.clear_corrected_payload("SHLOG-0001")
        log = DictDoc(frappe_stub.get_doc_values("Shopify Log", "SHLOG-0001"))
        self.assertEqual(json.loads(sl.get_effective_payload(log))["zip"], "999999")


class TestGetPayloadForEdit(unittest.TestCase):
    def setUp(self):
        frappe_stub.reset()

    def test_returns_pretty_original(self):
        frappe_stub.set_doc("Shopify Log", "SHLOG-0001",
                            make_log(payload={"id": ORDER_ID, "a": 1}))
        out = sl.get_payload_for_edit("SHLOG-0001")
        self.assertIn("\n", out["payload"])
        self.assertFalse(out["is_corrected"])
        self.assertEqual(out["shopify_order_id"], ORDER_ID)

    def test_returns_the_correction_when_present(self):
        """Repeated edits must build on the last correction, not the original."""
        frappe_stub.set_doc("Shopify Log", "SHLOG-0001", make_log(
            payload={"id": ORDER_ID, "zip": "999999"},
            corrected={"id": ORDER_ID, "zip": "400001"},
        ))
        out = sl.get_payload_for_edit("SHLOG-0001")
        self.assertEqual(json.loads(out["payload"])["zip"], "400001")
        self.assertTrue(out["is_corrected"])

    def test_unparseable_payload_is_handed_back_for_repair(self):
        frappe_stub.set_doc("Shopify Log", "SHLOG-0001", {
            "shopify_order_id": ORDER_ID,
            "payload": "{broken json",
            "corrected_payload": "",
        })
        self.assertEqual(sl.get_payload_for_edit("SHLOG-0001")["payload"], "{broken json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
