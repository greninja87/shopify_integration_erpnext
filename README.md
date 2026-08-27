# Shopify Integration for ERPNext

Automatically creates **Customers**, **Sales Orders**, **Payment Entries**, **Delivery Notes → Sales Invoices** in ERPNext from Shopify webhooks. Supports **India Compliance (GST)**, partial payments, and multi-store configurations.

## Features

- **Webhook-driven** — Real-time order sync via Shopify `orders/create` webhooks
- **India GST compliant** — Tax-inclusive prices back-calculated to tax-exclusive rates using ERPNext Item Tax Templates
- **GSTIN support** — Automatic B2B customer creation with GST-registered billing addresses via India Compliance portal
- **Payment Entry** — Automatic PE creation with gateway mapping (Cashfree, Razorpay, COD, etc.)
- **Gateway settlement reconciliation** — The payment gateway's own transaction id (PayU `txnid`, Razorpay payment id, …) captured onto the Payment Entry
- **Sales Invoice** — Auto-generated after Payment Entry or after Delivery Note (configurable)
- **Order fulfillment** — Submitting a Delivery Note marks the Shopify order fulfilled (immediately, after a delay, or manually), with tracking details and customer shipping emails
- **FIFO Advance Allocation** — Payment Entries automatically allocated to Sales Invoices
- **Multi-store** — Multiple Shopify stores on a single ERPNext instance
- **Retry-safe** — Failed webhooks stored in Shopify Log with one-click retry
- **Idempotent** — Duplicate webhooks silently skipped (no duplicate Sales Orders)

---

## Requirements

| Component | Version |
|-----------|---------|
| ERPNext   | v15 or v16 |
| Frappe    | v15 or v16 |
| India Compliance | Optional (recommended for GST) |

---

## Installation

```bash
cd /home/frappe/frappe-bench
bench get-app https://github.com/greninja87/shopify_integration_erpnext.git
bench --site your-site.local install-app shopify_integration
bench --site your-site.local migrate
bench restart
```

---

## Configuration

### 1. Shopify Settings

Go to **ERPNext → Shopify Integration → Shopify Settings** and create a record.

| Field | Description |
|-------|-------------|
| Store Name | Unique identifier (e.g. `mystore`) |
| Shop Domain | Your `.myshopify.com` domain (e.g. `mystore.myshopify.com`) |
| Webhook Secret | Copy from Shopify Admin → Settings → Notifications |
| Company | ERPNext Company to create orders under |
| Warehouse | Default warehouse for SO items |
| Enable Sync | Master on/off switch |
| Admin API Access Token | *Optional.* Legacy custom apps only — see Gateway Payment Reference below |
| Client ID / Client Secret | *Optional.* Dev Dashboard apps use these instead of a token |

### 2. Register the Webhook in Shopify

Go to **Shopify Admin → Settings → Notifications → Webhooks** and add:

- **Event:** Order creation
- **Format:** JSON
- **URL:** `https://your-erpnext-domain/api/method/shopify_integration.api.shopify_webhook`

Copy the webhook signing secret and paste it into **Webhook Secret** in Shopify Settings.

### 3. Payment Entry (Optional)

Enable **Enable Payment Entry Creation** and configure:

- **Default Bank/Cash Account** — fallback bank account for all gateways
- **Gateway Mapping** — map specific payment gateways to specific bank accounts
  - Use **Tag Contains** for Cashfree/Razorpay partial-COD orders
  - Use **Shopify Gateway** for exact gateway name matching
- **Auto Submit** — automatically submit Payment Entries

### 4. Gateway Payment Reference (Optional)

Shopify records the gateway's transaction id on the order's **transaction** record, not on
the order. It is absent from the `orders/create` and `orders/paid` webhook payloads — no
`transactions` array, `reference` and `source_identifier` are `null`, `note_attributes` is
empty. So it has to be pulled from the Admin API, which needs credentials.

**The app needs the `read_orders` scope** — and only that. It covers Shopify's
`OrderTransaction` object, which is what this reads. No other scope is required.

Which credentials you use depends on how your Shopify app was created.

#### A — Dev Dashboard app (anything created now)

