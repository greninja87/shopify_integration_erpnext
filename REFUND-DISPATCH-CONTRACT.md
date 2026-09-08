# Refund dispatch contract — `payment_portals` → `shopify_integration`

**Version 4.** Written 2026-09-04 by the `shopify_integration` side, at the
request of the `payment_portals` session, as the interface to build against.
Revised 2026-09-07.

> **Version 2 corrects a routing bug found on `electrobotictest`, and changes
> the meaning of `owns_payout`.** Version 1 derived it from `shopify_order_id`,
> which several guards returned without ever populating — so it read `false`,
> "not a Shopify order", for refunds that were Shopify's, including a Manual
> Portal Refund Shopify had **already paid**. A gate branching on it would have
> paid that customer a second time. If you integrated against version 1, read
> §3 again: branch on **`caller_must_pay`**, not on `owns_payout`.
>
> **Version 4 inverts the state this app accepts, and it is the change most
> likely to surprise an integrator.** Versions 1–3 required the Refund Request
> to be `Completed` — booked and paid — on the reasoning "Shopify is told once
> ERPNext has booked the refund, not before". That is *write-back* ordering, and
> it contradicts §0 of this very document: a successful `refundCreate` **pays
> the customer**. `payment_portals` sets `Completed` only when the Payment Entry
> is posted, so book-then-call paid the customer *after* the books said they
> were paid. Dispatch now runs from **`Approved` or `Queued`** on the new
> **`Shopify`** refund channel, and the gate is a positive allow-list on that
> channel. Two new refused codes, `channel_does_not_dispatch` and
> `already_booked`. See §2b and §10.
>
> **Version 3 removes the `no_permission` refusal, and the permission check that
> produced it.** It deadlocked: `payment_portals` authorises on `PAYOUT_ROLES`,
> this app checked submit permission on Refund Request, and a Refund Approver
> without doctype submit permission passed one gate and failed the other — which
> came back `unknown`, so neither app would pay the refund, for a reason that
> explained nothing. Authorisation is the caller's now. See §2.

`shopify_integration.utils.refund.CONTRACT_VERSION` is the machine-readable
version and every result dict carries it as `contract_version`. Bumping it is a
breaking change; new optional keys are not.

---

## 0. What this is, and the one fact that shapes all of it

`payment_portals` raises refunds. For a refund whose Sales Order came from
Shopify, the payout must go through Shopify's `refundCreate`, because the
Cashfree-OCC app bridges that into a real Cashfree refund. **A successful
`refundCreate` pays the customer.** It is a payment instruction, not a record of
one.

Everything awkward in this document follows from that. In particular there is a
failure mode that is neither success nor failure — *sent, outcome unknown* — and
it is the case this contract exists to make un-ignorable.

### Current state, so nothing is assumed

- `payment_portals` `3bc6164` added a fourth refund channel, **`Shopify`**, and
  built the whole calling side: `actions/refund_dispatch.py` resolves the hook,
  refuses on zero registered and on more than one, and reads the result dict
  through the frappe-free readings in `services/refund_dispatch_rules.py`. A
  `Payment Portal` refund on a Shopify-backed order is still refused there — it
  is the `Shopify` channel that dispatches.
- **The dispatcher IS now registered on this side** (`hooks.py`,
  `refund_payout_dispatchers`), as of contract version 4. Before that nothing was
  registered and every `Shopify`-channel refund refused with "No refund
  dispatcher is registered".
- `write_back_refund` is still **not whitelisted**, so registering it did not
  open an HTTP door. The door is `writeback_now`, behind the Refund Request form
  button, and it checks submit permission.
- `enable_refund_writeback` is **off** on every store, and that is now the only
  switch in front of a real payout. The credit-note guard is in place; the
  `payment_portals` routing guard is in place. What remains unproven is §9.5.

---

## 1. Coupling rules, both directions

- `shopify_integration` **never** imports `payment_portals`. It reads Refund
  Request through `frappe.db` by doctype name and is inert when the doctype is
  absent.
- `payment_portals` **must not** import `shopify_integration` either. It must
  stay installable and fully functional on a site with no Shopify app. So the
  call goes through Frappe's hook fan-out, never a direct import.
- The coupling is therefore exactly two things: **a hook name** and **the
  signature and result shape below**. Nothing else.

### An argument you send can vanish without an error

`frappe.call` resolves a dotted path and then calls `fn(*args, **newargs)`,
where `newargs` is the caller's kwargs **filtered to the parameters the resolved
function actually declares**. An argument the target does not accept is dropped
silently — no `TypeError`, no warning. Proven accidentally on
`electrobotictest`: a call passing `refund_request=…` to
`get_refund_writeback_status` failed complaining about a *missing* `refund_name`,
never about the unexpected keyword. (The same line is why whitelisting is not
required for the in-process hook path — the whitelist gate lives in
`frappe.handler.execute_cmd`, which only HTTP requests reach.)

