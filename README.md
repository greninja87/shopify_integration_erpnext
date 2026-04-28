# Shopify Integration for ERPNext

Automatically creates **Customers**, **Sales Orders**, **Payment Entries**, **Delivery Notes → Sales Invoices** in ERPNext from Shopify webhooks. Supports **India Compliance (GST)**, partial payments, and multi-store configurations.

## Features

- **Webhook-driven** — Real-time order sync via Shopify `orders/create` webhooks
- **India GST compliant** — Tax-inclusive prices back-calculated to tax-exclusive rates using ERPNext Item Tax Templates
- **GSTIN support** — Automatic B2B customer creation with GST-registered billing addresses via India Compliance portal
- **Payment Entry** — Automatic PE creation with gateway mapping (Cashfree, Razorpay, COD, etc.)
- **Sales Invoice** — Auto-generated after Payment Entry or after Delivery Note (configurable)
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

### 4. Sales Invoice (Optional)

Enable **Enable Sales Invoice Creation** and choose a trigger:

- **After Payment Entry** — SI created immediately after PE in the same webhook
- **After Delivery Note** — SI created by hourly scheduler after DN is submitted

Both paths use **Allocate Advances Automatically (FIFO)** to link Payment Entries.

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
  6. Sales Invoice created (if enabled, trigger-dependent)
     - FIFO advance allocation links PE → SI automatically
  ↓
  Shopify Log updated → "Processed"
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

---

## Retry Failed Orders

If a webhook fails (e.g. missing SKU, payment gateway not mapped), go to:
**ERPNext → Shopify Log → [failed log entry] → Retry Order**

The same payload is replayed through the full SO creation pipeline.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `No valid items` | SKU in Shopify doesn't match Item Code in ERPNext | Add matching SKU to ERPNext item or update Shopify SKU |
| `No Item Tax Template` | ERPNext item missing tax configuration | Add Item Tax Template in the item's Taxes table |
| `Payment Entry skipped (No Account)` | Gateway not mapped to a bank account | Add row in Shopify Settings → Gateway Mapping |
| `HMAC verification failed` | Wrong webhook secret | Re-copy the secret from Shopify Admin |

---

## License

GNU GPLv3 — see [LICENSE](LICENSE).
