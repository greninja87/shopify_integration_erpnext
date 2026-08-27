"""
shopify_api.py — Minimal outbound client for the Shopify Admin REST API.

This app is webhook-driven: Shopify pushes, we react.  A few facts, however,
simply are not in the webhook payload and can only be pulled.  The gateway
transaction id is one of them (see gateway_reference.py), so this module exists
to make exactly that class of read possible, with the safety rails a background
job needs.

Configuration (Shopify Settings → Connection → Shopify Admin API) — supply
EITHER a static token OR a Client ID/Secret pair:

    admin_api_access_token   Password  static token from a legacy custom app
    admin_api_client_id      Data      Dev Dashboard app Client ID
    admin_api_client_secret  Password  Dev Dashboard app Client Secret
    api_version              Data      REST version, defaults to 2026-01

Either way the app needs the `read_orders` scope, which covers the
OrderTransaction object the gateway reference reads.

Legacy custom apps (Shopify admin → Apps → Develop apps) could no longer be
created after 1 Jan 2026 and issue a non-expiring token you paste once.  A Dev
Dashboard app instead gives you a Client ID and Secret, which this module
exchanges for a 24-hour token and caches — see the Credentials section below.

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
#
# Two ways a store can authenticate, checked in this order:
#
#   1. A static Admin API access token (admin_api_access_token).
#      Legacy custom apps created in the Shopify admin before 1 Jan 2026 issue
#      one of these.  It does not expire until the app is uninstalled, so it is
#      read straight from Settings and used as-is.
#
#   2. Client ID + Client Secret (admin_api_client_id / admin_api_client_secret)
#      exchanged for a token via the client credentials grant.  This is what a
#      Dev Dashboard app gives you: there is no token to copy out of the
#      dashboard, and legacy custom apps can no longer be created.
#
#      That token lives 24 hours (expires_in is always 86399), so it is minted
#      on demand and cached until shortly before it lapses.  Minting per call
#      would add a round trip to every request for no benefit.
#
#      Restriction worth knowing: the client credentials grant only reaches
#      stores in the SAME Shopify organization as the app.  A store under a
#      separate account needs its own app and its own Settings record — a
#      cross-organization token is not obtainable this way.

# Refresh this many seconds before the token actually expires, so a request
# already in flight is never the one that discovers it went stale.
_TOKEN_REFRESH_MARGIN = 300


def _static_token(settings) -> str:
    """
    The stored Admin API access token, or "".

    Password fieldtypes read back as masked asterisks off the doc, so this has
    to go through get_decrypted_password().
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
        # A missing field on an un-migrated install must not raise — callers
        # treat "" as "not configured" and skip.
        return ""


def _client_credentials(settings) -> tuple:
    """(client_id, client_secret), either possibly ""."""
    if not settings or not settings.get("name"):
        return "", ""
    client_id = (settings.get("admin_api_client_id") or "").strip()
    try:
        client_secret = (
            get_decrypted_password(
                "Shopify Settings", settings.name, "admin_api_client_secret",
                raise_exception=False,
            ) or ""
        ).strip()
    except Exception:
        client_secret = ""
    return client_id, client_secret


def _token_cache_key(settings) -> str:
    return "shopify_admin_api_token::%s" % settings.get("name")


def invalidate_cached_token(settings):
    """
    Drop the cached token for a store.

    Called when Shopify rejects it (401/403).  Without this, a rotated Client
    Secret or a revoked token would keep failing for up to 24 hours while we
    kept handing back a cached value Shopify no longer accepts.
    """
    if not settings or not settings.get("name"):
        return
    try:
        frappe.cache().delete_value(_token_cache_key(settings))
    except Exception:
        pass  # cache trouble must never break a request


