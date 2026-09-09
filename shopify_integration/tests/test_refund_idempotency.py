"""
test_refund_idempotency.py — the key on refundCreate, and the currency it pays in.

Run WITHOUT a bench, from the app root:

    python -m unittest shopify_integration.tests.test_refund_idempotency -v

Two findings from reading the app against the 2026-01 Admin GraphQL reference on
2026-09-09, both about the mutation rather than the query.

**The key.** Shopify's changelog of 12 December 2025 makes `@idempotent`
mandatory on `refundCreate` from API version **2026-04** — one of seventeen
mutations — and it is *not* marked required in the schema, so nothing catches
its absence before the post. On 2026-01 the write-back works; the day somebody
bumps the version it fails at runtime, on the money post, where a failure is not
a clean refusal: `proves_not_executed` is false for it, so the row lands on
`Unverified` and a person has to open the order in Shopify.

So the directive is now sent whenever the configured API version is at or past
`IDEMPOTENCY_REQUIRED_FROM`, and not before. Not "always on", deliberately: an
unknown directive is a query-level error, and adding an unproven one to the
first live payout would risk turning a clean refusal into an `Unverified` row
for no gain on a version that does not require it. Bumping a store's
`api_version` to 2026-04 is the switch, and it turns the key on by itself.

The key is a UUIDv5 over the whole input, which is what the dedup rules demand:
keys live 24 hours, a repeat with the same key and the same parameters returns
the first response instead of refunding again, and a repeat with the same key
and *different* parameters is refused as `IDEMPOTENCY_KEY_PARAMETER_MISMATCH`.
A key over the Refund Request name and amount alone — what an earlier docstring
proposed — would hit that mismatch as soon as anyone edited the reason note
between two attempts.

**The currency.** `RefundInput.currency` is *"the presentment currency, which is
the currency used by the customer"*, and `OrderTransactionInput.amount` is a
bare `Money` scalar carrying no currency of its own. Every figure this module
allocates is read from `presentmentMoney`, and the currency was never sent — so
on an order whose presentment currency differs from the shop currency, the right
number would have been refunded in the wrong currency. Inert on a single-
currency store and wrong the moment one is not.
"""

import re
import unittest
import uuid

from shopify_integration.tests import frappe_stub

frappe_stub.install()

from shopify_integration.tests.test_refund_writeback import (  # noqa: E402
    ORDER_GID,
    REFUND,
    WritebackTestCase,
    refund_created,
    targets_response,
)
from shopify_integration.utils import refund as r  # noqa: E402

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def payload(amount="12999.00", note="Damaged in transit", currency="INR"):
    """A RefundInput as build_refund_input returns it."""
    body = {
        "orderId": ORDER_GID,
        "notify": False,
        "note": note,
        "transactions": [{
            "orderId": ORDER_GID,
            "parentId": "gid://shopify/OrderTransaction/99",
            "kind": "REFUND",
            "gateway": "manual",
            "amount": amount,
        }],
    }
    if currency:
        body["currency"] = currency
    return {"input": body}


# ── The key ──────────────────────────────────────────────────────────────────

class TestTheIdempotencyKey(unittest.TestCase):

    def test_it_is_a_uuid_v5(self):
        """"We strongly recommend using UUIDs", and v5 because it is
        deterministic: the same refund retried has to produce the same key or
        the dedup protects nothing."""
        key = r.idempotency_key(REFUND, payload())
        self.assertRegex(key, UUID_RE)
        self.assertEqual(uuid.UUID(key).version, 5)

    def test_the_same_refund_and_input_give_the_same_key(self):
        self.assertEqual(
            r.idempotency_key(REFUND, payload()),
            r.idempotency_key(REFUND, payload()),
        )

    def test_key_ordering_inside_the_payload_does_not_change_it(self):
        """A dict is not ordered by anything Shopify cares about, and a key that
        moved when a field was reordered would be a new refund every time."""
        one = payload()
        other = {"input": dict(reversed(list(one["input"].items())))}
        self.assertEqual(r.idempotency_key(REFUND, one),
                         r.idempotency_key(REFUND, other))

    def test_a_different_amount_is_a_different_key(self):
        self.assertNotEqual(r.idempotency_key(REFUND, payload()),
                            r.idempotency_key(REFUND, payload(amount="1.00")))

    def test_a_different_note_is_a_different_key(self):
        """The mismatch case, and the reason the key covers the whole input:
        Shopify refuses a key reused with different parameters, and an edited
        reason note is different parameters."""
        self.assertNotEqual(r.idempotency_key(REFUND, payload()),
                            r.idempotency_key(REFUND, payload(note="Wrong item")))

    def test_a_different_refund_request_is_a_different_key(self):
        self.assertNotEqual(r.idempotency_key(REFUND, payload()),
                            r.idempotency_key("REF-0008", payload()))

    def test_it_is_stable_across_runs(self):
        """Pinned to a literal.  The namespace is part of the contract with
        Shopify's 24-hour dedup window: change it and a retry stops being
        recognised as the same refund, which is the one thing the key is for."""
        self.assertEqual(
            r.idempotency_key("REF-0007", {"input": {"orderId": "gid://x"}}),
            str(uuid.uuid5(
                r.IDEMPOTENCY_NAMESPACE,
                'REF-0007|{"input":{"orderId":"gid://x"}}',
            )),
        )

    def test_the_namespace_is_the_documented_one(self):
        self.assertEqual(
            r.IDEMPOTENCY_NAMESPACE,
            uuid.uuid5(uuid.NAMESPACE_URL,
                       "https://electrobotic.in/shopify_integration/refund-writeback"),
        )