**So this rule governs every optional argument across this seam:**

> An optional argument that changes a **safety** decision must be paired with a
> **positive acknowledgement in the result**, emitted only on a path that
> actually honoured it.

Without that, sending the argument to a version that does not implement it is
indistinguishable from having it honoured, and the caller believes a guard ran
when nothing did. That is the same failure class as `caller_must_pay`: make the
safe direction the default for absent, unknown and not-yet-implemented.

The arguments that exist today are safe under this rule for reasons worth
stating, not by luck:

| argument | if it were dropped | why that is acceptable |
|---|---|---|
| `refund_name` | required positional → `TypeError` | fails loudly; cannot be missed |
| `triggered_by` | defaults to `"manual"` | reaches a log line only; no decision depends on it |
| `expected_amount` | **silently no cross-check** | **not acceptable — see §9.2** |

`contract_version` does not save you here. It says what the document promises,
not what the function resolved on this call actually accepted.

---

## 2. The hook

`payment_portals` `docs/design/SHOPIFY-REFUND-WRITEBACK.md` already sketches
this: one dispatcher hook, resolved at the payout step, refusing rather than
falling back when nothing is registered. That shape is accepted as-is, and this
section just pins the names.

**It needs no new code on this side.** The dispatcher points straight at
`write_back_refund`, which already returns everything in §4.

`frappe.call` resolves a dotted path through `frappe.get_attr` and does not
require the target to be whitelisted, so this works on a plain function — and
`write_back_refund` is deliberately **not** whitelisted. See below.

### On the `shopify_integration` side (added once you confirm the hook name)

```python
# hooks.py
refund_payout_dispatchers = ["shopify_integration.utils.refund.write_back_refund"]
```

### On the `payment_portals` side

```python
dispatchers = frappe.get_hooks("refund_payout_dispatchers")
if not dispatchers:
    refuse("No refund dispatcher is registered for a Shopify-backed order.")

# Exactly one is expected. More than one is a misconfiguration, not something
# to resolve by picking: two dispatchers for one payout is two payouts.
if len(dispatchers) > 1:
    refuse(f"{len(dispatchers)} refund dispatchers registered; expected one.")

result = frappe.call(dispatchers[0], refund_name=doc.name)
```

The keyword is **`refund_name`**, matching the signature your own §5 specifies.

### Signature

```python
def write_back_refund(refund_name: str, triggered_by: str = "manual") -> dict
```

`triggered_by` is a free-text label that reaches the log line only; pass
something like `"payment_portals_payout"` so a log reader can tell a dispatched
payout from a button press. It never changes behaviour.

### Authorisation is yours, and this app does not re-check it

`write_back_refund` performs **no permission check**, and is **not whitelisted**,
so it is not reachable over HTTP. Every caller is in-process and trusted, and by
the time you dispatch, you have already authorised the payout against
`PAYOUT_ROLES`.

Version 2 did check submit permission on the Refund Request, and you were right
that it was a real integration risk rather than a theoretical one: our permission
models can diverge, a Refund Approver without doctype submit permission would
pass your gate and fail this one, and the `unknown` that produced meant **neither
app would pay the refund** — with a message about doctype permissions that
explained nothing about why. Two permission models guarding one payout is one too
many, and the one that should win is the one that owns the money path.

The HTTP door is `writeback_now`, which is whitelisted and does require submit
permission on the Refund Request. That is the form button a person presses, and
the conventional Frappe check for acting on a document from its own form. If you
want that button available to a role that lacks doctype submit permission, the
answer is Frappe permissions on Refund Request — not a role list hardcoded in
this app, which would couple it to your configuration.

A test asserts that `write_back_refund` is not whitelisted, because an
accidental decorator there is a payout one HTTP call away from anyone logged in.

### What to pass, and what not to

**The Refund Request name, and nothing else.** Everything else is read here from
the single source of truth:

| needed | read from |
|---|---|
| amount | `Refund Request.net_refund_amount` |
| reason | `Refund Request.reason_note` |
| channel / status / docstatus | `Refund Request` |
| Shopify order id, store | `Refund Request.sales_order` → `Sales Order.shopify_order_id` / `shopify_store` |

Do not pass the amount as the authoritative figure. If the caller passed one and
it disagreed with `net_refund_amount`, there would be no defensible answer to
"which one do we pay", and picking either is how the wrong number leaves the
building. If you want a cross-check rather than a source, see §9.

## 2b. The state this app accepts, and why it is not the booked one

**New in version 4, and it replaces the rule versions 1–3 stated.**

| the Refund Request must have | value |
|---|---|
| `refund_channel` | **`Shopify`** — exactly that, nothing else |
| `status` | **`Approved`** or **`Queued`** |
| `docstatus` | `1` |
| `shopify_refund_gid` | blank |
| `shopify_writeback_status` | not `Unverified` |

