"""
shopify_graphql.py — Admin GraphQL client.

Why a second client
-------------------
shopify_api.py speaks REST, which is all the gateway-reference lookup needs.
Fulfillment cannot be done over REST in any meaningful way: the modern model is
fulfillment orders, and the mutations that drive it (fulfillmentCreate,
fulfillmentCancel, fulfillmentOrderSubmitFulfillmentRequest) exist only in
GraphQL.  REST has also been a legacy API since October 2024.

So this module handles GraphQL, and deliberately reuses shopify_api for the
parts that must not diverge:

    * the access token and API version (same Shopify Settings fields)
    * the request pacer — importing shopify_api._throttle() means REST and
      GraphQL calls share ONE rate budget per process instead of two that each
      think they own the whole limit

The thing that makes GraphQL different, and dangerous
----------------------------------------------------
GraphQL fails at HTTP 200.  A client that trusts the status code will read
every one of these as success:

    1. Transport/auth failure     → non-2xx, same as REST
    2. Query-level `errors`       → HTTP 200 + {"errors": [...]}
       Throttling arrives here as extensions.code == "THROTTLED"
    3. Mutation `userErrors`      → HTTP 200, no `errors`, but the mutation
       payload carries userErrors and NOTHING HAPPENED

(3) is the one that corrupts data: treat it as success and you mark a Delivery
Note fulfilled when Shopify rejected the request.  So execute() raises on (2),
and check_user_errors() exists to make (3) impossible to forget — every caller
runs its payload through it.
"""

import json
import time

import frappe

from shopify_integration.utils.shopify_api import (
    ShopifyAPIError,
    invalidate_cached_token,
    _BACKOFF_BASE,
    _MAX_ATTEMPTS,
    _MAX_SLEEP,
    _MIN_INTERVAL,
    _TIMEOUT,
    _throttle,
    get_admin_api_token,
    get_api_version,
)

# GraphQL is metered in cost points (1000-point bucket, 50/s restore on a
# standard store) rather than requests/second.  When Shopify throttles it tells
# us the bucket state, so we can wait exactly long enough instead of guessing.
_DEFAULT_THROTTLE_WAIT = 2.0


class ShopifyUserError(ShopifyAPIError):
    """
    A mutation returned userErrors — the request was well-formed and
    authenticated, and Shopify declined it.

    Never retried: retrying an unchanged rejected mutation just gets rejected
    again.  `user_errors` holds the raw list for logging.
    """

    def __init__(self, message, user_errors=None):
        super().__init__(message)
        self.user_errors = user_errors or []


def _extract_error_codes(errors) -> list:
    codes = []
    for err in errors or []:
        if isinstance(err, dict):
            code = (err.get("extensions") or {}).get("code")
            if code:
                codes.append(str(code))
    return codes


def _throttle_wait_from(body) -> float:
    """
    How long to wait after a THROTTLED response.

    Shopify returns the leaky bucket's state in extensions.cost.throttleStatus,
    so we can compute the real wait rather than back off blindly:

        (requestedQueryCost - currentlyAvailable) / restoreRate
    """
    try:
        cost = ((body.get("extensions") or {}).get("cost") or {})
        status = cost.get("throttleStatus") or {}
        requested = float(cost.get("requestedQueryCost") or 0)
        available = float(status.get("currentlyAvailable") or 0)
        restore = float(status.get("restoreRate") or 0)
        if restore > 0 and requested > available:
            return min((requested - available) / restore + 0.25, _MAX_SLEEP)
    except (TypeError, ValueError, AttributeError):
        pass
    return _DEFAULT_THROTTLE_WAIT


