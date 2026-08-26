"""
shopify_api.py — Minimal outbound client for the Shopify Admin REST API.

This app is webhook-driven: Shopify pushes, we react.  A few facts, however,
simply are not in the webhook payload and can only be pulled.  The gateway
transaction id is one of them (see gateway_reference.py), so this module exists
to make exactly that class of read possible, with the safety rails a background
job needs.

Configuration (Shopify Settings → Connection → Shopify Admin API):
    admin_api_access_token  Password  Admin API access token (shpat_…)
    api_version             Data      REST version, defaults to 2026-01

The token needs the `read_orders` scope.  Create it in Shopify Admin →
Settings → Apps and sales channels → Develop apps → your app →
API credentials → Admin API access token.

Rate limiting
-------------
Shopify's REST Admin API uses a leaky-bucket limiter: 2 requests/second
sustained for a standard store (4/s on Advanced, 20/s on Plus).  We pace to the
most conservative figure — one request every 0.5 s — and additionally honour
`Retry-After` when the server answers 429.  The pacer is per worker process,
which is correct for the two ways this module is used:

    * order sync — one request per order, nowhere near the ceiling
    * backfill   — a single sequential loop inside one background job

If you ever fan the backfill out across concurrent workers, the per-process
pacer no longer bounds the aggregate rate; the 429 retry still keeps it
correct, just less polite.
"""

import time

import frappe
from frappe.utils.password import get_decrypted_password

DEFAULT_API_VERSION = "2026-01"

_MIN_INTERVAL   = 0.5   # seconds between requests → 2 req/sec
_MAX_ATTEMPTS   = 5     # total tries per request, including the first
_BACKOFF_BASE   = 1.0   # seconds; doubles per retry for 5xx / transport errors
_MAX_SLEEP      = 30.0  # cap on any single sleep, so a hostile header can't hang a worker
_TIMEOUT        = 30    # per-request socket timeout

# Per-process pacer.  time.monotonic() is immune to clock adjustments.
_last_request_at = 0.0


class ShopifyAPIError(Exception):
    """Any non-recoverable failure talking to the Shopify Admin API."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


# ── Credentials ────────────────────────────────────────────────────────────────

def get_admin_api_token(settings) -> str:
    """
    Decrypted Admin API access token for a store, or "" when not configured.

    admin_api_access_token is a Password fieldtype — reading it off the doc
    returns masked asterisks, so it must go through get_decrypted_password().
    """
    if not settings or not settings.get("name"):
        return ""
    try:
        return (
            get_decrypted_password(
                "Shopify Settings", settings.name, "admin_api_access_token",
                raise_exception=False,
            ) or ""
        ).strip()
    except Exception:
        # A missing field on an older install must not raise — the caller
        # treats "" as "feature not configured" and skips silently.
        return ""


def has_admin_api_credentials(settings) -> bool:
    """True when this store can make Admin API calls."""
    return bool(settings and settings.get("shop_domain") and get_admin_api_token(settings))


def get_api_version(settings) -> str:
    """Configured REST API version, falling back to DEFAULT_API_VERSION."""
    configured = (settings.get("api_version") or "").strip() if settings else ""
    return configured or DEFAULT_API_VERSION


# ── Request plumbing ───────────────────────────────────────────────────────────

def _throttle():
    """Block until at least _MIN_INTERVAL has elapsed since the last request."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if 0 <= elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def _retry_after_seconds(response, fallback: float) -> float:
    """Seconds to wait before retrying, from the Retry-After header."""
    raw = (response.headers.get("Retry-After") or "").strip()
    try:
        wait = float(raw)
    except (TypeError, ValueError):
        wait = fallback
    if wait <= 0:
        wait = fallback
    return min(wait, _MAX_SLEEP)