Legacy custom apps could no longer be created after **1 January 2026**, so a new app is
built in the Dev Dashboard and issues **no static token** — there is nothing to copy out
of the UI.

1. https://dev.shopify.com/dashboard → your app → **App settings → Credentials**
2. Copy the **Client ID** and **Client Secret**
3. ERPNext → Shopify Settings → **Connection → Shopify Admin API** → paste both

The app exchanges them for an access token via the **client credentials grant**, caches it,
and re-mints it before it expires. Tokens live 24 hours; you never touch them again.

> **Same-organization only.** The client credentials grant can only reach a store in the
> *same Shopify organization* as the app. A store under a separate Shopify account needs
> its **own app** in its own organization, and its own Shopify Settings record. A
> cross-organization token cannot be obtained this way.

#### B — Legacy custom app (created in the Shopify admin before 1 Jan 2026)

1. Shopify Admin → **Settings → Apps and sales channels → Apps** → your app →
   **API credentials**
2. Copy the **Admin API access token** (`shpat_…`) — Shopify reveals it **once**
3. Paste it into **Admin API Access Token**

Non-expiring until the app is uninstalled. If both this and a Client ID/Secret pair are
filled in, the static token wins.

Either way, leave **REST API Version** blank unless you need to pin something other than
the app default (`2026-01`).

Once credentials are set, every new order's Payment Entry gets
**Gateway Payment Reference** (`custom_gateway_reference`) filled in from the order's
transactions, plus **Payment Gateway** (`custom_gateway_name`) identifying the portal —
e.g. `Cards, UPI, NB by PayU India`. Leave the credentials blank and the lookups simply
don't run.

**Half-filled credentials are refused at save time.** A Client ID with no Secret (or the
reverse) cannot mint a token, and every lookup would skip silently — leaving a blank
reference with nothing in the Error Log to explain it. Shopify Settings blocks that
instead.

**What it does not touch:** `reference_no` still holds the Shopify order name (`#6282`) and
is never modified by this feature. Writes use `update_modified=False`, so filling the field
leaves `modified` alone and creates no Version rows.

**Which transaction is used:** `kind` in (`sale`, `capture`) **and** `status == "success"`;
earliest by `created_at` when several match. The reference is read from `authorization`,
falling back to `receipt.txnid`, then `receipt.payment_id`. If all are empty the field stays
**blank** and the miss is logged — a placeholder is never written.

**Backfilling historical orders**

Populates existing Shopify-created Payment Entries, oldest first. Safe to re-run: entries
that already carry a reference are skipped, so nothing is duplicated or overwritten.

Dry run first to see the scope, writing nothing:

```bash
bench --site <site> execute shopify_integration.utils.gateway_reference.backfill_gateway_references --kwargs "{'limit': 500, 'dry_run': 1}"
```

Then for real:

```bash
bench --site <site> execute shopify_integration.utils.gateway_reference.backfill_gateway_references --kwargs "{'limit': 500}"
```

Optionally restrict to one store with `'store': 'mystore.myshopify.com'`. Requests are paced
to Shopify's 2 req/sec REST limit and retried on HTTP 429, so a few hundred entries take a
few minutes

> **The 60-day ceiling.** `read_orders` only reaches orders from roughly the last 60 days.
> The backfill walks *oldest first*, so on a long history it will fail on exactly the orders
> you most want. Nothing breaks — each miss logs and the field stays blank — but you will
> not get the older references without the **`read_all_orders`** scope, which requires a
> written request to Shopify and their approval. Run with `dry_run: 1` first to see how many
> entries are pending before spending the calls — from the UI, enqueue it as a background job instead:

```bash
bench --site <site> execute shopify_integration.utils.gateway_reference.enqueue_backfill --kwargs "{'limit': 500}"
```

Returns `{"scanned", "updated", "no_reference", "failed", "entries"}`.

### 5. Sales Invoice (Optional)

Enable **Enable Sales Invoice Creation** and choose a trigger:

- **After Payment Entry** — SI created immediately after PE in the same webhook
- **After Delivery Note** — SI created by hourly scheduler after DN is submitted

Both paths use **Allocate Advances Automatically (FIFO)** to link Payment Entries.

---

