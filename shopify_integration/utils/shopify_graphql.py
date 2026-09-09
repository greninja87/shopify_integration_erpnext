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

Retries, and the documents that must not be re-posted
-----------------------------------------------------
execute() retries the identical document up to _MAX_ATTEMPTS times on transport
errors, 429 and 5xx.  That is what a READ still gets, and reads are most of the
callers: fulfillment's orderFulfillmentOrders query and refund.py's
RefundTargets query both keep it, because re-posting a read cannot create
anything.

An earlier version of this docstring justified giving fulfillmentCreate the
same treatment by asserting that re-pushing one that already landed "gets a
userErrors rejection, not a second shipment", and left it standing as an open
question.  It has been audited since, and the answer is that the claim is false
for the case fulfillment.py actually builds.  It holds for a FULL fulfillment;
a PARTIAL one leaves unfulfilled quantity on the fulfillment order, so Shopify
ACCEPTS a second post of the identical input — a duplicate fulfillment, the
same goods fulfilled twice, and a second tracking email to the customer, since
notify comes from notify_customer_on_fulfillment.  It is the same headroom
argument the refund rule below rests on, and fulfillment.py builds partial
fulfillments deliberately (plan["unallocated"], STATUS_PARTIAL).  So
fulfillment.py now posts fulfillmentCreate with idempotent=False, and its
fulfillmentCancel keeps the retries: that one names an existing fulfillment by
id and drives it to CANCELED, so a re-post reaches the same end state.

What remains open is on fulfillment.py's side, not this module's.  It has no
possibly-sent state: every failure path there records STATUS_FAILED, which the
hourly scheduler and the Fulfil button both re-pick, so idempotent=False cuts
the automatic re-posts of a possibly-committed mutation to one and then hands
the remaining risk to the OUTER retry, guarded by nothing but the warning text
fulfillment._possibly_sent_warning writes into the error field.  refund.py has
the state this needs — STATUS_UNVERIFIED, which no trigger picks up — and
giving fulfillment the equivalent needs a new Select option on the custom field
plus a patch.

refundCreate is different, and audited.  It moves money — a successful one pays
the customer through the Cashfree-OCC bridge — and refund.py posts it with no
@idempotent key, so Shopify cannot recognise a second POST of the same document
as the same refund.  A partial refund that succeeds and loses its answer to the
30-second socket timeout leaves the order with enough headroom to take another,
so the retry creates a SECOND refund and the customer is paid twice.

That is what `idempotent=False` is for.  It does NOT mean "one attempt": it
means "retry only on failures that PROVE the document did not execute".  See
execute()'s docstring for which failures qualify and why.

Exactly one failure qualifies, and it is narrow: HTTP 200 with extensions.code
THROTTLED, in the shape the GraphQL spec reserves for a request refused BEFORE
execution began — no `data` key at all, and no errors[].path.  Everything else
on the money path is posted once and handed to a human.  In particular a bare
HTTP 429 does not qualify: Shopify's own GraphQL throttling arrives as a 200
body, so a 429 on this endpoint may well be a CDN, a WAF or an egress proxy in
front of the store, and a layer like that knows nothing about whether the
document behind it ran.

Whether a document is RE-POSTED and whether the failure PROVES non-execution
are two separate questions, and round 2 got into trouble by fusing them.  An
idempotent caller may re-post on a throttle whatever the body shape, because a
re-read is free and refund.py's RefundTargets query is promised that
resilience; it just gets no proof out of it.  A non-idempotent caller may
re-post only on the proven refusal above.  Both mutations that create something
— refundCreate and fulfillmentCreate — are now on the second path, so the
idempotent path is reads and fulfillmentCancel.