# ── When it is sent ──────────────────────────────────────────────────────────

class TestTheDirectiveFollowsTheApiVersion(WritebackTestCase):

    def set_version(self, version):
        self.settings._values["api_version"] = version

    def refund_once(self):
        self.responses = [targets_response(), refund_created()]
        return r.write_back_refund(REFUND)

    def posted_mutation(self):
        return self.mutations[0]["query"]

    def test_2026_01_posts_no_directive(self):
        """Optional there, and an unproven directive on a live payout is a risk
        with nothing on the other side of it."""
        self.set_version("2026-01")
        self.refund_once()
        self.assertNotIn("@idempotent", self.posted_mutation())

    def test_2026_04_posts_one(self):
        self.set_version("2026-04")
        self.refund_once()
        self.assertIn("@idempotent", self.posted_mutation())

    def test_a_later_version_posts_one_too(self):
        for version in ("2026-07", "2027-01", "unstable"):
            self.seed()
            self.set_version(version)
            self.refund_once()
            self.assertIn("@idempotent", self.posted_mutation(), version)

    def test_the_key_it_posts_is_the_one_for_that_input(self):
        self.set_version("2026-04")
        self.refund_once()
        sent = self.mutations[0]
        key = r.idempotency_key(REFUND, sent["variables"])
        self.assertIn(f'@idempotent(key: "{key}")', sent["query"])

    def test_an_unset_version_falls_back_to_the_default_and_stays_off(self):
        self.settings._values.pop("api_version", None)
        self.refund_once()
        self.assertNotIn("@idempotent", self.posted_mutation())

    def test_the_post_is_still_made_at_most_once_either_way(self):
        """The key makes a re-post safe; it does not make one happen.  Turning
        execute()'s retries back on is a CONTRACT_VERSION decision and nobody
        has taken it, so every post-send verdict still describes the only
        request that could have run."""
        for version in ("2026-01", "2026-04"):
            self.seed()
            self.set_version(version)
            self.refund_once()
            self.assertIs(self.posted_kwargs("refundCreate")["idempotent"], False)

    def test_the_payout_still_succeeds_with_the_directive_on(self):
        self.set_version("2026-04")
        result = self.refund_once()
        self.assertEqual(result["outcome"], r.OUTCOME_PAID, result)


# ── The currency ─────────────────────────────────────────────────────────────

class TestTheRefundNamesItsCurrency(unittest.TestCase):
    """`OrderTransactionInput.amount` is a bare `Money` scalar; the currency for
    the whole refund comes from `RefundInput.currency`, and it has to be the
    presentment one because that is where every figure here is read from."""

    def node(self, currency="INR", amount="5.00"):
        return {
            "id": "t/1", "kind": "SALE", "status": "SUCCESS", "gateway": "manual",
            "amountSet": {"presentmentMoney": {"amount": amount,
                                               "currencyCode": currency}},
            "maximumRefundableV2": {"amount": amount, "currencyCode": currency},
        }

    def test_the_plan_carries_the_presentment_currency(self):
        plan = r.plan_refund([self.node()], 5.0)
        self.assertEqual(plan["currency"], "INR")

    def test_the_input_sends_it(self):
        plan = r.plan_refund([self.node()], 5.0)
        built = r.build_refund_input("gid://shopify/Order/1", plan, "note")
        self.assertEqual(built["input"]["currency"], "INR")

    def test_a_foreign_presentment_currency_is_what_gets_sent(self):
        """The bug this fixes: the amounts were always presentment, the currency
        was never said, so a shop-currency default would have refunded the right
        number in the wrong money."""
        plan = r.plan_refund([self.node(currency="USD")], 5.0)
        self.assertEqual(
            r.build_refund_input("gid://shopify/Order/1", plan, "note")
            ["input"]["currency"],
            "USD",
        )

    def test_no_currency_reported_sends_no_currency(self):
        """Optional in RefundInput, and inventing one is worse than omitting it
        — Shopify then applies the order's own."""
        plan = r.plan_refund([self.node(currency=None)], 5.0)
        self.assertEqual(plan["currency"], "")
        built = r.build_refund_input("gid://shopify/Order/1", plan, "note")
        self.assertNotIn("currency", built["input"])

    def test_it_is_normalised_to_upper_case(self):
        plan = r.plan_refund([self.node(currency="inr")], 5.0)
        self.assertEqual(plan["currency"], "INR")

    def test_a_refusal_carries_no_currency_to_send(self):
        plan = r.plan_refund([self.node(amount="1.00")], 5.0)
        self.assertEqual(plan["problem_code"], "insufficient_refundable")
        self.assertEqual(plan["currency"], "")


class TestTheCurrencyReachesShopify(WritebackTestCase):

    def test_the_posted_input_names_it(self):
        self.responses = [targets_response(), refund_created()]
        r.write_back_refund(REFUND)
        self.assertEqual(
            self.mutations[0]["variables"]["input"]["currency"], "INR"
        )

    def test_an_order_that_reports_none_posts_none(self):
        self.responses = [targets_response(transactions=[{
            "id": "gid://shopify/OrderTransaction/99",
            "kind": "SALE", "status": "SUCCESS", "gateway": "manual",
            "amountSet": {"presentmentMoney": {"amount": "12999.00"}},
        }]), refund_created()]
        r.write_back_refund(REFUND)
        self.assertNotIn("currency", self.mutations[0]["variables"]["input"])


if __name__ == "__main__":
    unittest.main()