---

### 6. Order Fulfillment (Optional — ships disabled)

Marks the Shopify order **fulfilled** when a Delivery Note is submitted, which is
also what triggers your store's shipping-confirmation email to the customer.

> **This feature is off by default and does nothing until you enable it.**
> `Enable Order Fulfillment` is unticked, so no hook fires, the hourly job returns
> immediately, and no button appears on the Delivery Note. Nothing reaches Shopify.
>
> The one thing that *does* happen while it is off: `custom_shopify_line_item_id`
> is populated on Sales Order Items as orders sync. That value can only be captured
> at sync time, so recording it now means fulfillment matches lines exactly if you
> ever switch the feature on — orders synced without it can only fall back to SKU
> matching, which is wrong when one order carries the same SKU twice.
>
> If you fulfil orders elsewhere, note the trade-off: Shopify orders stay
> **Unfulfilled** permanently, so Shopify admin's order views, the customer's order
> status page, and any Shopify shipping/3PL app will not reflect what shipped.

**Prerequisite — token scopes.** Fulfillment is Admin-API-only; no webhook can do it.
The token from step 4 needs more than `read_orders`:

- `write_merchant_managed_fulfillment_orders`
- `write_third_party_fulfillment_orders` — only if some orders route to a 3PL
- the **`fulfill_and_ship_orders`** permission

A `read_orders`-only token fails with HTTP 403, and Shopify Settings refuses to save
with fulfillment enabled and no token at all.

**Timing** — Shopify Settings → Fulfillment → *Fulfil Shopify Order*:

| Mode | Behaviour |
|------|-----------|
| **Manual** | Nothing automatic. Only the **Fulfil in Shopify** button on the Delivery Note, and the list-view bulk action. |
| **Immediate** | Fires as soon as the Delivery Note is submitted (background job). |
| **Scheduled** | The hourly job fulfils Delivery Notes submitted more than *Delay After Submission* hours ago. |

**Scheduled is the safe default for a reason.** The delay is a window in which a
Delivery Note submitted in error can be cancelled before Shopify is ever told — so the
customer never receives a shipping email for a shipment that did not happen. With
**Immediate** there is no such window, and saving that combination with customer
emails on raises a warning.

**Tracking details** — all optional. Enter the *fieldname* of the Delivery Note field
holding each value (e.g. `lr_no`, or your own `custom_tracking_id`); the app reads them
at fulfillment time. Shopify builds the tracking URL itself only when the courier name
matches its supported-carrier list **exactly, capitalisation included** (`Delhivery`,
`DTDC`, `India Post`). Otherwise supply a Tracking URL field, or the customer sees an
unclickable number.

**Customer emails** — *Email Shipping Confirmation to Customer* passes `notifyCustomer`
to Shopify. On by default, because fulfilling the order is normally the point at which
you want the customer told. **Turn it off while testing** — it reaches real customers.

**Fulfilling manually**

- **One Delivery Note:** open it → **Shopify → Fulfil in Shopify**. Confirms first, and
  tells you whether a customer email will go out. Runs inline, so you get the real
  result rather than "queued".
- **In bulk:** Delivery Note list → tick rows → **Actions → Fulfil in Shopify**. Already-
  fulfilled, draft, return and non-Shopify rows are filtered out and reported, not
  silently dropped. Runs in the background, paced to Shopify's rate limit.

**Never fulfilled twice.** `custom_shopify_fulfillment_id` is the idempotency key: once
set, no path sends another request. Because four things can trigger a fulfillment
(on_submit, scheduler, button, bulk) and they can race, the claim is a compare-and-swap
committed before any API call — two workers cannot both win. A claim abandoned by a
killed worker is retried after 30 minutes.

**Cancelling a fulfilled Delivery Note** — Shopify Settings → Fulfillment →
*When a Fulfilled Delivery Note is Cancelled*:

- **Do Nothing** — Shopify keeps showing the order fulfilled; the divergence is written
  to the Error Log so it is at least visible.
- **Cancel Fulfillment in Shopify** — calls `fulfillmentCancel`. Shopify may refuse for
  an already-shipped, already-notified fulfillment; if it does you are alerted rather
  than left with the two systems quietly disagreeing.