def get(settings, path: str, params: dict = None) -> dict:
    """
    GET one Admin API endpoint and return the decoded JSON body.

    :param settings: Shopify Settings document
    :param path:     path below the version, e.g. "orders/123/transactions.json"
    :raises ShopifyAPIError: on missing credentials, HTTP error, or bad JSON
    """
    # requests ships with Frappe; imported lazily so this module stays
    # importable in environments without it (e.g. the off-bench unit tests).
    import requests

    token = get_admin_api_token(settings)
    if not token:
        raise ShopifyAPIError(
            f"No Admin API access token configured for store "
            f"'{settings.get('name') if settings else '?'}'."
        )

    shop_domain = (settings.get("shop_domain") or "").strip()
    if not shop_domain:
        raise ShopifyAPIError("Shopify Settings has no shop_domain.")

    url = f"https://{shop_domain}/admin/api/{get_api_version(settings)}/{path.lstrip('/')}"
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
    }

    last_error = None
    for attempt in range(_MAX_ATTEMPTS):
        _throttle()
        try:
            response = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT)
        except Exception as exc:  # transport-level: DNS, TLS, timeout, reset
            last_error = ShopifyAPIError(f"GET {path} failed: {exc}")
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(min(_BACKOFF_BASE * (2 ** attempt), _MAX_SLEEP))
                continue
            raise last_error

        status = response.status_code

        if status == 429:
            # Rate limited.  Honour Retry-After; Shopify normally sends "2.0".
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_retry_after_seconds(response, _MIN_INTERVAL * 2))
                continue
            raise ShopifyAPIError(
                f"GET {path} rate limited (429) after {_MAX_ATTEMPTS} attempts.", 429
            )

        if status >= 500:
            # Shopify hiccup — worth retrying with exponential backoff.
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(min(_BACKOFF_BASE * (2 ** attempt), _MAX_SLEEP))
                continue
            raise ShopifyAPIError(f"GET {path} returned HTTP {status}.", status)

        if status in (401, 403):
            raise ShopifyAPIError(
                f"GET {path} returned HTTP {status} — the Admin API access token is "
                f"invalid or is missing the read_orders scope. "
                f"Check Shopify Settings → Connection → Shopify Admin API.",
                status,
            )

        if status == 404:
            raise ShopifyAPIError(
                f"GET {path} returned HTTP 404 — the order no longer exists in "
                f"Shopify, or the API version is wrong.",
                404,
            )

        if status >= 400:
            raise ShopifyAPIError(
                f"GET {path} returned HTTP {status}: {(response.text or '')[:300]}",
                status,
            )

        try:
            return response.json() or {}
        except Exception as exc:
            raise ShopifyAPIError(f"GET {path} returned unparseable JSON: {exc}", status)

    # Unreachable — every branch above either returns or raises.
    raise last_error or ShopifyAPIError(f"GET {path} failed.")


# ── Endpoints ──────────────────────────────────────────────────────────────────

def get_order(settings, shopify_order_id) -> dict:
    """
    One order, fresh from Shopify.

        GET /admin/api/{version}/orders/{id}.json

    Used to re-pull a webhook payload after the underlying order was corrected
    in Shopify — see shopify_log.refetch_payload_from_shopify().  The response
    body's `order` object has the same shape as the orders/create webhook, so it
    can be replayed through create_sales_order_from_shopify() unchanged.

    :raises ShopifyAPIError: propagated from get(), or when no order comes back
    """
    order_id = str(shopify_order_id or "").strip()
    if not order_id:
        raise ShopifyAPIError("get_order called without a Shopify order id.")

    body = get(settings, f"orders/{order_id}.json")
    order = body.get("order")
    if not isinstance(order, dict) or not order:
        raise ShopifyAPIError(
            f"Shopify returned no order for id {order_id}.", None
        )
    return order


def get_order_transactions(settings, shopify_order_id) -> list:
    """
    All transactions recorded against a Shopify order, newest-first as Shopify
    returns them (callers must not rely on the order — see
    gateway_reference.select_gateway_transaction).

        GET /admin/api/{version}/orders/{id}/transactions.json

    :raises ShopifyAPIError: propagated from get()
    """
    order_id = str(shopify_order_id or "").strip()
    if not order_id:
        raise ShopifyAPIError("get_order_transactions called without a Shopify order id.")

    body = get(settings, f"orders/{order_id}/transactions.json")
    txns = body.get("transactions")
    return txns if isinstance(txns, list) else []