### Why `Approved`, and not `Completed`

Because of §0, and it took a contradiction to see it. A successful
`refundCreate` **is the payout**. `Completed` is set in exactly one place in
`payment_portals` — `_record_on_refund`, when the refund's Payment Entry is
posted. So requiring `Completed` meant: book the refund, *then* ask Shopify to
pay it, and the customer is paid after the books already said they were. On a
Cashfree-OCC order both halves of that are true and together they cannot both be
right.

The correct order is the one the Cashfree send path in your own app already
uses:

```
approve  ->  dispatch to Shopify  ->  money moves  ->  THEN book in ERPNext
```

`Queued` is in the set because `send_refund_to_portal` commits `Queued` and then
enqueues the job, so a **dispatched** payout arrives here on `Queued` and never
on `Approved`. `Approved` is in it because that is what a person sees on the
form, and what a refusal returns the document to — `SENDABLE_STATUSES` is
`Approved` alone.

> **One line on your side makes this look wrong, and it is worth naming because
> acting on it would refuse every dispatched payout.** `execute_queued_refund`
> passes `status="Approved" if refund.status == "Queued" else refund.status` to
> its own `sending_allowed` re-check. That is a normalisation **in the argument**
> and it is never written back, so the stored status is still `Queued` when
> `_dispatch_to_storefront` calls in here. Anyone reading that line as evidence
> the document has been promoted to `Approved` would narrow this set to
> `{Approved}` — and then every dispatch refuses `wrong_refund_status` while the
> form button keeps working, which reads as a status bug rather than a missing
> state. Confirmed from that side 2026-09-07; a test here fails if either state
> is dropped.

### Why the channel is an allow-list

`caller_must_pay` is a positive flag because the natural idiom around a negative
one silently did the dangerous thing. The channel gate is an allow-list for the
same reason. The old gate excluded only `Manual Portal Refund`, which left
**`Bank Transfer` accepted** — money already sent by NEFT, written back to
Shopify, and paid a second time through the OCC bridge. #6518's ₹12,999 is that
shape. A channel this app does not recognise — including one a later
`payment_portals` adds — now refuses on its own.

The channel is judged **before** the status, deliberately: a `Bank Transfer`
refund is undispatchable in every status, so a status complaint would name the
wrong problem and send somebody to change a field that would not help.

### What did not change

Ownership is still settled **second**, immediately after the idempotency guard
and before both new refusals. That ordering is what makes the §3 biconditional
hold, and `tests/test_refund_dispatch_gate.py` pins it from this side too: a
non-Shopify order on an undispatchable channel still comes back
`not_a_shopify_order` with `caller_must_pay: true`.

### No idempotency token

Deliberately absent. **The document is the idempotency key.** Once Shopify
accepts a refund, `shopify_refund_gid` is written and committed, and every entry
point treats it as a hard stop. Calling `write_back_refund` twice for the same
Refund Request cannot produce two refunds; the second returns
`refused` / `already_paid` with the original GID. A caller-supplied token would
add a second, weaker key for the same guarantee.

---

## 3. Optional pre-flight, for the UI

There is already a read-only, whitelisted, side-effect-free call that answers
"what will happen if I dispatch this?":

```python
frappe.call("shopify_integration.utils.refund.get_refund_writeback_status",
            refund_name=name)
```

It returns `payout_owner` and `caller_must_pay` (the routing answer), plus
`can_write_back`, `reason`, `reason_code`, `amount`, `status`, `refund_gid`,
`gateway`, `shopify_order_id`, `shopify_store`, `is_shopify` and `error`. Use it
to tell a person, on the form and before they commit, which route their refund
will take — your own design doc asks for exactly that. It sends nothing to
Shopify and is safe to call on every form refresh.

It is **not** a substitute for reading the dispatch result. Between a pre-flight
and a payout, anything can change; only the result dict says what happened.

### And one read for when it refuses

```python
frappe.call("shopify_integration.utils.refund.refund_targets_now",
            refund_name=name)
```

Whitelisted, gated on **Shopify Settings write**, and it **cannot pay anybody**:
it runs the same `RefundTargets` query the payout runs and stops, posts no
mutation, and writes nothing to the Refund Request. Returns every transaction on
the order with `kind`, `status`, `gateway`, `amount`, `refundable`,
`refundable_reported` and `rejected_because`, plus `refundable_total` and the
`would_refuse_with` verdict taken from `plan_refund` itself — so it can never
say "fine" about a refund that would then refuse.

It exists because a refusal used to be unexplainable. `REF-00207` on production
(2026-09-08) refused `no_refundable_transactions` twice and the response was
discarded, so which row failed which test could only be answered from a Shopify
admin login for a store nobody had one for. Deliberately **not** gated on
`enable_refund_writeback`: the diagnosis is needed precisely while the payout is
switched off.