def execute(settings, query: str, variables: dict = None, operation: str = "") -> dict:
    """
    Run one GraphQL document and return its `data` object.

    :param settings:  Shopify Settings document
    :param query:     the GraphQL query or mutation
    :param variables: variables dict
    :param operation: short label used in error messages, e.g. "fulfillmentCreate"
    :raises ShopifyAPIError: missing credentials, HTTP error, query-level errors,
                             or unparseable response
    """
    import requests  # ships with Frappe; lazy so off-bench tests can fake it

    token = get_admin_api_token(settings)
    if not token:
        raise ShopifyAPIError(
            f"No Admin API access token configured for store "
            f"'{settings.get('name') if settings else '?'}'."
        )

    shop_domain = (settings.get("shop_domain") or "").strip()
    if not shop_domain:
        raise ShopifyAPIError("Shopify Settings has no shop_domain.")

    url = f"https://{shop_domain}/admin/api/{get_api_version(settings)}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"query": query, "variables": variables or {}}
    label = operation or "graphql"

    last_error = None
    for attempt in range(_MAX_ATTEMPTS):
        _throttle()
        try:
            response = requests.post(
                url, headers=headers, data=json.dumps(payload), timeout=_TIMEOUT
            )
        except Exception as exc:
            last_error = ShopifyAPIError(f"{label} failed: {exc}")
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(min(_BACKOFF_BASE * (2 ** attempt), _MAX_SLEEP))
                continue
            raise last_error

        status = response.status_code

        if status == 429:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_MIN_INTERVAL * 2)
                continue
            raise ShopifyAPIError(f"{label} rate limited (429).", 429)

        if status >= 500:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(min(_BACKOFF_BASE * (2 ** attempt), _MAX_SLEEP))
                continue
            raise ShopifyAPIError(f"{label} returned HTTP {status}.", status)

        if status in (401, 403):
            # Same eviction as shopify_api.get(): drop any cached minted token so
            # the next call re-mints.  Without this a rotated Client Secret would
            # keep failing here for the life of the cache entry (~24h) while REST
            # recovered on its very next call.
            invalidate_cached_token(settings)
            raise ShopifyAPIError(
                f"{label} returned HTTP {status} — the Admin API access token is "
                f"invalid or lacks the required scopes. Fulfillment needs "
                f"write_merchant_managed_fulfillment_orders (and "
                f"write_third_party_fulfillment_orders for 3PL orders) plus the "
                f"fulfill_and_ship_orders permission. A read_orders-only token "
                f"is not enough.",
                status,
            )

        if status >= 400:
            raise ShopifyAPIError(
                f"{label} returned HTTP {status}: {(response.text or '')[:300]}", status
            )

        try:
            body = response.json() or {}
        except Exception as exc:
            raise ShopifyAPIError(f"{label} returned unparseable JSON: {exc}", status)

        # ── HTTP 200 with query-level errors ─────────────────────────────────
        errors = body.get("errors")
        if errors:
            codes = _extract_error_codes(errors)
            if "THROTTLED" in codes and attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_throttle_wait_from(body))
                continue
            raise ShopifyAPIError(
                f"{label} returned GraphQL errors: {json.dumps(errors)[:500]}", status
            )

        data = body.get("data")
        if data is None:
            raise ShopifyAPIError(f"{label} returned no data: {json.dumps(body)[:300]}", status)

        return data

    raise last_error or ShopifyAPIError(f"{label} failed.")


def check_user_errors(data: dict, mutation_key: str, context: str = ""):
    """
    Raise if a mutation payload carries userErrors, and return the payload.

    This is the guard against the quiet failure mode: HTTP 200, no `errors`,
    but the mutation did nothing.  Call it on every mutation result before
    recording success anywhere.

    :param data:         the `data` object returned by execute()
    :param mutation_key: e.g. "fulfillmentCreate"
    :param context:      extra detail for the error message (e.g. the DN name)
    :raises ShopifyUserError: when userErrors is non-empty
    :raises ShopifyAPIError:  when the mutation key is missing entirely
    """
    payload = (data or {}).get(mutation_key)
    if payload is None:
        raise ShopifyAPIError(
            f"{mutation_key} missing from the GraphQL response"
            f"{f' ({context})' if context else ''}."
        )

    user_errors = payload.get("userErrors") or []
    if user_errors:
        rendered = "; ".join(
            f"{'.'.join(str(f) for f in (e.get('field') or []))}: {e.get('message')}".strip(": ")
            for e in user_errors
            if isinstance(e, dict)
        )
        raise ShopifyUserError(
            f"{mutation_key} rejected by Shopify"
            f"{f' ({context})' if context else ''}: {rendered}",
            user_errors=user_errors,
        )

    return payload


def gid(resource: str, numeric_id) -> str:
    """
    Build a Shopify global id.

        gid("Order", 6428)  ->  "gid://shopify/Order/6428"

    Values that already look like a GID are passed through, so callers can hand
    us either form without checking.
    """
    raw = str(numeric_id or "").strip()
    if raw.startswith("gid://"):
        return raw
    return f"gid://shopify/{resource}/{raw}"


def numeric_id(global_id) -> str:
    """Trailing numeric id from a GID; the input unchanged when it isn't one."""
    raw = str(global_id or "").strip()
    if raw.startswith("gid://"):
        return raw.rsplit("/", 1)[-1]
    return raw
