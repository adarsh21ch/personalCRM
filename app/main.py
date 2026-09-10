# -*- coding: utf-8 -*-
"""
Personal CRM + Subscriptions - one operator's dashboard for every client
they've built a website/app for: who's on which plan, whether this month's
Razorpay auto-debit went through, and a link to send a new client so they
can put themselves on a plan.

    uvicorn app.main:app --reload --port 8090

Env: see .env.example. Nothing here is a client's payment gateway - it is
yours: one Razorpay account, entered once at /admin/settings, used for every
client's subscription.
"""
import os, sys, json, hmac, hashlib, uuid
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE]

from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import (HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import store, settings, auth, rzp, notify, invoice, emails

app = FastAPI(title="Personal CRM", docs_url=None, redoc_url=None)
# check_dir=False: StaticFiles raises at construction time if the directory is
# missing, which on a serverless platform would crash EVERY request (the app
# object is rebuilt on each cold start) rather than just the /static ones -
# a bundling quirk should degrade one route, not take the whole app down.
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static"), check_dir=False),
          name="static")
T = Jinja2Templates(directory=os.path.join(HERE, "templates"))

DEFAULT_PLANS = [
    ("Personal", 99900),
    ("Personal Plus", 119900),
    ("Business", 399900),
    ("Business Plus", 499900),
]

# Subscription lifecycle, collapsed to the four buckets asked for on the
# dashboard. Razorpay's own states (created/authenticated/active/pending/
# halted/cancelled/completed/expired) are kept verbatim in `status` - this
# is only how the dashboard groups and colours them.
STATUS_GROUPS = {
    "active": "active",
    "authenticated": "pending", "created": "pending", "pending": "pending",
    "halted": "declined",
    "cancelled": "cancelled", "completed": "cancelled", "expired": "cancelled",
}
STATUS_LABEL = {
    "created": "Awaiting checkout", "authenticated": "Mandate confirmed, awaiting first charge",
    "active": "Active", "pending": "Payment retry due", "halted": "Declined (charges failing)",
    "cancelled": "Cancelled", "completed": "Completed", "expired": "Expired",
}