**`refundable_reported` is the field to read on a headroom refusal.**
`maximumRefundableV2` reported as `0.00` and not reported at all are the same
number by the time the filter sees them, and they mean opposite things — a
settled order, versus this app reading a field the API version does not populate,
in which case *no* order is refundable anywhere and every one presents as
already fully refunded. The filter treats both as no-headroom, because refusing
on a number we do not have is the safe direction; this flag is how a person tells
them apart.

### The one distinction that decides the money path

Every result — pre-flight or dispatch — carries `payout_owner`, one of three
values, and `caller_must_pay`, a bool.

| `payout_owner` | `caller_must_pay` | meaning | your move |
|---|---|---|---|
| `caller` | **true** | not a Shopify order | pay via Cashfree as today |
| `shopify` | false | Shopify's payout — possibly already made | never pay it yourself |
| `unknown` | false | could not be determined | never pay it yourself |

**Branch on `caller_must_pay`, and only on that.** It is a *positive* assertion
of the one dangerous action, so anything unknown, unrecognised, or added in a
later version defaults to the safe direction on its own.

The invariant, which `tests/test_refund_contract.py` pins by enumerating the
whole vocabulary:

```
payout_owner == "caller"   <=>   reason_code == "not_a_shopify_order"
```

Exactly one code puts the payout on you. **Every other `refused` code means
"mine, and I cannot do it right now"** — the toggle is off, credentials are
missing, the store is unconfigured, the document is in the wrong state, or it
was refunded in Shopify already. Shopify owns the payout. **Refuse both paths
and surface the reason.** Falling back to Cashfree there is a double-payout
waiting for whoever fixes the cause and retries.

`payout_owner: "unknown"` means this app could not read what deciding it needs:
it is not installed here, or the document does not exist. It is deliberately
*not* `caller` — answering "not mine" without having looked is what version 1 did
wrong.

#### Do not use `owns_payout` as a boolean

It is kept for a caller already reading it, and it is now three-state: `true`,
`false`, or **`null`** when undeterminable. `if not owns_payout` is therefore
**not** a safe test — `null` is falsy, and that branch pays a customer whose
refund may already have been paid. Use `caller_must_pay`.

#### `is_shopify` is a UI flag

It is `true` only when ownership was determined as Shopify's. It no longer goes
`false` merely because a guard returned early — `shopify_order_id` and
`shopify_store` are now populated on every path that can see them — but
`payout_owner` is the field to route on.

## 4. The result dict

What `write_back_refund` returns, on every path. Extra keys may be added without
a version bump; none will be removed or change meaning without one.

```python
{
  "provider":         "shopify",       # which app answered
  "contract_version": 4,

  "outcome":     "paid" | "refused" | "failed_unsent" | "failed_unknown" | "in_progress",
  "reason_code": "<stable slug, see §6>",
  "retry_safe":     bool,   # true for exactly one outcome: failed_unsent
  "possibly_paid":  bool,   # true for paid AND failed_unknown

  "payout_owner":    "shopify" | "caller" | "unknown",   # see §3
  "caller_must_pay": bool,   # THE routing flag; true only for not_a_shopify_order
  "owns_payout":     True | False | None,                # diagnostic; see §3

  # RESERVED, not emitted yet: present and true only when an expected_amount
  # you sent was actually compared. Absent means not compared. See §9.2.
  # "expected_amount_checked": True,

  "message":     "<human sentence, safe to show a user>",
  "amount":      12999.0,   # net_refund_amount as understood here
  "refund_gid":  "gid://shopify/Refund/123",   # set only when outcome == "paid"
  "gateway":     "manual",  # what Shopify attached the refund to

  "status":         "Done",        # the Refund Request write-back field value
  "refund_request": "REF-0007",
}
```

`get_refund_writeback_status` (§3) returns the same three routing keys plus
`is_shopify`, `can_write_back`, `reason`, `reason_code`, `amount`,
`shopify_order_id`, `shopify_store`, `status`, `refund_gid`, `gateway`,
`written_back_at` and `error`. It never returns an `outcome`, because nothing
has happened yet.

### `retry_safe` and `possibly_paid` are redundant on purpose

Both are derivable from `outcome`. They are stated anyway because this is the one
axis where a caller getting the mapping wrong pays a customer twice, and a
boolean is harder to get wrong than a string comparison against a set the caller
has to remember. Read either; do not compute your own.

---

## 5. The four states you asked about

### `paid` — money is moving

Shopify accepted the mutation and returned a refund with an id. `refund_gid` is
set and already committed here.

`payment_portals` may mark its refund as sent. **Do not also call Cashfree.**
Note that the Cashfree refund arrives through the OCC bridge and lands in
Settlement Recon at a median lag of about 48 hours — so its absence proves
nothing for a day or two, and must not be read as a failed payout.