Either way the Delivery Note cancellation itself always succeeds — the Shopify call runs
after commit and can never block a stock document.

**Third-party fulfillment services.** Orders assigned to a 3PL only accept a fulfillment
*request* (`fulfillmentOrderSubmitFulfillmentRequest`), not a direct fulfillment. The app
detects this from `supportedActions`, refuses with a clear message, and leaves the order
for Shopify admin — it does not guess.

---

## How It Works

```
Shopify Order → Webhook POST → ERPNext API
  ↓
  1. HMAC signature verified
  2. Shopify Log created (audit trail)
  3. Customer found or created (GSTIN → phone → email → create)
  4. Sales Order created with tax-exclusive rates
     - GST stripped from Shopify tax-inclusive prices
     - India Compliance resolves intra/inter-state tax templates
     - Paisa-level rounding reconciled to match Shopify total exactly
  5. Payment Entry created (if enabled)
     - Gateway-specific bank account mapping
     - Partial payments supported (Cashfree partial-COD)
     - Gateway transaction id fetched from the order's transactions
       and stored in custom_gateway_reference (if an Admin API token is set;
       any failure here is logged and never blocks the order)
  6. Sales Invoice created (if enabled, trigger-dependent)
     - FIFO advance allocation links PE → SI automatically
  ↓
  Shopify Log updated → "Processed"
```

Later, when goods ship:

```
Delivery Note submitted in ERPNext
  ↓
  Immediate  → fulfilled in Shopify now
  Scheduled  → fulfilled by the hourly job after the delay window
  Manual     → fulfilled when you click the button
  ↓
  order.fulfillmentOrders queried → open lines matched to DN lines
  fulfillmentCreate → Shopify marks the order Fulfilled
  ↓
  custom_shopify_fulfillment_id stored → never sent again
  (notifyCustomer on → Shopify emails the customer their tracking)
```

---

## Custom Fields Added

**Customer:**
- `shopify_customer_id` — Shopify customer ID for deduplication
- `shopify_phone` — Phone used as primary matching key
- `shopify_email` — Email for secondary matching

**Sales Order:**
- `shopify_order_id` — Shopify order ID (prevents duplicate SOs)
- `shopify_store` — Store domain for this order

**Delivery Note:**
- `shopify_order_id` — Inherited from Sales Order
- `shopify_store` — Store domain

**Payment Entry:**
- `custom_gateway_reference` — The payment gateway's own transaction id, for reconciling
  gateway settlements against orders. Read-only; blank when the gateway returned nothing.
- `custom_gateway_name` — Gateway reported by Shopify, e.g. `Cards, UPI, NB by PayU India`

Standard `reference_no` continues to hold the Shopify order name and is untouched.

**Sales Order Item:**
- `custom_shopify_line_item_id` — Shopify `line_item.id` for the row (hidden). Lets
  fulfillment target the right line when one order carries the same SKU twice.

**Delivery Note (fulfillment state):**
- `custom_shopify_fulfillment_status` — Pending / Fulfilled / Partially Fulfilled /
  Failed / Cancelled / Not Applicable
- `custom_shopify_fulfillment_id` — Shopify Fulfillment GID. **The idempotency key:**
  while set, no further fulfillment request is ever sent for this Delivery Note.
- `custom_shopify_fulfilled_at` — when Shopify accepted it; on a Pending/Failed row, the
  last attempt time
- `custom_shopify_fulfillment_error` — why the last attempt failed, or what a partial
  fulfillment left unsent
---

## Retry Failed Orders

If a webhook fails (e.g. missing SKU, payment gateway not mapped), go to:
**ERPNext → Shopify Log → [failed log entry] → Retry Order**

The same payload is replayed through the full SO creation pipeline.

### When the payload itself is wrong

Some orders fail because of the data Shopify sent, not because of ERPNext config
— most often a wrong or missing pincode, which India Compliance rejects since it
cannot reconcile the PIN against the GST state. Retrying replays the same bad
data and fails identically, so the payload has to be corrected first.