def rupees(paise):
    return "{:,}".format(int(paise or 0) // 100)


T.env.filters["rupees"] = rupees
T.env.globals["status_label"] = lambda s: STATUS_LABEL.get(s, s)
T.env.globals["status_group"] = lambda s: STATUS_GROUPS.get(s, "pending")


@app.on_event("startup")
def _startup():
    # On a serverless platform this runs on every cold start, not once at
    # deploy - so a transient or misconfigured database must not take the
    # whole app down (every route, including the login page and /healthz).
    # Let it through here; individual routes that touch the database will
    # surface their own error instead of the app refusing to serve anything.
    try:
        store.init()
        if not store.list_rows("plans", limit=1):
            for name, amount in DEFAULT_PLANS:
                store.insert("plans", {"name": name, "amount_paise": amount,
                                       "interval": "monthly", "active": 1})
    except Exception as exc:
        print("startup: database not ready yet (%s): %s" % (type(exc).__name__, exc))


# --------------------------------------------------------------------- auth
def _session_email(request: Request):
    return auth.valid(request.cookies.get(auth.COOKIE))


def require_admin(request: Request):
    email = _session_email(request)
    if not email:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return email


def _nav_section(path):
    """Which sidebar link to highlight. A client's detail page (/admin/clients/{id})
    falls under Dashboard - it's reached by clicking a client row there, not
    a nav item of its own - but /admin/clients/new is its own link."""
    if path == "/admin/clients/new":
        return "add"
    if path.startswith("/admin/plans"):
        return "plans"
    if path.startswith("/admin/showcase"):
        return "showcase"
    if path.startswith("/admin/settings"):
        return "settings"
    if path.startswith("/admin"):
        return "dashboard"
    return ""


def ctx(request: Request, **extra):
    base = dict(request=request, business=settings.business_name(),
               rzp_ready=settings.razorpay_ready(), rzp_mode=settings.razorpay_mode(),
               nav=_nav_section(request.url.path))
    base.update(extra)
    return base


@app.exception_handler(HTTPException)
async def _redirect_to_login(request: Request, exc: HTTPException):
    if exc.status_code == 303 and exc.headers and exc.headers.get("Location") == "/admin/login":
        return RedirectResponse("/admin/login", status_code=303)
    return PlainTextResponse(str(exc.detail), status_code=exc.status_code)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """The backstop. Without this, any bug we haven't specifically guarded
    against (like the dashboard's database calls before this fix) shows an
    unreadable blank "Internal Server Error" with no way to diagnose it short
    of digging through platform logs. Always logged in full either way; on
    the operator-only surfaces (/admin*, /healthz) the message itself is also
    shown in the response, since nobody but the operator ever sees those and
    seeing the real error immediately beats a round trip through Vercel's log
    viewer. Public, client-facing pages (/subscribe, /webhook) keep a generic
    message - a stranger has no business seeing our stack traces."""
    import traceback
    print("UNHANDLED %s %s -> %s: %s\n%s" % (
        request.method, request.url.path, type(exc).__name__, exc, traceback.format_exc()))
    path = request.url.path
    if path.startswith("/admin") or path == "/healthz":
        return PlainTextResponse(
            "Something broke loading this page: %s: %s\n\n"
            "This has been logged. If it keeps happening after a reload, check "
            "/healthz and the Razorpay/Supabase settings." % (type(exc).__name__, exc),
            status_code=500)
    return PlainTextResponse("Something went wrong. Please try again in a moment.",
                             status_code=500)


@app.get("/admin/login", response_class=HTMLResponse)
def login_form(request: Request):
    if _session_email(request):
        return RedirectResponse("/admin", status_code=303)
    return T.TemplateResponse("login.html", ctx(request, error=None))


@app.post("/admin/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    ok, msg = auth.check(email, password, ip)
    if not ok:
        return T.TemplateResponse("login.html", ctx(request, error=msg), status_code=401)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.issue(email), max_age=auth.SESSION_MAX_AGE,
                    httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return resp


@app.post("/admin/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# ---------------------------------------------------------------- dashboard
def _empty_dashboard(error):
    return ([], {}, {"clients": 0, "products": 0, "subscriptions": 0, "by_status": {},
                     "mrr_paise": 0, "collected_paise": 0, "pending_paise": 0, "error": error})


@app.get("/admin", response_class=HTMLResponse)
def dashboard(request: Request):
    require_admin(request)
    try:
        # One concurrent round trip for all five tables instead of up to six
        # sequential ones (the previous version fetched clients/subscriptions
        # twice over, once for stats and again for the table) - this was the
        # actual reason the dashboard felt slow to load.
        clients, subs, plans, products, payments, tokens = store.fetch_many([
            dict(table="clients", limit=500),
            dict(table="subscriptions", limit=1000),
            dict(table="plans", limit=500),
            dict(table="products", limit=10000),
            dict(table="payments", limit=10000),
            dict(table="onboard_tokens", limit=1000),
        ])
    except Exception as exc:
        clients, plans_by_id, stats = _empty_dashboard("%s: %s" % (type(exc).__name__, exc))
    else:
        plans_by_id = {p["id"]: p for p in plans}
        by_client = {}
        by_status = {}
        mrr_paise = 0
        pending_paise = 0
        for s in subs:
            by_client.setdefault(s["client_id"], []).append(s)
            by_status[s["status"]] = by_status.get(s["status"], 0) + 1
            if s["status"] == "active":
                mrr_paise += s["amount_paise"]
            # "pending" here = money expected but not yet actually collected:
            # mandate not confirmed yet, or charges failing (halted). Distinct
            # from MRR, which is confirmed active revenue, and from collected,
            # which is what has actually landed.
            if STATUS_GROUPS.get(s["status"]) in ("pending", "declined"):
                pending_paise += s["amount_paise"]
        # A client with an old cancelled test plan AND a brand new mandate
        # still awaiting the client's own click used to show whichever
        # subscription happened to be most recently created - usually the
        # stale cancelled one, since the new mandate has no subscription row
        # at all yet (it is still just an onboard_tokens link). Sort each
        # client's own subs so a live one (anything not cancelled/completed/
        # expired) always wins the "which one do we show" pick, newest first
        # within that.
        TERMINAL = ("cancelled", "completed", "expired")
        for c in clients:
            c["subs"] = sorted(by_client.get(c["id"], []),
                               key=lambda s: (s["status"] in TERMINAL, ),
                               )
        # subs came back created_at.desc already (list_rows' default) and
        # Python's sort is stable, so the above only reorders live-vs-terminal
        # and leaves recency as the tiebreaker within each group, unchanged.
        pending_by_client = {}
        for t in tokens:
            if not t.get("subscription_id"):
                pending_by_client.setdefault(t["client_id"], []).append(t)
        for c in clients:
            c["pending_link"] = bool(pending_by_client.get(c["id"]))
        collected_paise = sum(p["amount_paise"] for p in payments if p["status"] == "captured")
        stats = {"clients": len(clients), "products": len(products), "subscriptions": len(subs),
                 "by_status": by_status, "mrr_paise": mrr_paise, "collected_paise": collected_paise,
                 "pending_paise": pending_paise, "error": None}
    return T.TemplateResponse("dashboard.html", ctx(
        request, stats=stats, clients=clients, plans_by_id=plans_by_id))


# ------------------------------------------------------------------ clients
@app.get("/admin/clients/new", response_class=HTMLResponse)
def client_new_form(request: Request):
    require_admin(request)
    plans = [p for p in store.list_rows("plans", order="amount_paise.asc")
             if p["active"] and not p.get("client_id")]
    return T.TemplateResponse("client_new.html", ctx(request, plans=plans))


@app.post("/admin/clients")
def client_create(request: Request, name: str = Form(...), email: str = Form(""),
                  phone: str = Form(""), company: str = Form(""), notes: str = Form(""),
                  product_name: str = Form(""), product_url: str = Form(""),
                  plan_id: str = Form(""), custom_amount: str = Form("")):
    """Client, product and onboarding link in one submit. Doing this in three
    round trips through three screens was the slow part of onboarding
    somebody - not the typing."""
    require_admin(request)
    row = store.insert("clients", {"name": name.strip(), "email": email.strip(),
                                   "phone": phone.strip(), "company": company.strip(),
                                   "notes": notes.strip(), "status": "active"})
    product = None
    if product_name.strip():
        product = store.insert("products", {"client_id": row["id"], "name": product_name.strip(),
                                            "url": product_url.strip(), "notes": ""})

    # Optional: mint the onboarding link straight away.
    chosen = None
    if (custom_amount or "").strip():
        try:
            amount = int(float(custom_amount))
        except ValueError:
            amount = 0
        if amount >= 1:
            plan_row = {"name": "Custom — %s" % row["name"][:40], "amount_paise": amount * 100,
                        "interval": "monthly", "active": 0}
            if store.supports("plans", "client_id"):
                plan_row["client_id"] = row["id"]
            chosen = store.insert("plans", plan_row)
    elif plan_id:
        chosen = store.get("plans", plan_id)

    if chosen:
        token = uuid.uuid4().hex
        store.insert("onboard_tokens", {"client_id": row["id"], "plan_id": chosen["id"],
                                        "product_id": (product or {}).get("id"), "token": token})
        return RedirectResponse("/admin/clients/%s?link=%s" % (row["id"], token), status_code=303)
    return RedirectResponse("/admin/clients/%s" % row["id"], status_code=303)


@app.get("/admin/clients/{client_id}", response_class=HTMLResponse)
def client_detail(request: Request, client_id: str, link: str = None):
    require_admin(request)
    client = store.get("clients", client_id)
    if not client:
        raise HTTPException(404, "No such client.")
    # One concurrent batch. This page used to issue a query per subscription
    # just to collect payments, on top of four more in sequence - the slowest
    # screen in the app, and the one opened most often.
    products, subs, all_plans, all_tokens, all_payments = store.fetch_many([
        dict(table="products", where={"client_id": client_id}, order="created_at.asc"),
        dict(table="subscriptions", where={"client_id": client_id}),
        dict(table="plans", order="amount_paise.asc"),
        dict(table="onboard_tokens", where={"client_id": client_id}),
        dict(table="payments", limit=10000),
    ])
    sub_ids = {s["id"] for s in subs}
    payments = sorted((p for p in all_payments if p["subscription_id"] in sub_ids),
                      key=lambda p: p["created_at"], reverse=True)
    plans_by_id = {p["id"]: p for p in all_plans}
    # Custom plans belong to one client and never appear in anyone else's picker.
    active_plans = [p for p in all_plans
                    if p["active"] and (p.get("client_id") or client_id) == client_id]
    custom_plans = [p for p in all_plans if p.get("client_id") == client_id]

    # Links already sent but not yet acted on: a link generated here does NOT
    # create a subscription row - that only happens once the client actually
    # opens it and starts checkout. Without this, the operator has no way to
    # tell "already sent, waiting on them" from "never sent" and the plan
    # picker below just resets to the cheapest plan every time, looking like
    # nothing happened.
    base_url = str(request.base_url).rstrip("/")
    pending_links = [dict(t, plan=plans_by_id.get(t["plan_id"]),
                          url="%s/subscribe/%s" % (base_url, t["token"]))
                     for t in all_tokens if not t.get("subscription_id")]
    pending_links.sort(key=lambda t: t["created_at"], reverse=True)

    link_url = None
    if link:
        link_url = "%s/subscribe/%s" % (base_url, link)
    return T.TemplateResponse("client_detail.html", ctx(
        request, client=client, products=products, subs=subs, payments=payments,
        plans=active_plans, custom_plans=custom_plans, plans_by_id=plans_by_id,
        link_url=link_url, pending_links=pending_links,
        sent=request.query_params.get("sent")))


@app.post("/admin/clients/{client_id}/edit")
def client_edit(request: Request, client_id: str, name: str = Form(...), email: str = Form(""),
                phone: str = Form(""), company: str = Form(""), notes: str = Form(""),
                status: str = Form("active")):
    require_admin(request)
    store.patch("clients", client_id, {"name": name.strip(), "email": email.strip(),
                                       "phone": phone.strip(), "company": company.strip(),
                                       "notes": notes.strip(), "status": status})
    return RedirectResponse("/admin/clients/%s" % client_id, status_code=303)


@app.post("/admin/clients/{client_id}/products")
def product_add(request: Request, client_id: str, name: str = Form(...), url: str = Form("")):
    require_admin(request)
    store.insert("products", {"client_id": client_id, "name": name.strip(), "url": url.strip(), "notes": ""})
    return RedirectResponse("/admin/clients/%s" % client_id, status_code=303)


@app.post("/admin/clients/{client_id}/custom-plan")
def create_custom_plan(request: Request, client_id: str, amount_rupees: int = Form(...),
                       name: str = Form(""), product_id: str = Form("")):
    """A one-off price agreed with a single client. Stored as a plan row like
    any other - Razorpay needs a Plan object per amount either way - but
    inactive and tagged to this client, so it stays out of the shared plan
    list, every other client's picker, and the public homepage."""
    require_admin(request)
    client = store.get("clients", client_id)
    if not client:
        raise HTTPException(404, "No such client.")
    if amount_rupees < 1:
        raise HTTPException(400, "A plan amount has to be at least ₹1.")
    row = {"name": (name.strip() or "Custom — %s" % client["name"])[:60],
           "amount_paise": int(amount_rupees) * 100, "interval": "monthly", "active": 0}
    if store.supports("plans", "client_id"):
        row["client_id"] = client_id
    plan = store.insert("plans", row)

    token = uuid.uuid4().hex
    store.insert("onboard_tokens", {"client_id": client_id, "plan_id": plan["id"],
                                    "product_id": product_id or None, "token": token})
    return RedirectResponse("/admin/clients/%s?link=%s" % (client_id, token), status_code=303)


@app.post("/admin/clients/{client_id}/delete")
def client_delete(request: Request, client_id: str):
    """Remove a client and everything hanging off them. Any live Razorpay
    subscription is cancelled FIRST - deleting our row would otherwise leave
    Razorpay happily charging their card every month with nothing on this
    side to show for it."""
    require_admin(request)
    client = store.get("clients", client_id)
    if not client:
        raise HTTPException(404, "No such client.")

    subs = store.list_rows("subscriptions", where={"client_id": client_id})
    for s in subs:
        if s.get("rzp_subscription_id") and s["status"] not in ("cancelled", "completed", "expired") \
                and settings.razorpay_ready():
            try:
                rzp.cancel_subscription(settings.razorpay_key_id(), settings.razorpay_key_secret(),
                                        s["rzp_subscription_id"])
            except rzp.RzpError as exc:
                print("delete client: could not cancel %s: %s" % (s["rzp_subscription_id"], exc))
        for p in store.list_rows("payments", where={"subscription_id": s["id"]}):
            store.delete("payments", p["id"])
        store.delete("subscriptions", s["id"])
    for t in store.list_rows("onboard_tokens", where={"client_id": client_id}):
        store.delete("onboard_tokens", t["id"])
    for p in store.list_rows("products", where={"client_id": client_id}):
        store.delete("products", p["id"])
    if store.supports("plans", "client_id"):
        for p in store.list_rows("plans", where={"client_id": client_id}):
            store.delete("plans", p["id"])
    store.delete("clients", client_id)
    return RedirectResponse("/admin?deleted=%s" % client["name"][:40], status_code=303)


@app.post("/admin/clients/{client_id}/link")
def create_link(request: Request, client_id: str, plan_id: str = Form(...),
                product_id: str = Form("")):
    require_admin(request)
    if not store.get("clients", client_id):
        raise HTTPException(404, "No such client.")
    token = uuid.uuid4().hex
    store.insert("onboard_tokens", {"client_id": client_id, "plan_id": plan_id,
                                    "product_id": product_id or None, "token": token})
    return RedirectResponse("/admin/clients/%s?link=%s" % (client_id, token), status_code=303)


@app.post("/admin/clients/{client_id}/send-link")
def send_link(request: Request, client_id: str, token: str = Form(...), channel: str = Form(...)):
    require_admin(request)
    client = store.get("clients", client_id)
    row = store.get_by("onboard_tokens", "token", token)
    plan = store.get("plans", row["plan_id"]) if row else None
    if not (client and row and plan):
        raise HTTPException(404, "No such link.")
    url = "%s/subscribe/%s" % (str(request.base_url).rstrip("/"), token)
    product = store.get("products", row["product_id"]) if row.get("product_id") else None
    amount = rupees(plan["amount_paise"])
    if channel == "whatsapp":
        ok, info = notify.send_whatsapp(client["phone"], notify.subscription_link_message(
            client["name"], settings.business_name(), plan["name"], amount, url))
    else:
        subject, text, html = emails.subscription_invite(
            client["name"], plan["name"], amount, url,
            product_name=(product or {}).get("name"))
        ok, info = notify.send_email(client["email"], subject, text, html=html)
    return RedirectResponse("/admin/clients/%s?link=%s&sent=%s" % (client_id, token, "1" if ok else "0"),
                            status_code=303)


@app.post("/admin/subscriptions/{sub_id}/cancel")
def subscription_cancel(request: Request, sub_id: str, reason: str = Form("")):
    require_admin(request)
    sub = store.get("subscriptions", sub_id)
    if not sub:
        raise HTTPException(404, "No such subscription.")
    if sub.get("rzp_subscription_id") and settings.razorpay_ready():
        try:
            rzp.cancel_subscription(settings.razorpay_key_id(), settings.razorpay_key_secret(),
                                    sub["rzp_subscription_id"])
        except rzp.RzpError:
            pass  # webhook will reconcile; local cancel below still records intent
    store.patch("subscriptions", sub_id, {"status": "cancelled",
                                          "cancelled_at": datetime.utcnow().isoformat(timespec="seconds"),
                                          "cancel_reason": reason.strip()})
    return RedirectResponse("/admin/clients/%s" % sub["client_id"], status_code=303)


@app.post("/admin/subscriptions/{sub_id}/delete")
def subscription_delete(request: Request, sub_id: str):
    """Remove a subscription record that is already over - a cancelled test
    plan, a completed/expired one - not a live one. Restricted to terminal
    statuses on purpose: this deletes the LOCAL row only, never touches
    Razorpay, so deleting an active or pending subscription would just hide
    a mandate that is still charging the client every month with nothing
    left here to show for it. Cancel it first (which does call Razorpay),
    then delete.

    crm_payments.subscription_id references this row ON DELETE CASCADE, so
    any payment history tied to it goes with it - fine for the test/dummy
    subscriptions this exists to clean up, which is why the template's
    confirm() names that cost before the click."""
    require_admin(request)
    sub = store.get("subscriptions", sub_id)
    if not sub:
        raise HTTPException(404, "No such subscription.")
    if sub["status"] not in ("cancelled", "completed", "expired"):
        raise HTTPException(400, "Cancel this subscription before deleting it.")
    store.delete("subscriptions", sub_id)
    return RedirectResponse("/admin/clients/%s" % sub["client_id"], status_code=303)


# --------------------------------------------------------------------- plans
@app.get("/admin/plans", response_class=HTMLResponse)
def plans_list(request: Request):
    require_admin(request)
    # Per-client custom plans are deliberately absent: they belong to one
    # client's page, not to the shared price list.
    plans = [p for p in store.list_rows("plans", order="amount_paise.asc")
             if not p.get("client_id")]
    # A plan that has ever been subscribed to (even a cancelled test sub)
    # can't be hard-deleted - historical payments and invoices still show
    # its name via plans_by_id lookups, so removing the row would blank
    # those out. Disable is always safe; delete only offered when neither
    # a subscription nor a still-outstanding invite link points at it.
    subs, tokens = store.fetch_many([
        dict(table="subscriptions", limit=2000),
        dict(table="onboard_tokens", limit=2000),
    ])
    used_ids = {s["plan_id"] for s in subs} | {t["plan_id"] for t in tokens}
    for p in plans:
        p["deletable"] = p["id"] not in used_ids
    active_plans = [p for p in plans if p["active"]]
    disabled_plans = [p for p in plans if not p["active"]]
    return T.TemplateResponse("plans.html", ctx(
        request, active_plans=active_plans, disabled_plans=disabled_plans))


@app.post("/admin/plans")
def plan_create(request: Request, name: str = Form(...), amount_rupees: int = Form(...)):
    require_admin(request)
    store.insert("plans", {"name": name.strip(), "amount_paise": int(amount_rupees) * 100,
                           "interval": "monthly", "active": 1})
    return RedirectResponse("/admin/plans", status_code=303)


@app.post("/admin/plans/{plan_id}/edit")
def plan_edit(request: Request, plan_id: str, name: str = Form(...), amount_rupees: int = Form(...)):
    require_admin(request)
    plan = store.get("plans", plan_id)
    if not plan:
        raise HTTPException(404, "No such plan.")
    fields = {"name": name.strip()}
    new_paise = int(amount_rupees) * 100
    if new_paise != plan["amount_paise"]:
        fields["amount_paise"] = new_paise
        # Razorpay plans are immutable once created - see _ensure_plan_synced.
        # Clearing this makes the next checkout mint a fresh Razorpay plan at
        # the new price instead of silently reusing the old one. Anyone
        # already subscribed keeps their own already-created mandate/price;
        # this only changes what a NEW subscriber on this plan pays.
        fields["rzp_plan_id"] = None
    store.patch("plans", plan_id, fields)
    return RedirectResponse("/admin/plans", status_code=303)


@app.post("/admin/plans/{plan_id}/toggle")
def plan_toggle(request: Request, plan_id: str):
    require_admin(request)
    plan = store.get("plans", plan_id)
    if plan:
        store.patch("plans", plan_id, {"active": 0 if plan["active"] else 1})
    return RedirectResponse("/admin/plans", status_code=303)


@app.post("/admin/plans/{plan_id}/delete")
def plan_delete(request: Request, plan_id: str):
    require_admin(request)
    plan = store.get("plans", plan_id)
    if not plan:
        raise HTTPException(404, "No such plan.")
    used = (store.list_rows("subscriptions", where={"plan_id": plan_id}, limit=1)
            or store.list_rows("onboard_tokens", where={"plan_id": plan_id}, limit=1))
    if used:
        raise HTTPException(400, "This plan has subscribers or an outstanding invite link - "
                                  "disable it instead of deleting.")
    store.delete("plans", plan_id)
    return RedirectResponse("/admin/plans", status_code=303)


# ----------------------------------------------------------------- showcase
@app.get("/admin/showcase", response_class=HTMLResponse)
def showcase_list(request: Request):
    require_admin(request)
    try:
        items = store.list_rows("showcase", order="created_at.desc", limit=200)
        problem = None
    except Exception as exc:
        items = []
        problem = "%s: %s" % (type(exc).__name__, exc)
    return T.TemplateResponse("showcase.html", ctx(request, items=items, problem=problem))


@app.post("/admin/showcase")
def showcase_create(request: Request, name: str = Form(...), url: str = Form(...),
                    description: str = Form("")):
    require_admin(request)
    store.insert("showcase", {"name": name.strip(), "url": url.strip(),
                              "description": description.strip(), "active": 1})
    return RedirectResponse("/admin/showcase", status_code=303)


@app.post("/admin/showcase/{item_id}/edit")
def showcase_edit(request: Request, item_id: str, name: str = Form(...), url: str = Form(...),
                  description: str = Form("")):
    require_admin(request)
    if not store.get("showcase", item_id):
        raise HTTPException(404, "No such showcase entry.")
    store.patch("showcase", item_id, {"name": name.strip(), "url": url.strip(),
                                      "description": description.strip()})
    return RedirectResponse("/admin/showcase", status_code=303)


@app.post("/admin/showcase/{item_id}/toggle")
def showcase_toggle(request: Request, item_id: str):
    require_admin(request)
    item = store.get("showcase", item_id)
    if item:
        store.patch("showcase", item_id, {"active": 0 if item["active"] else 1})
    return RedirectResponse("/admin/showcase", status_code=303)


@app.post("/admin/showcase/{item_id}/delete")
def showcase_delete(request: Request, item_id: str):
    require_admin(request)
    store.delete("showcase", item_id)
    return RedirectResponse("/admin/showcase", status_code=303)


IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
MAX_IMAGE_BYTES = 6 * 1024 * 1024


@app.post("/admin/showcase/{item_id}/image")
async def showcase_image(request: Request, item_id: str, file: UploadFile = File(...)):
    """A manually uploaded preview - replaces the auto-generated screenshot,
    which some sites never rendered for (behind auth, slow to respond, or
    just never got captured). No "generating preview" wait, no dependence
    on a third party's screenshot service at all once one is uploaded."""
    require_admin(request)
    if not store.get("showcase", item_id):
        raise HTTPException(404, "No such showcase entry.")
    if not store.supports("showcase", "image_url"):
        raise HTTPException(400, "Run supabase/migrations/0004_showcase_image.sql first, "
                                  "then reload this page.")
    ext = IMAGE_TYPES.get(file.content_type)
    if not ext:
        raise HTTPException(400, "Please upload a JPEG, PNG, WEBP or GIF image.")
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "That image is over 6MB - please use a smaller file.")
    url = store.save_showcase_image(item_id, ext, file.content_type, data)
    store.patch("showcase", item_id, {"image_url": url})
    return RedirectResponse("/admin/showcase", status_code=303)


# ------------------------------------------------------------------ settings
@app.get("/admin/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = None, tested: str = None):
    require_admin(request)
    return T.TemplateResponse("settings.html", ctx(
        request, groups=settings.GROUPS, rows={r["key"]: r for r in settings.rows()},
        problem=settings.store_problem(), enc_problem=settings.encryption_problem(),
        saved=saved, tested=tested))


@app.post("/admin/settings/save")
def settings_save(request: Request, key: str = Form(...), value: str = Form("")):
    require_admin(request)
    if key not in settings.FIELDS:
        raise HTTPException(400, "Unknown setting.")
    try:
        if value.strip():
            settings.put(key, value)
        else:
            settings.clear(key)
    except ValueError:
        pass
    return RedirectResponse("/admin/settings?saved=%s" % key, status_code=303)


@app.post("/admin/settings/clear")
def settings_clear(request: Request, key: str = Form(...)):
    require_admin(request)
    settings.clear(key)
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/settings/test-razorpay")
def settings_test_razorpay(request: Request):
    require_admin(request)
    ok, msg = rzp.test_connection(settings.razorpay_key_id(), settings.razorpay_key_secret())
    return RedirectResponse("/admin/settings?tested=%s" % ("ok" if ok else "fail"), status_code=303)


# ------------------------------------------------------------ public checkout
def _ensure_plan_synced(plan):
    """Lazily create the Razorpay Plan the first time it is needed, and cache
    the id. Razorpay plans cannot be edited - a changed price on our side
    means the row's rzp_plan_id is cleared so the next checkout mints a new
    one, rather than silently reusing a stale price."""
    if plan.get("rzp_plan_id"):
        return plan["rzp_plan_id"]
    created = rzp.create_plan(settings.razorpay_key_id(), settings.razorpay_key_secret(),
                              plan["name"], plan["amount_paise"], plan["interval"])
    store.patch("plans", plan["id"], {"rzp_plan_id": created["id"]})
    return created["id"]


@app.get("/subscribe/{token}", response_class=HTMLResponse)
def subscribe_page(request: Request, token: str):
    row = store.get_by("onboard_tokens", "token", token)
    if not row:
        raise HTTPException(404, "This link is not valid.")
    client = store.get("clients", row["client_id"])
    plan = store.get("plans", row["plan_id"])
    if not (client and plan):
        raise HTTPException(404, "This link is not valid.")
    product = store.get("products", row["product_id"]) if row.get("product_id") else None
    return T.TemplateResponse("subscribe.html", ctx(
        request, client=client, plan=plan, product=product, token=token,
        rzp_key_id=settings.razorpay_key_id()))


@app.post("/subscribe/{token}/start")
def subscribe_start(token: str):
    row = store.get_by("onboard_tokens", "token", token)
    if not row:
        raise HTTPException(404, "This link is not valid.")
    client = store.get("clients", row["client_id"])
    plan = store.get("plans", row["plan_id"])
    if not (client and plan):
        raise HTTPException(404, "This link is not valid.")
    if not settings.razorpay_ready():
        raise HTTPException(503, "Payments are not configured yet. Please contact us directly.")

    # Reuse a still-open subscription for this token rather than minting a
    # second mandate if the client reloads the page mid-checkout.
    if row.get("subscription_id"):
        existing = store.get("subscriptions", row["subscription_id"])
        if existing and existing["status"] in ("created", "authenticated"):
            return JSONResponse({"subscription_id": existing["rzp_subscription_id"],
                                 "key_id": settings.razorpay_key_id(),
                                 "name": settings.business_name(), "plan_name": plan["name"],
                                 "amount": plan["amount_paise"],
                                 "prefill": {"name": client["name"], "email": client["email"] or "",
                                             "contact": client["phone"] or ""}})

    try:
        plan_id = _ensure_plan_synced(plan)
        sub_row = store.insert("subscriptions", {
            "client_id": client["id"], "product_id": row.get("product_id"),
            "plan_id": plan["id"], "status": "created", "amount_paise": plan["amount_paise"]})
        created = rzp.create_subscription(
            settings.razorpay_key_id(), settings.razorpay_key_secret(), plan_id,
            notes={"row_id": sub_row["id"], "client_id": client["id"]})
        store.patch("subscriptions", sub_row["id"], {"rzp_subscription_id": created["id"],
                                                      "short_url": created.get("short_url", "")})
        store.patch("onboard_tokens", row["id"], {"subscription_id": sub_row["id"]})
    except rzp.RzpError as exc:
        raise HTTPException(502, str(exc))

    return JSONResponse({"subscription_id": created["id"], "key_id": settings.razorpay_key_id(),
                         "name": settings.business_name(), "plan_name": plan["name"],
                         "amount": plan["amount_paise"],
                         "prefill": {"name": client["name"], "email": client["email"] or "",
                                     "contact": client["phone"] or ""}})


@app.get("/subscribe/{token}/done", response_class=HTMLResponse)
def subscribe_done(request: Request, token: str):
    row = store.get_by("onboard_tokens", "token", token)
    client = store.get("clients", row["client_id"]) if row else None
    plan = store.get("plans", row["plan_id"]) if row else None
    return T.TemplateResponse("subscribe_done.html", ctx(
        request, client=client, plan=plan, token=token))


def _token_payment(token):
    """(subscription, latest captured payment) behind an onboarding token, or
    (None, None). Both the status poll and the invoice download go through
    this, so the token is the single thing that grants access to either."""
    row = store.get_by("onboard_tokens", "token", token)
    if not row or not row.get("subscription_id"):
        return None, None
    sub = store.get("subscriptions", row["subscription_id"])
    if not sub:
        return None, None
    paid = [p for p in store.list_rows("payments", where={"subscription_id": sub["id"]})
            if p["status"] == "captured"]
    paid.sort(key=lambda p: p["created_at"], reverse=True)
    return sub, (paid[0] if paid else None)


@app.get("/subscribe/{token}/status")
def subscribe_status(token: str):
    """Polled by the confirmation page. Razorpay's webhook lands a second or
    two after the browser returns, so the page opens in a "confirming"
    state and this is what lets it settle into a real confirmation instead
    of claiming success before the money is actually recorded."""
    try:
        sub, payment = _token_payment(token)
    except Exception:
        return JSONResponse({"state": "pending"})
    if not sub:
        return JSONResponse({"state": "pending"})
    if payment:
        return JSONResponse({"state": "paid",
                             "invoice_no": invoice.invoice_number(payment),
                             "invoice_url": "/invoice/%s" % payment["id"],
                             "amount": rupees(payment["amount_paise"])})
    if sub["status"] in ("cancelled", "expired"):
        return JSONResponse({"state": "cancelled"})
    return JSONResponse({"state": "pending"})


@app.get("/invoice/{payment_id}")
def invoice_download(payment_id: str):
    """The PDF, by payment id. The id is a 16-character random hex - the same
    unguessable-link model the onboarding token uses - because the client
    receiving the invoice has no account here to log into."""
    payment = store.get("payments", payment_id)
    if not payment or payment["status"] != "captured":
        raise HTTPException(404, "No invoice found for that payment.")
    sub = store.get("subscriptions", payment["subscription_id"])
    client = store.get("clients", sub["client_id"]) if sub else None
    plan = store.get("plans", sub["plan_id"]) if sub else None
    if not client:
        raise HTTPException(404, "No invoice found for that payment.")
    pdf = invoice.build_invoice_pdf(payment, client, plan)
    inv_no = invoice.invoice_number(payment)
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": 'inline; filename="%s.pdf"' % inv_no})


# ----------------------------------------------------------------- webhook
@app.post("/webhook/razorpay")
async def webhook_razorpay(request: Request):
    body = await request.body()
    sig = request.headers.get("X-Razorpay-Signature", "")
    secret = settings.razorpay_webhook_secret()
    if not secret:
        raise HTTPException(503, "Webhook secret not configured.")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(400, "Bad signature.")

    event = json.loads(body)
    kind = event.get("event", "")
    payload = event.get("payload", {})

    if kind.startswith("subscription."):
        _handle_subscription_event(kind, payload.get("subscription", {}).get("entity", {}),
                                   payload.get("payment", {}).get("entity", {}))
    elif kind == "payment.failed":
        _handle_payment_failed(payload.get("payment", {}).get("entity", {}))

    return JSONResponse({"ok": True})


def _row_for_rzp_sub(rzp_sub):
    row_id = (rzp_sub.get("notes") or {}).get("row_id")
    sub = store.get("subscriptions", row_id) if row_id else None
    if not sub:
        sub = store.get_by("subscriptions", "rzp_subscription_id", rzp_sub.get("id", ""))
    return sub


STATUS_MAP = {
    "created": "created", "authenticated": "authenticated", "active": "active",
    "pending": "pending", "halted": "halted", "cancelled": "cancelled",
    "completed": "completed", "expired": "expired",
}


def _send_payment_receipt(client, plan, payment, sub=None):
    """WhatsApp gets a short text notification; email gets the branded
    receipt with the actual PDF invoice attached, since WhatsApp text can't
    carry a document here. Both are attempted independently - one failing
    must not skip the other, and neither failing may break webhook
    processing, so every step is guarded."""
    amount_str = rupees(payment["amount_paise"])
    inv_no = invoice.invoice_number(payment)
    try:
        notify.send_whatsapp(client["phone"], notify.payment_receipt_message(
            client["name"], settings.business_name(), plan["name"], amount_str, inv_no))
    except Exception as exc:
        print("payment receipt: whatsapp send failed: %s" % exc)

    try:
        pdf = invoice.build_invoice_pdf(payment, client, plan)
        next_billing = (sub or {}).get("next_billing_at")
        site = settings.site_url()
        subject, text, html = emails.payment_receipt(
            client["name"], plan["name"], amount_str, inv_no,
            next_billing=next_billing[:10] if next_billing else None,
            invoice_url=("%s/invoice/%s" % (site, payment["id"])) if site else None)
        notify.send_email_with_attachment(client["email"], subject, text, pdf,
                                          "%s.pdf" % inv_no, html=html)
    except Exception as exc:
        print("payment receipt: invoice email failed: %s" % exc)


def _handle_subscription_event(kind, rzp_sub, rzp_payment=None):
    sub = _row_for_rzp_sub(rzp_sub)
    if not sub:
        return
    rzp_status = rzp_sub.get("status", "")
    patch = {}
    if rzp_status in STATUS_MAP:
        patch["status"] = STATUS_MAP[rzp_status]
    charge_at = rzp_sub.get("charge_at")
    if charge_at:
        patch["next_billing_at"] = datetime.utcfromtimestamp(charge_at).isoformat(timespec="seconds")
    if kind == "subscription.activated" and not sub.get("started_at"):
        patch["started_at"] = datetime.utcnow().isoformat(timespec="seconds")
    if patch:
        store.patch("subscriptions", sub["id"], patch)

    client = store.get("clients", sub["client_id"])
    plan = store.get("plans", sub["plan_id"])
    if not (client and plan):
        return

    if kind == "subscription.charged" and rzp_payment and rzp_payment.get("id"):
        # Idempotent: the unique index on payments.rzp_payment_id would raise on
        # a Razorpay retry-delivery of the same webhook, so check first.
        if not store.get_by("payments", "rzp_payment_id", rzp_payment["id"]):
            payment = store.insert("payments", {
                "subscription_id": sub["id"], "rzp_payment_id": rzp_payment["id"],
                "amount_paise": rzp_payment.get("amount", plan["amount_paise"]),
                "status": "captured", "method": rzp_payment.get("method", ""), "notes": ""})
            _send_payment_receipt(client, plan, payment, dict(sub, **patch))

    if kind == "subscription.halted":
        # Both channels, not one-or-the-other: a failing payment is the one
        # message a client must not miss because a single provider hiccuped.
        try:
            notify.send_whatsapp(client["phone"], notify.payment_failed_message(
                client["name"], settings.business_name(), plan["name"],
                settings.support_email(), settings.support_phone()))
        except Exception as exc:
            print("payment failed notice: whatsapp send failed: %s" % exc)
        try:
            subject, text, html = emails.payment_failed(client["name"], plan["name"])
            notify.send_email(client["email"], subject, text, html=html)
        except Exception as exc:
            print("payment failed notice: email failed: %s" % exc)


def _handle_payment_failed(rzp_payment):
    sub_id = None
    notes = rzp_payment.get("notes") or {}
    if notes.get("row_id"):
        sub_id = notes["row_id"]
    if not sub_id:
        return
    sub = store.get("subscriptions", sub_id)
    if not sub:
        return
    store.insert("payments", {"subscription_id": sub["id"], "rzp_payment_id": rzp_payment.get("id"),
                              "amount_paise": rzp_payment.get("amount", 0), "status": "failed",
                              "method": rzp_payment.get("method", ""),
                              "notes": (rzp_payment.get("error_description") or "")[:300]})


# --------------------------------------------------------------- legal
# Required before any real (non-test) Razorpay payment is collected: Razorpay
# checks that Terms, Privacy and a Refund/Cancellation policy are reachable
# from the page that actually takes payment (/subscribe/{token}, linked in
# its footer). Public, unauthenticated - a prospective client reads these
# before they've signed in to anything.
LEGAL_UPDATED = "7 September 2026"


def legal_ctx(request: Request, **extra):
    base = dict(request=request, business=settings.business_name(),
               support_email=settings.support_email(), support_phone=settings.support_phone(),
               updated=LEGAL_UPDATED)
    base.update(extra)
    return base


@app.get("/terms", response_class=HTMLResponse)
def legal_terms(request: Request):
    return T.TemplateResponse("legal-terms.html", legal_ctx(request))


@app.get("/privacy", response_class=HTMLResponse)
def legal_privacy(request: Request):
    return T.TemplateResponse("legal-privacy.html", legal_ctx(request))


@app.get("/refund", response_class=HTMLResponse)
def legal_refund(request: Request):
    return T.TemplateResponse("legal-refund.html", legal_ctx(request))


@app.get("/contact", response_class=HTMLResponse)
def legal_contact(request: Request):
    return T.TemplateResponse("legal-contact.html", legal_ctx(request))


# --------------------------------------------------------------- homepage
def _wa_link():
    """A wa.me link from the configured support phone, or a mailto: fallback,
    or a link to /contact if neither is set - the CTA must always go
    somewhere, never a dead '#'."""
    phone = settings.support_phone()
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())
        return "https://wa.me/%s?text=%s" % (
            digits, "Hi%2C%20I%27d%20like%20to%20talk%20about%20building%20something.")
    if settings.support_email():
        return "mailto:%s" % settings.support_email()
    return "/contact"


def _plan_groups(plans):
    """Personal/Business columns for the homepage, each with an optional Plus
    tier - purely by name (no schema column for this), since the admin
    already names them this way and there are only ever a handful of
    curated shared plans. First match wins per bucket if there's ever a
    duplicate; plans is already amount_paise-ascending."""
    groups = {"personal": None, "personal_plus": None, "business": None, "business_plus": None}
    for p in plans:
        name = p["name"].strip().lower()
        key = ("business" if name.startswith("business") else "personal") + \
              ("_plus" if name.endswith("plus") else "")
        if groups.get(key) is None:
            groups[key] = p
    return groups


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    try:
        plans = [p for p in store.list_rows("plans", order="amount_paise.asc", limit=20)
                 if p["active"] and not p.get("client_id")]
    except Exception:
        plans = []  # the homepage must render even if the database is unreachable
    try:
        showcase = [s for s in store.list_rows("showcase", order="created_at.desc", limit=50)
                    if s["active"]]
    except Exception:
        showcase = []  # degrades to no "our work" section until 0003_showcase.sql is run
    return T.TemplateResponse("home.html", dict(
        request=request, business=settings.business_name(), support_email=settings.support_email(),
        wa_link=_wa_link(), plans=plans, plan_groups=_plan_groups(plans), showcase=showcase,
        year=datetime.utcnow().year))


@app.get("/healthz")
def healthz():
    try:
        store.init()
        store_ok = True
    except Exception as exc:
        store_ok = str(exc)[:200]
    return {"ok": store_ok is True, "store": store_ok, "backend": store.backend(),
           "razorpay_ready": settings.razorpay_ready(), "razorpay_mode": settings.razorpay_mode(),
           "whatsapp_ready": settings.whatsapp_ready(), "email_ready": settings.email_ready(),
           "admin_configured": auth.configured()}