### `refused` — nobody was paid, and retrying changes nothing

A precondition failed. Nothing was sent; no Shopify call was made at all in most
cases. `retry_safe` is false, because a bare retry is pointless, not because it
is dangerous.

Two sub-cases the caller must distinguish by `reason_code`:

- `already_paid` — we have a GID for this refund. It is done. `refund_gid` is
  returned. Treat as `paid`, not as an error.
- everything else — configuration or data. **This refund is still unpaid and
  Shopify owns it.** Do not fall back to Cashfree; surface `message` and let a
  person fix the cause.

### `failed_unsent` — nobody was paid, safe to retry

Either nothing left this process, or Shopify read the request and explicitly
declined it. `retry_safe: true`. This is the **only** outcome a caller may retry
automatically.

It covers: the `RefundTargets` query failing, the order not being found, no
refundable headroom, `userErrors` from `refundCreate`, and an auth rejection
(401/403 — declined before the document ran).

Retrying is *safe*, which is not the same as *useful*: `insufficient_refundable`
will fail identically until something changes in Shopify. Back off rather than
loop.

### `failed_unknown` — POSSIBLY PAID, never retry

The mutation was posted and its fate cannot be established. `possibly_paid: true`,
`retry_safe: false`.

It covers:

- transport failure, timeout, or HTTP 5xx after the mutation went out — note that
  `execute()` retries internally, so a lost response on *any* attempt may be
  hiding a refund that succeeded;
- HTTP 200 with empty `userErrors` and no `refund` object — Shopify answered
  without complaining and did not say what happened, so "nothing happened" is an
  assumption, not a fact;
- a response whose `refundCreate` payload is missing or unintelligible;
- any unexpected exception raised after the mutation was posted;
- **a worker that never came back at all** — killed mid-request by a container
  restart, an OOM or an eviction. The document is moved to `Unverified` and
  committed *before* the mutation is posted, precisely so this case leaves the
  correct answer behind rather than a retryable one. You will not see a result
  dict for it; you will find the state on the document.

**What `payment_portals` must do:** treat the refund as *possibly paid*. Do not
retry. Do not fall back to Cashfree — that is the double-payout. Map it to
whatever your money-moving guards use for "unreconciled", and surface it for a
person. The refund is not payable again by any route until somebody has looked.

On this side it lands on `shopify_writeback_status = "Unverified"`, which no
trigger picks up and which the form refuses to offer a retry button for. It is
cleared only by a person, through
`resolve_unverified_writeback(refund_name, resolution, shopify_refund_gid=…)`
with `resolution` of `"paid"` (a refund was found in Shopify; the GID is
mandatory) or `"not_paid"` (none exists; cleared so it can be sent again). Who
resolved it and which way is recorded on the document.

### `in_progress` — not an answer yet

Another worker holds the claim. Nothing was sent by *this* call; the other call
may be paying right now. Do not retry and do not treat as failure — re-read the
document, or run the section 3 pre-flight again later. A stale claim (>30 min) is
taken over automatically.

### Summary table

| `outcome` | paid? | `retry_safe` | `possibly_paid` | caller's move |
|---|---|---|---|---|
| `paid` | yes | false | true | mark sent; never call Cashfree |
| `refused` | no | false | false | fix the cause; do not fall back |
| `failed_unsent` | no | **true** | false | safe to retry, with backoff |
| `failed_unknown` | **unknown** | false | **true** | reconcile by hand; never retry |
| `in_progress` | unknown | false | false | wait and re-read |

Any `outcome` value you do not recognise — including one added by a later
contract version — **must be treated as `failed_unknown`**. That is the only safe
default, and it is why the vocabulary is closed and versioned.

---

## 6. `reason_code` vocabulary

Stable slugs, safe to branch on. `message` is for humans and may be reworded at
any time.

**With `outcome: "refused"`**

| `reason_code` | meaning |
|---|---|
| `already_paid` | `shopify_refund_gid` is set; `refund_gid` returned |
| `not_a_shopify_order` | no Sales Order, or no `shopify_order_id` on it. **The only code with `caller_must_pay: true`** — see section 3 |
| `channel_is_manual_portal_refund` | refunded in Shopify already |
| `channel_does_not_dispatch` | `refund_channel` is not `Shopify` (and not `Manual Portal Refund`, which has its own code). That channel pays the customer by another route — Cashfree's own API, or a bank transfer — so a Shopify refund on top pays them twice. **New in version 4** |
| `wrong_refund_status` | Refund Request status is not `Approved` or `Queued`. **Inverted in version 4** — it used to mean "not `Completed`" |
| `already_booked` | status is `Completed` **and** a `payment_entry` is set: ERPNext has booked this refund and no Shopify refund is recorded against it, so either a payout would land after its own record or something settled it outside this flow. Needs a person, not a corrected field. **New in version 4** |
| `not_submitted` | docstatus is not 1 |
| `nothing_to_refund` | `net_refund_amount <= 0` |
| `writeback_unavailable_for_store` | no enabled Shopify Settings with the toggle on |
| `no_api_credentials` | store has no Admin API credentials |
| `not_installed` | write-back custom fields absent; run `bench migrate` |
| `refund_request_missing` | no such document |
| `amount_mismatch` | `expected_amount` disagreed with `net_refund_amount` — **Reserved — not implemented yet, see section 9.2.** Every other code in these tables is emitted today |