Who gets to say "nobody was paid"
---------------------------------
This module does, on the exception, via ShopifyAPIError.proves_not_executed.
Callers must not re-derive it from error_codes: a THROTTLED refusal, a
THROTTLED body that shows execution began, and an HTTP 429 all carry the same
code and do not carry the same meaning, and only the raise site knows which one
it saw.
"""

import json
import time

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


def _refused_before_execution(body, codes) -> bool:
    """
    Whether this HTTP 200 is Shopify refusing to RUN the document.

    The one shape that proves non-execution, stated once so the rule is
    readable.  The GraphQL spec separates a request that failed BEFORE
    execution began from one that failed DURING it, and the two arrive as
    different bodies:

        no "data" KEY at all       execution never started.  This is Shopify's
                                   ordinary cost refusal, and the spec reserves
                                   the omission for exactly this case.
        a "data" key, possibly     execution BEGAN and died partway.
        null, plus errors[].path   `path` names the field it died on, and a
                                   path exists only for a field that was being
                                   resolved.

    Round 2 keyed the proof on `body.get("data") is None`, which reads the
    second shape as the first: {"data": null, "errors": [{"path":
    ["refundCreate"]}]} is a mutation that started running, and calling that
    "nobody was paid, safe to retry" is the double-pay incident wearing the
    safe branch's clothes.  So both halves are required — no `data` key AND no
    error entry carrying a path.

    `path` is tested by KEY presence, not by truthiness: a body that reports
    the key at all has said something about execution, and between "might pay
    twice" and "might need a human to look" this module chooses the human.
    """
    if "THROTTLED" not in (codes or []):
        # Only THROTTLED is the documented refusal.  Any other code on a 200
        # says Shopify processed the document far enough to complain about
        # something else.
        return False
    if "data" in (body or {}):
        return False
    for err in (body or {}).get("errors") or []:
        if isinstance(err, dict) and "path" in err:
            return False
    return True


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


def execute(
    settings,
    query: str,
    variables: dict = None,
    operation: str = "",
    idempotent: bool = True,
) -> dict:
    """
    Run one GraphQL document and return its `data` object.

    :param settings:   Shopify Settings document
    :param query:      the GraphQL query or mutation
    :param variables:  variables dict
    :param operation:  short label used in error messages, e.g. "fulfillmentCreate"
    :param idempotent: may this exact document be safely re-posted?  True, the
                       default, keeps every existing caller's retry behaviour
                       byte-for-byte.  Pass False for a document that must be
                       posted AT MOST ONCE in any way that could have executed.
    :raises ShopifyAPIError: missing credentials, HTTP error, query-level errors,
                             or unparseable response.  `error_codes` carries the
                             GraphQL extensions.code values on a 200-with-errors
                             failure, for logs and human triage.
                             `proves_not_executed` is the one a money-moving
                             caller may branch on — see below.

    proves_not_executed: who is allowed to say "nobody was paid"
    ------------------------------------------------------------
    This function, on the exception it raises — never the caller, from the error
    codes.  It is a claim about Shopify's behaviour that only the client can
    make: only the raise site saw the response body and knows whether Shopify
    answered INSTEAD of running the document.  It is True on exactly two
    raises:

        401 / 403        refused at the auth layer, the document unreached
        exhausted        Shopify's cost refusal, and ONLY in the shape the spec
        THROTTLED        reserves for a request refused before execution began:
                         no `data` key, no errors[].path.  See
                         _refused_before_execution.

    Everywhere else it is False, including two responses that carry the same
    extensions.code and the opposite meaning:

        200 + THROTTLED  a document can resolve part way and then blow the cost
        + execution       budget.  `data` present, or an errors[].path naming
        began             the field it died on, says it at least partly ran and
                          may be hiding a mutation that committed.
        HTTP 429          says a rate limiter refused the request; it does not
                          say WHICH one.  Shopify throttles GraphQL with a 200
                          body, so a 429 here may be a CDN, a WAF or an egress
                          proxy in front of the store, which is no evidence at
                          all about the document behind it.

    Any caller sniffing "THROTTLED" in error_codes gets both of those exactly
    backwards, and backwards here means reporting a paid refund as never-sent
    and inviting a retry.  The default is the safe one: False means "assume it
    may have run", which sends the row to a human.

    Why idempotent=False exists
    ---------------------------
    refundCreate is the caller that needs it.  A refundCreate that succeeds pays
    a real customer real money through the Cashfree-OCC bridge, and below Admin
    API version 2026-04 the document carries no @idempotent key — the directive
    is optional there and refund.build_refund_mutation() is given one only from
    the version that requires it — so Shopify has no way to recognise a second
    POST of the same document as the same refund.  Attempt 1 creates a real
    partial refund, its response dies in the _TIMEOUT socket timeout, this
    function re-POSTs the identical mutation, the order still has headroom
    because the refund was partial, and Shopify creates a SECOND refund.
    Nothing in between consults the stored GID or the worker claim, so nothing
    stops it.

    A keyed post (2026-04 and above) would survive that re-POST — Shopify
    deduplicates a repeat carrying the same key and the same parameters for 24
    hours and returns the first response — but idempotent=False is passed
    regardless, and re-enabling the retries on the strength of the key is a
    deliberate contract change in refund.py rather than something this docstring
    grants.

    Retry only on failures that PROVE non-execution
    -----------------------------------------------
    idempotent=False is deliberately NOT `attempts = 1`, because the failures
    this loop retries on are not all the same kind of fact.  What each one
    proves, and what idempotent=False therefore does with it:

        transport exception  the POST may have been delivered and executed and
                             only the ANSWER lost.  Proves nothing -> no retry.
        status >= 500        Shopify's edge can answer 502/503 after the
                             mutation committed.  Proves nothing -> no retry.
        status == 429        proves nothing either: Shopify throttles GraphQL
                             with a 200 body, so this may be a CDN, a WAF or an
                             egress proxy in front of the store, and that layer
                             knows nothing about the document behind it.  Round
                             2 retried it as a refusal; round 3 posts once and
                             stops -> no retry.
        200 + THROTTLED      Shopify's own cost refusal, in the shape the spec
        refused before       reserves for it: no `data` key, no errors[].path.
        execution            The document did not run -> STILL RETRIED, and the
                             only failure a money-moving post is retried on.
        200 + THROTTLED      NOT a refusal.  A document can resolve part way
        after execution      and then blow the cost budget; `data` present, or
        began                a path naming the field it died on, says it at
                             least partly executed.  Proves nothing -> no retry
                             for a non-idempotent caller.

    The last two rows are gated on the body shape; the 429 row and the last row
    are still RE-POSTED for an idempotent caller, which is a different question
    from proof (see the module docstring).  Collapsing the proven refusal into a
    single attempt would be the blunt fix and the wrong one: re-posting after a
    refusal cannot double-pay, and dropping that retry would park Shopify's
    ordinary cost limiting in a state that needs a human to reconcile every row
    by hand.  When the choice really is between "might pay twice" and "might
    need a human to look", we choose the human — and everywhere the refusal is
    not PROVEN, that is now the choice being made, at the cost of somebody
    opening the order in Shopify.
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
            # The double-pay case.  A timeout or a reset says the answer never
            # came back, NOT that the document never ran, so a non-idempotent
            # document cannot be given a second chance on that evidence.
            if idempotent and attempt < _MAX_ATTEMPTS - 1:
                time.sleep(min(_BACKOFF_BASE * (2 ** attempt), _MAX_SLEEP))
                continue
            raise last_error

        status = response.status_code

        if status == 429:
            # A bare HTTP 429 is NOT evidence that Shopify refused anything.
            # This module documents Shopify's own GraphQL throttling as HTTP
            # 200 with extensions.code THROTTLED; a 429 on the graphql.json
            # endpoint is as likely to come from a CDN, a WAF or an egress
            # proxy sitting in FRONT of Shopify, and such a layer knows nothing
            # about whether the document behind it ran.  The branch is
            # inherited from the REST client's shape, not from anything Shopify
            # documents here.
            #
            # So a non-idempotent document is posted once and stops: round 2
            # re-posted it up to five times on the premise "a rate limiter
            # rejects without executing", which is a premise about somebody
            # else's infrastructure, and the price of it being wrong is a
            # second real refund.  An idempotent caller keeps the retry — a
            # read or a re-postable document loses nothing by waiting.
            if idempotent and attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_MIN_INTERVAL * 2)
                continue
            raise ShopifyAPIError(
                f"{label} rate limited (429).",
                429,
                # The code is DIAGNOSTIC here and the proof below is not, on
                # one and the same exception: a human triaging a rate limit
                # wants to see THROTTLED on it, while nobody may conclude
                # non-execution from it.  That pairing is the whole reason
                # callers must not sniff error_codes for a payout decision.
                error_codes=["THROTTLED"],
                # Withdrawn in round 3.  The open trade: an HTTP-layer rate
                # limit on the refund path now lands as "unknown", so a person
                # opens the order in Shopify.  That is the doctrine this module
                # states for itself, and the alternative asserts a premise
                # about infrastructure we do not own.
                proves_not_executed=False,
            )

        if status >= 500:
            # Not proof of non-execution: a 502 from the edge or a 503 from a
            # load balancer shedding traffic can arrive after the mutation
            # committed, so idempotent=False stops here.
            if idempotent and attempt < _MAX_ATTEMPTS - 1:
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
                f"invalid or lacks the required scopes. A read_orders-only token "
                f"is not enough for any write: fulfillment needs "
                f"write_merchant_managed_fulfillment_orders (and "
                f"write_third_party_fulfillment_orders for 3PL orders) plus the "
                f"fulfill_and_ship_orders permission, and a refund needs "
                f"write_orders. The operation that failed is named at the front "
                f"of this message.",
                status,
                # Rejected at the auth layer: Shopify never reached the
                # document, so a refundCreate that dies here paid nobody.
                proves_not_executed=True,
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
            # The one fact both decisions below turn on: did Shopify refuse to
            # run this document, or did the document begin running and die?
            # See _refused_before_execution for the two wire shapes and why
            # round 2's `data is None` conflated them.
            refused = _refused_before_execution(body, codes)

            # The retry gate is `idempotent or refused`, and the two halves are
            # different arguments:
            #
            #   idempotent    the caller says this exact document may be
            #                 re-posted.  A re-read is free, so a read keeps
            #                 its throttle resilience — refund.py's comment at
            #                 the RefundTargets query promises exactly that,
            #                 and round 2 stripped it by gating on the body
            #                 shape for everyone.  What is left on this half is
            #                 reads plus fulfillmentCancel, which reaches the
            #                 same end state however often it is posted.
            #                 fulfillmentCreate used to be here too, on the
            #                 open question the module docstring recorded; the
            #                 audit found a PARTIAL fulfillment leaves headroom
            #                 for a duplicate, so it is a non-idempotent caller
            #                 now.
            #   refused       for a non-idempotent document this is the only
            #                 licence to post again.  refundCreate resolves a
            #                 transactions(first: 10) connection with real
            #                 query cost, so it can resolve part way, blow the
            #                 budget, and answer 200 with `data` AND
            #                 THROTTLED — re-POSTing that throws away a refund
            #                 Shopify may have committed and creates a second
            #                 one.
            if ("THROTTLED" in codes and (idempotent or refused)
                    and attempt < _MAX_ATTEMPTS - 1):
                time.sleep(_throttle_wait_from(body))
                continue
            # The codes travel for logs and human triage.  The claim a
            # money-moving caller acts on is proves_not_executed, set here
            # because only this branch knows which THROTTLED it saw: a refusal
            # before execution means nobody was paid; the same code on a body
            # that shows execution began may be hiding a committed mutation, so
            # it must reach the caller as "unknown".  That gap is exactly why
            # callers must not sniff error_codes for this.
            raise ShopifyAPIError(
                f"{label} returned GraphQL errors: {json.dumps(errors)[:500]}",
                status,
                error_codes=codes,
                proves_not_executed=refused,
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
    :raises ShopifyAPIError:  when the mutation key is missing entirely, with
                              status_code 200 — Shopify answered in full, the
                              answer just did not contain the mutation, and a
                              caller that splits transport failures from
                              unusable answers on `status_code is None` must
                              not read this as the network breaking
    """
    payload = (data or {}).get(mutation_key)
    if payload is None:
        raise ShopifyAPIError(
            f"{mutation_key} missing from the GraphQL response"
            f"{f' ({context})' if context else ''}.",
            # 200 as a literal, and it is the right literal: this function only
            # ever sees a body execute() already accepted and parsed, which
            # execute() reaches only on a 2xx with a non-null `data`.  Shopify
            # ANSWERED, in full — the answer just did not contain the mutation.
            # refund.py splits "the transport failed" from "Shopify answered
            # something unusable" on `status_code is None`, so raising bare
            # filed this as transport_error_after_send and sent the reader to
            # look at the network on a request that had come back.
            200,
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
