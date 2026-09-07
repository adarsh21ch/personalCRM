# -*- coding: utf-8 -*-
"""
Razorpay REST calls, over httpx - no SDK, same reasoning as sadhna-astro's
web/rzp.py: a payments dependency is a dependency that has to be trusted,
pinned and updated, and this is four thin calls.

This app uses Razorpay SUBSCRIPTIONS, not one-time Orders: a client
authorizes a mandate once and Razorpay auto-debits it every billing cycle,
which is what "cut automatically every month" requires. Plans and
Subscriptions are separate objects from Orders and use different endpoints
and different webhook events (subscription.*, not payment.captured for the
recurring charge - though payment.* events still fire per-charge too).
"""
import httpx

API = "https://api.razorpay.com/v1"
TIMEOUT = 20


class RzpError(Exception):
    """Message is shown to the operator or the client - never a traceback."""


def _call(method, path, key_id, key_secret, **kw):
    if not (key_id and key_secret):
        raise RzpError("No Razorpay key id and secret are configured.")
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.request(method, API + path, auth=(key_id, key_secret), **kw)
    except httpx.HTTPError as exc:
        raise RzpError("Could not reach Razorpay (%s)." % type(exc).__name__)

    if r.status_code == 401:
        raise RzpError("Razorpay rejected these keys. Check the Key ID and Key Secret "
                       "are from the same pair and the same mode (test/live).")
    if r.status_code >= 400:
        try:
            detail = (r.json().get("error") or {}).get("description") or r.text[:200]
        except Exception:
            detail = r.text[:200]
        raise RzpError("Razorpay said: %s" % detail)
    try:
        return r.json()
    except Exception:
        raise RzpError("Razorpay returned something that was not JSON.")


def test_connection(key_id, key_secret):
    try:
        data = _call("GET", "/payments", key_id, key_secret, params={"count": 1})
    except RzpError as exc:
        return False, str(exc)
    n = data.get("count", 0)
    mode = "test" if key_id.startswith("rzp_test_") else "live"
    return True, ("These keys work. Connected to the %s account%s."
                  % (mode, "" if n else " — no payments on it yet"))


def create_plan(key_id, key_secret, name, amount_paise, interval="monthly"):
    """A Razorpay Plan - the price + cadence a Subscription is created against.
    Razorpay has no "update a plan" call; a changed price means a new plan,
    which is why plans are created lazily and cached by (name, amount)."""
    period = {"monthly": "monthly", "yearly": "yearly", "weekly": "weekly"}.get(interval, "monthly")
    return _call("POST", "/plans", key_id, key_secret, json={
        "period": period,
        "interval": 1,
        "item": {"name": name, "amount": int(amount_paise), "currency": "INR",
                 "description": "%s — billed %s" % (name, period)},
    })


def create_subscription(key_id, key_secret, plan_id, notes, customer_notify=True,
                         total_count=120):
    """total_count=120 monthly cycles (~10 years) is Razorpay's convention for
    an "indefinite" subscription; it auto-charges every cycle until the client
    cancels or a charge is halted after repeated failures. notes MUST carry
    subscription row id so the webhook can match a callback to our row."""
    if not (notes or {}).get("row_id"):
        raise RzpError("Internal: a subscription was about to be created with no "
                       "row_id in its notes, which the webhook needs to match it.")
    return _call("POST", "/subscriptions", key_id, key_secret, json={
        "plan_id": plan_id,
        "customer_notify": 1 if customer_notify else 0,
        "total_count": total_count,
        "notes": notes,
    })


def fetch_subscription(key_id, key_secret, subscription_id):
    return _call("GET", "/subscriptions/%s" % subscription_id, key_id, key_secret)


def cancel_subscription(key_id, key_secret, subscription_id, cancel_at_cycle_end=False):
    return _call("POST", "/subscriptions/%s/cancel" % subscription_id, key_id, key_secret,
                 json={"cancel_at_cycle_end": 1 if cancel_at_cycle_end else 0})