**With `outcome: "failed_unsent"`**

| `reason_code` | meaning |
|---|---|
| `query_failed` | the `RefundTargets` query failed; nothing was sent |
| `shopify_order_not_found` | order missing or invisible to the token |
| `insufficient_refundable` | Shopify's headroom is short of the amount; refused outright rather than partially refunded |
| `no_refundable_transactions` | no SALE/CAPTURE parent to attach to |
| `rejected_by_shopify` | `userErrors` — read and declined |
| `not_authorised` | 401/403; declined before the document ran |
| `setup_failed` | unexpected error before anything was sent |

**With `outcome: "failed_unknown"`**

| `reason_code` | meaning |
|---|---|
| `transport_error_after_send` | mutation posted, no usable response |
| `response_unverifiable` | response gave no refund object, or was unintelligible |
| `unverified_previous_attempt` | an earlier attempt is still unresolved |

**With `outcome: "in_progress"`**: `claimed_elsewhere`.

### Ownership per code

`caller_must_pay` is true for `not_a_shopify_order` and nothing else — the two
codes version 4 adds are both `shopify`. That is worth one sentence, because
`channel_does_not_dispatch` can read like "somebody else's": it means the refund
is Shopify's order but is paid by another route, *not* that you should pay it
through Cashfree. Paying it there is the double refund.

`payout_owner` is `unknown` for exactly two codes — `not_installed` and
`refund_request_missing` — the refusals that happen before this app can read
enough to decide. Every other code above is `shopify`, including all the
`failed_unsent` and `failed_unknown` ones: a refund that failed to send is still
Shopify's to pay.

---

## 7. Synchronous, and why not enqueued

**`write_back_refund` is synchronous.** It returns only after Shopify has
answered, or after it is known that Shopify's answer cannot be had.

Your own constraint decides this: *a payout that returns before it has happened
cannot be reported as sent.* An enqueued call could only return "queued", which
is `failed_unknown` wearing a friendlier label — the caller would have to poll
the document to find out whether a customer was paid, and every poll before the
job runs looks like a refund that has not happened.

Practically: two GraphQL calls, typically 1–3 seconds, bounded by
`shopify_api._TIMEOUT` per request with internal retries. `fulfil_now` already
runs this shape inside a web request. Concurrency is safe without a queue —
the row-locked claim is committed before any HTTP, so a second caller gets
`in_progress` rather than a second refund.

**If `payment_portals` enqueues its own job that calls this**, that is fine, but
the job's own failure — killed worker, timeout, lost result — is
`failed_unknown` and not `failed_unsent`. A job that vanished after calling
`write_back_refund` may have paid the customer. Treat a missing result as
possibly-paid, and read `shopify_writeback_status` on the document to find out.

---

## 8. What this side will never do

So the other side can rely on it rather than defend against it:

- never write to Refund Request fields other than its own five
  (`shopify_refund_gid`, `shopify_writeback_status`, `shopify_refund_gateway`,
  `shopify_writeback_at`, `shopify_writeback_error`);
- never change the Refund Request's `status`, `docstatus`, or any money field;
- never call the Cashfree API, or any gateway directly;
- never send `refundLineItems`, so Shopify never restocks — ERPNext is the
  inventory master;
- never notify the customer unless the store's `notify_customer_on_refund` is
  on (default off);
- never send a partial refund when Shopify's headroom is short — it refuses
  instead, because a partial refund looks settled and is not;
- never raise from `write_back_refund`; every outcome is a result dict;
- never second-guess your authorisation: `write_back_refund` has no permission
  check and is not whitelisted, so a caller you have authorised is never refused
  here on permission grounds.

---

## 9. Open, and blocking

1. ~~**Confirm the hook name `refund_payout_dispatchers`.**~~ **Confirmed and
   registered, version 4.** One line in `hooks.py`, and a test refuses to let a
   second entry land beside it. Nothing else on this side changed for it.