The `Payload` field is read-only and is **never** modified — it is the record of
what Shopify actually sent, and Frappe will not let Customize Form unset
read-only on it. Corrections go into a separate `Corrected Payload` field, which
Retry Order prefers when present. A corrected log therefore shows both versions,
plus who changed it and why, and the form displays an orange banner so nobody
mistakes it for verbatim Shopify data.

Two ways to correct, under **Shopify Log → Fix Payload**:

| Action | When to use |
|--------|-------------|
| **Re-fetch from Shopify** | Preferred for address problems. Fix the order in Shopify first — that is where the customer's own record lives, so the correction also applies to their next order — then re-fetch and retry. Needs an Admin API access token. |
| **Edit Payload** | Manual JSON edit in a dialog, for cases Shopify itself cannot hold. **Save & Retry** does both in one step. |
| **Discard Correction** | Go back to replaying exactly what Shopify sent. |

Guards on a correction:

- Invalid JSON, a JSON array, or a scalar is refused — nothing is written
- The `id` field must be present and must still match this log's Shopify Order
  ID. A correction cannot move a log onto a different order, and cannot drop the
  id (which is what links the Sales Order back to Shopify for duplicate
  detection)
- Requires **write** permission on Shopify Log, not just read

**One address gotcha.** Address matching compares `address_line1 + city +
pincode`. If the payload's pincode is *blank*, matching falls back to street +
city, so pre-creating the Address in ERPNext with the right pincode is enough. If
the payload's pincode is present but *wrong*, the strict match fails and a retry
creates another address with the wrong pincode — so the payload must be
corrected. After a successful retry, check for a stale Address record left behind
by the failed attempt.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `No valid items` | SKU in Shopify doesn't match Item Code in ERPNext | Add matching SKU to ERPNext item or update Shopify SKU |
| `No Item Tax Template` | ERPNext item missing tax configuration | Add Item Tax Template in the item's Taxes table |
| `Payment Entry skipped (No Account)` | Gateway not mapped to a bank account | Add row in Shopify Settings → Gateway Mapping |
| `HMAC verification failed` | Wrong webhook secret | Re-copy the secret from Shopify Admin |
| `You cannot unset 'Read Only' for field Payload` | Frappe blocks it — `payload` is read-only in the DocType | Use **Fix Payload → Edit Payload** instead; never edit the audit field |
| Retry keeps failing on a pincode | Retry replays the *stored* payload, not a fresh copy | **Fix Payload → Re-fetch from Shopify**, or **Edit Payload** |
| `Gateway Reference Field Missing` | App upgraded but not migrated | Run `bench --site <site> migrate` |
| `Gateway Reference API Error` (401/403) | Token invalid/expired, or app missing `read_orders` | Check the credentials; a cached minted token is evicted automatically so the next call re-mints |
| `Shopify rejected the client credentials` | Wrong Client ID/Secret, **or the store is in a different Shopify organization** | Verify both values; a store in another account needs its own app |
| `Incomplete Admin API Credentials` on save | Only one of Client ID / Client Secret filled in | Supply both, or clear both |
| `Gateway Reference Empty` | Gateway returned no reference for that transaction | Nothing to fix — the field is correctly left blank |
| Fulfillment HTTP 403 | Token lacks the fulfillment scopes | Add `write_merchant_managed_fulfillment_orders` + `fulfill_and_ship_orders`, regenerate, re-paste |
| `Fulfillment Field Missing` / `not migrated` | App upgraded but not migrated | Run `bench --site <site> migrate` |
| `assigned to a third-party fulfillment service` | Order routes to a 3PL | Fulfil it in Shopify admin — a direct fulfillment is not permitted |
| `No open Shopify fulfillment order line matched` | SKU/line mismatch, or already fulfilled in Shopify | Check the order in Shopify; the note field lists what could not be matched |
| Fulfillment stuck on `Pending` | Worker died mid-request | Reclaimed automatically after 30 min, or click Fulfil again |

---

## Tests

The gateway-reference logic has unit tests that run without a bench (`frappe` is faked —
see `shopify_integration/tests/frappe_stub.py`):

```bash
python -m unittest discover -s shopify_integration/tests -t . -p "test_*.py"
```

---

## License

GNU GPLv3 — see [LICENSE](LICENSE).