def _mint_client_credentials_token(settings, client_id: str, client_secret: str) -> str:
    """
    Exchange Client ID + Secret for a 24-hour Admin API access token.

        POST https://{shop}/admin/oauth/access_token
        Content-Type: application/x-www-form-urlencoded
        grant_type=client_credentials&client_id=...&client_secret=...

    Response carries access_token, scope and expires_in.  The token is cached
    until _TOKEN_REFRESH_MARGIN seconds before it expires.

    :raises ShopifyAPIError: on any failure.  The secret never appears in an
                             error message or a log line.
    """
    import requests

    shop_domain = (settings.get("shop_domain") or "").strip()
    if not shop_domain:
        raise ShopifyAPIError("Shopify Settings has no shop_domain.")

    url = "https://%s/admin/oauth/access_token" % shop_domain
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    _throttle()
    try:
        response = requests.post(
            url, data=payload, headers={"Accept": "application/json"}, timeout=_TIMEOUT
        )
    except Exception as exc:
        raise ShopifyAPIError("Could not reach Shopify to mint an access token: %s" % exc)

    status = response.status_code

    if status in (400, 401, 403):
        # Deliberately does not echo the response body: Shopify sometimes
        # reflects request parameters, and the secret must not reach a log.
        raise ShopifyAPIError(
            "Shopify rejected the client credentials for store '%s' (HTTP %s). "
            "Check the Client ID and Client Secret in Shopify Settings, and that "
            "this store is in the SAME Shopify organization as the app — the "
            "client credentials grant cannot reach a store in another "
            "organization." % (shop_domain, status),
            status,
        )

    if status >= 400:
        raise ShopifyAPIError("Minting an access token failed with HTTP %s." % status, status)

    try:
        body = response.json() or {}
    except Exception as exc:
        raise ShopifyAPIError("Token response was not JSON: %s" % exc)

    token = (body.get("access_token") or "").strip()
    if not token:
        raise ShopifyAPIError("Shopify returned no access_token.")

    # expires_in is documented as always 86399, but read it rather than
    # hardcoding a lifetime we would not notice changing.
    try:
        expires_in = int(body.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    ttl = max(60, (expires_in or 86399) - _TOKEN_REFRESH_MARGIN)

    try:
        frappe.cache().set_value(_token_cache_key(settings), token, expires_in_sec=ttl)
    except Exception:
        # Operating without the cache still works; it just re-mints per call.
        frappe.log_error(
            "Could not cache the Shopify access token for '%s'. Requests will "
            "still work, but a token will be minted per call."
            % settings.get("name"),
            "Shopify: Token Cache Unavailable",
        )

    frappe.logger().info(
        "Shopify: minted an Admin API token for '%s' (valid %ss, cached %ss, "
        "scopes: %s)"
        % (settings.get("name"), expires_in or 86399, ttl, body.get("scope") or "unknown")
    )
    return token


def get_admin_api_token(settings) -> str:
    """
    A usable Admin API access token for this store, or "" when the store has no
    credentials configured at all.

    Prefers a static token; otherwise returns the cached minted token, minting a
    fresh one when the cache is empty.

    :raises ShopifyAPIError: when credentials ARE configured but no token can be
                             obtained.  "" means "not configured", so a caller
                             can tell "feature off" from "feature broken".
    """
    static = _static_token(settings)
    if static:
        return static

    client_id, client_secret = _client_credentials(settings)
    if not (client_id and client_secret):
        return ""

    try:
        cached = frappe.cache().get_value(_token_cache_key(settings))
    except Exception:
        cached = None
    if cached:
        return cached.decode() if isinstance(cached, bytes) else str(cached)

    return _mint_client_credentials_token(settings, client_id, client_secret)


def has_admin_api_credentials(settings) -> bool:
    """
    Whether this store is configured to call the Admin API.

    Configuration check only — deliberately never mints a token.  Callers use
    this as a cheap "is the feature on?" test on paths that run once per order,
    and an HTTP round trip there would be indefensible.
    """
    if not settings or not (settings.get("shop_domain") or "").strip():
        return False
    if _static_token(settings):
        return True
    client_id, client_secret = _client_credentials(settings)
    return bool(client_id and client_secret)


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
            # Drop any cached minted token so the next call re-mints rather than
            # replaying a token Shopify has stopped accepting.
            invalidate_cached_token(settings)
            raise ShopifyAPIError(
                f"GET {path} returned HTTP {status} — the Admin API token is "
                f"invalid, expired, or missing the read_orders scope. "
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