2. **`expected_amount` is not implemented yet, and must not ship without its
   acknowledgement.** Raised from the `payment_portals` side, and correctly: as
   originally specified it was unsafe. Sending it to a version that does not
   implement it gets it dropped by `frappe.call` (§1) with no error, so the
   caller would believe a cross-check had run when nothing compared anything —
   a guard that silently is not there, which is the exact failure this document
   exists to eliminate.

   **The specification, so it can be built mechanically and only in one piece:**

   - Signature becomes
     `write_back_refund(refund_name, triggered_by="manual", expected_amount=None)`.
   - When `expected_amount` is `None` or absent: behave exactly as now, and do
     **not** emit the acknowledgement.
   - When it is supplied: compare it to `net_refund_amount` **in minor units**
     (`_paise`, the same integer basis the allocation uses — a float compare
     would fail on 46952.16). On any difference, refuse before sending:
     `outcome: "refused"`, `reason_code: "amount_mismatch"`, both figures in
     `message`, nothing sent, `payout_owner` unchanged.
   - On a match, and **only** on a path that actually performed that comparison,
     emit `expected_amount_checked: true` in the result. It is never emitted
     otherwise — absent and `false` both mean "not checked", and a caller must
     not have to tell those apart.
   - It never selects the amount to pay. `net_refund_amount` remains the single
     source of truth; `expected_amount` can only ever cause a refusal.

   **Your side's rule:** if you sent `expected_amount` and the result does not
   carry `expected_amount_checked: true`, do not treat the figure as validated.
   The case to handle deliberately is `outcome: "paid"` **without** the
   acknowledgement — that is money already moved, at `net_refund_amount`, with
   your expectation never compared. It is not a retry (retrying pays twice) and
   not a failure; it is a reconciliation item, and it can only arise from a
   version mismatch, which is worth alerting on rather than absorbing.

   `amount_mismatch` and `expected_amount_checked` are both listed as reserved
   and neither is emitted today. Everything else in §6 is. Say the word and I
   will build the pair together — a test already refuses to let the parameter
   land without the acknowledgement.
3. ~~**Guard against two dispatchers.**~~ **Done on both sides.**
   `actions/refund_dispatch._dispatcher` refuses on a list longer than one
   rather than picking, and on this side a test parses `hooks.py` and asserts the
   list has exactly one entry — so a duplicate is caught at test time rather than
   at payout time.
4. `order.transactions` shape still needs one live response (see
   `REFUND-WRITEBACK-BRIEF.md` §4). Does not block this interface.
5. **The one assumption the whole feature rests on is still unproven: nobody
   has confirmed the Cashfree-OCC bridge fires for an API-created refund.** It is
   proven for a refund made *by hand* in the Shopify admin — `#6491` marked
   refunded 31 Aug 5:02 pm, Cashfree refund `144073385` for the same ₹46,952.16
   at 17:02:59 the same day. Whether the bridge reacts the same way to
   `refundCreate` over the Admin API is assumed, and the failure direction is the
   worst one available: this app would report `paid`, `payment_portals` would
   book it, and no money would have moved. Prove it on one low-value real order
   before this is enabled on anything else, and note that
   `REFUND-WRITEBACK-BRIEF.md` §10 step 2 **cannot** prove it — the headroom
   guard refuses before the mutation, so the safe probe never posts one.

## 10. Changes on this side that this document reflects

### Version 4 — dispatch at `Approved`, and an allow-list on the channel (2026-09-07)

Asked for from the `payment_portals` side after it built the whole calling half
in `3bc6164`, and it is the right call.

**The contradiction.** §0 has said since version 1 that a successful
`refundCreate` pays the customer. The gate said Shopify is told "once ERPNext has
booked and paid the refund, not before". Both sentences were in this document at
the same time and they cannot both hold: `Completed` means the Payment Entry is
posted, so the sequence was book → call → *then* the money moves. Nobody had put
the two next to each other, and the toggle being `0` everywhere is what kept it
theoretical.

**The fix** is to dispatch from `Approved`/`Queued` on the new `Shopify` channel
— which is symmetry with the Cashfree send path rather than a new idea — and to
make the channel test an allow-list. See §2b.

**A hazard closed on the way.** The old gate's only channel exclusion was
`Manual Portal Refund`, so a `Bank Transfer` refund on a Shopify order passed
both tests and the form button would have written it back. That is a NEFT refund
paid a second time by the OCC bridge. It was never reachable in practice —
`enable_refund_writeback` has been `0` on every store — but it was one checkbox
away, and it is the reason the gate is positive now rather than one exclusion
longer.

**`already_booked` is separate from `wrong_refund_status` on purpose.** A
`Completed` row with a Payment Entry and no GID is not a status to correct; it is
a refund something settled outside this flow, and the message has to send a
person to find out what rather than to change a field.

**The dispatcher is registered.** One line in `hooks.py`, and a test refuses to
let a second entry land there — `frappe.get_hooks` returns a list, and two
dispatchers for one payout is two payouts. Registering it did **not** whitelist
`write_back_refund`; a test asserts that too.

### Post-review fixes (2026-09-04), no version bump

A code review of the whole feature found one defect that mattered to this
contract and several that did not reach it.

**The one that did:** `Unverified` was only ever reached from a *caught*
exception, so a worker killed during `refundCreate` left the row at `Pending` —
indistinguishable from one that never posted. After the 30-minute stale-claim
window the refund was re-sent, and since a partial refund leaves the order
enough headroom to take another, Shopify paid the customer twice. The
`sent` flag that classified the failure was a local variable and died with the
worker. Fixed by committing `Unverified` **before** posting the mutation: the
risky state is now entered before the risk, a clean rejection (`userErrors`,
401/403) moves the row back to `Failed`, and a worker that never returns leaves
the answer a person has to resolve rather than one a retry will act on.

**`no_refundable_transactions` was unreachable**, and an order whose every
transaction is a refund, a void or fully refunded was reported as
`insufficient_refundable`. Both codes now mean what §6 always said they meant —
this makes the code match the published contract rather than changing it, which
is why the version is unchanged.

Also, and not caller-visible: the possibly-paid warning now leads its message
instead of trailing it, because the field it is stored in keeps only the first
1000 characters and a verbose GraphQL error could push the words "do NOT retry"
off the end; `resolve_unverified_writeback` and `writeback_now` now check
availability before permission, as `write_back_refund` already did; and the
Shopify Settings description no longer tells an admin the write-back is enqueued
and fires when a Refund Request reaches Completed, neither of which has been true
since the dispatcher was deferred.

### Version 3 — authorisation belongs to the caller (2026-09-04)

Raised from the `payment_portals` side as an integration risk, and it was a real
one. Version 2's `no_permission` → `unknown` mapping was safe in isolation and
broken in combination: your payout gate requires `Refund Approver` or
`System Manager`; this app required submit permission on Refund Request. A user
with the former and not the latter passes your gate, gets refused here, and
`unknown` means refuse-both-paths — a refund neither app will pay, explained by a
message about the wrong permission model.

The fix is not to align the two checks but to remove one. By the time a
dispatcher reaches `write_back_refund`, the payout is already authorised;
re-deciding it here adds no safety and one failure mode. So the check is gone,
`no_permission` is gone from the vocabulary, and — the part that makes this safe
rather than merely convenient — `write_back_refund` is no longer whitelisted, so
it is not reachable over HTTP at all. The whitelisted door is `writeback_now`,
which still checks.

That leaves `unknown` meaning exactly two things, both of them "this app cannot
answer": not installed, and no such document.

### Version 2 — the routing fix (2026-09-04, after the `electrobotictest` deploy)

Three live calls returned the same `owns_payout` with opposite meanings:
`not_a_shopify_order`, and two `channel_is_manual_portal_refund` — the latter
being refunds Shopify had already paid. All three said `owns_payout: false`.

Cause: ownership was derived from `shopify_order_id`, but the channel, status,
docstatus and amount guards all returned *before* the Sales Order was looked up.
A blank order id there meant "never looked", and was reported as "not a Shopify
order". The same defect made `is_shopify` false for `NG-SO2627-1022` and
`NG-SO2627-2160`, which are both Shopify orders.

Fixed by settling ownership **before any guard that can return**, which required
reordering: the ownership check now runs second, immediately after the
idempotency guard. Guard order is load-bearing for the invariant — leave the
ownership test after `nothing_to_refund` and a zero-amount non-Shopify refund
comes back `caller`-owned with the wrong `reason_code`, breaking the
biconditional again.

Also: a stored `shopify_refund_gid` now settles ownership on its own, so a refund
Shopify demonstrably paid stays Shopify's even if its Sales Order was later
amended and lost the order id.

`caller_must_pay` was added because the underlying mistake was shape, not just
data: a bool that must never be read with `not` is a trap, and the natural
idiom — `if not owns_payout: pay()` — silently did the wrong thing for every
undeterminable case. A positive flag for the dangerous action fails safe.

### Version 1

Written while specifying §5, because the contract would otherwise have promised
something the code could not do:

- `failed_unknown` is now distinguishable. Previously a transport error after the
  mutation was recorded as plain `Failed`, and the form offered a retry — which
  could have paid a customer twice. It now lands on the new
  `shopify_writeback_status = "Unverified"`, which nothing retries.
- `resolve_unverified_writeback` added, so `Unverified` has an exit and is not a
  silent dead end.
- `outcome` / `reason_code` / `retry_safe` / `possibly_paid` added to
  `write_back_refund`'s result. Existing keys are unchanged.
- `gateway_moves_money` and `plan_refund`'s `moves_money` **deleted**. Nothing
  consumed them, and a boolean derived from a gateway name carried no information
  that `shopify_refund_gateway` does not already hold — while inviting exactly
  the misreading that produced the wrong first draft of the brief's §1.
