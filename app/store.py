# -*- coding: utf-8 -*-
"""
Data store: clients, products, plans, subscriptions, payments, onboarding
links and the settings vault - one SQLite file, or Supabase Postgres when
SUPABASE_URL and SUPABASE_SERVICE_KEY are set. Same trade sadhna-astro's
web/store.py makes: identical schema either way, so switching is an
environment change, not a code change.

Generic table helpers (insert/patch/get/list/delete) instead of one function
per table - there are six tables here doing the same shape of work, and a
per-table copy of the same eight lines six times over is not a real
abstraction, it is a chance for one of the six to drift.
"""
import os, sqlite3, uuid, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or ""
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)

# This app is expected to live in a Supabase project shared with other small
# tools (a "Nevorai Tools" style project, one Postgres per family of tools
# rather than one per tool). "clients", "payments", "plans" etc. are exactly
# the table names another tool in the same project is likely to also want -
# so every table this module touches is prefixed, in both backends, to stay
# out of the way of anything else living in the same database.
TABLE_PREFIX = os.environ.get("CRM_TABLE_PREFIX", "crm_")


def _t(name):
    return TABLE_PREFIX + name


DB = os.environ.get("CRM_DB",
                    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "data", "crm.db"))
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS {p}clients (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  name TEXT NOT NULL, email TEXT, phone TEXT, company TEXT, notes TEXT,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS {p}products (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  client_id TEXT NOT NULL, name TEXT NOT NULL, url TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS {p}plans (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  name TEXT NOT NULL, amount_paise INTEGER NOT NULL,
  interval TEXT NOT NULL DEFAULT 'monthly',
  rzp_plan_id TEXT, active INTEGER NOT NULL DEFAULT 1,
  client_id TEXT
);
CREATE TABLE IF NOT EXISTS {p}subscriptions (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  client_id TEXT NOT NULL, product_id TEXT, plan_id TEXT NOT NULL,
  rzp_subscription_id TEXT, status TEXT NOT NULL DEFAULT 'created',
  amount_paise INTEGER NOT NULL,
  next_billing_at TEXT, started_at TEXT, cancelled_at TEXT, cancel_reason TEXT,
  short_url TEXT
);
CREATE TABLE IF NOT EXISTS {p}payments (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  subscription_id TEXT NOT NULL, rzp_payment_id TEXT,
  amount_paise INTEGER NOT NULL, status TEXT NOT NULL, method TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS {p}onboard_tokens (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  client_id TEXT NOT NULL, plan_id TEXT NOT NULL, product_id TEXT,
  token TEXT UNIQUE NOT NULL, used_at TEXT, subscription_id TEXT
);
CREATE TABLE IF NOT EXISTS {p}app_settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {p}showcase (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
  name TEXT NOT NULL, url TEXT NOT NULL, description TEXT,
  active INTEGER NOT NULL DEFAULT 1, image_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_{p}products_client ON {p}products(client_id);
CREATE INDEX IF NOT EXISTS idx_{p}subs_client ON {p}subscriptions(client_id);
CREATE INDEX IF NOT EXISTS idx_{p}payments_sub ON {p}payments(subscription_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_{p}subs_rzp ON {p}subscriptions(rzp_subscription_id)
  WHERE rzp_subscription_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_{p}payments_rzp ON {p}payments(rzp_payment_id)
  WHERE rzp_payment_id IS NOT NULL;
"""

TABLES = ("clients", "products", "plans", "subscriptions", "payments", "onboard_tokens", "showcase")

# Columns added after the first release. CREATE TABLE IF NOT EXISTS never
# touches an existing table, so SQLite needs these applied by hand; Supabase
# gets them from supabase/migrations/, and supports() below keeps the app
# working in the window before that migration is run.
ADDED_COLUMNS = (("plans", "client_id", "TEXT"), ("showcase", "image_url", "TEXT"))


# ------------------------------------------------------------------ supabase
def _rest():
    import httpx
    return httpx.Client(
        base_url=SUPABASE_URL, timeout=20,
        headers={"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY,
                 "Content-Type": "application/json"})


def _sb(method, path, **kw):
    with _rest() as c:
        r = c.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError("supabase %s %s -> %s %s"
                               % (method, path, r.status_code, r.text[:300]))
        return r


# ------------------------------------------------------------------ sqlite
def _conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    if USE_SUPABASE:
        _sb("GET", "/rest/v1/%s" % _t("clients"), params={"select": "id", "limit": 1})  # fail loudly
        return
    with _lock, _conn() as c:
        c.executescript(SCHEMA.format(p=TABLE_PREFIX))
        for table, column, ddl in ADDED_COLUMNS:
            have = {r["name"] for r in c.execute("PRAGMA table_info(%s)" % _t(table))}
            if column not in have:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (_t(table), column, ddl))


_caps = {}


def supports(table, column):
    """Whether the live database actually has this column yet.

    SQLite self-migrates in init(), so it always does. Supabase needs its
    migration run by hand, and this is what stops the gap between "code
    deployed" and "SQL run" from turning every write into a 400: the caller
    drops the field and loses the refinement it powers, rather than failing
    outright. Cached per process - the answer only changes on a deploy or a
    migration, both of which restart it."""
    if not USE_SUPABASE:
        return True
    key = (table, column)
    if key not in _caps:
        try:
            _sb("GET", "/rest/v1/%s" % _t(table), params={"select": column, "limit": 1})
            _caps[key] = True
        except Exception:
            _caps[key] = False
    return _caps[key]


# ------------------------------------------------------------------ generic rows
def insert(table, row):
    table = _t(table)
    row = dict(row)
    row.setdefault("id", uuid.uuid4().hex[:16])
    row.setdefault("created_at", datetime.utcnow().isoformat(timespec="seconds"))
    if USE_SUPABASE:
        r = _sb("POST", "/rest/v1/%s" % table, json=row,
                headers={"Prefer": "return=representation"})
        return r.json()[0]
    with _lock, _conn() as c:
        c.execute("INSERT INTO %s (%s) VALUES (%s)"
                  % (table, ", ".join(row), ", ".join("?" * len(row))), list(row.values()))
    return row


def patch(table, row_id, fields):
    if not fields:
        return
    table = _t(table)
    if USE_SUPABASE:
        _sb("PATCH", "/rest/v1/%s" % table, params={"id": "eq." + row_id}, json=fields)
        return
    with _lock, _conn() as c:
        c.execute("UPDATE %s SET %s WHERE id = ?"
                  % (table, ", ".join("%s = ?" % k for k in fields)),
                  list(fields.values()) + [row_id])


def get(table, row_id):
    table = _t(table)
    if USE_SUPABASE:
        rows = _sb("GET", "/rest/v1/%s" % table,
                   params={"id": "eq." + row_id, "select": "*", "limit": 1}).json()
        return rows[0] if rows else None
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM %s WHERE id = ?" % table, (row_id,)).fetchone()
    return dict(r) if r else None


def get_by(table, column, value):
    table = _t(table)
    if USE_SUPABASE:
        rows = _sb("GET", "/rest/v1/%s" % table,
                   params={column: "eq." + value, "select": "*", "limit": 1}).json()
        return rows[0] if rows else None
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM %s WHERE %s = ?" % (table, column), (value,)).fetchone()
    return dict(r) if r else None


def list_rows(table, where=None, order="created_at.desc", limit=500):
    table = _t(table)
    where = where or {}
    if USE_SUPABASE:
        params = {"select": "*", "order": order.replace(".", "."), "limit": limit}
        for k, v in where.items():
            params[k] = "eq." + str(v)
        return _sb("GET", "/rest/v1/%s" % table, params=params).json()
    col, direction = order.split(".")
    sql = "SELECT * FROM %s" % table
    args = []
    if where:
        sql += " WHERE " + " AND ".join("%s = ?" % k for k in where)
        args = list(where.values())
    sql += " ORDER BY %s %s LIMIT ?" % (col, "DESC" if direction == "desc" else "ASC")
    args.append(limit)
    with _lock, _conn() as c:
        rs = c.execute(sql, args).fetchall()
    return [dict(r) for r in rs]


def fetch_many(specs):
    """Run several list_rows() calls concurrently and return their results in
    the same order. Supabase mode is dominated by per-request network
    latency, not CPU - a page needing four tables was paying for four
    sequential round trips for no reason. SQLite mode still runs them one
    after another under _lock, so this only helps where it actually can.

    specs: list of dicts, each the kwargs for one list_rows() call
    (must include "table"; where/order/limit optional)."""
    if not USE_SUPABASE or len(specs) <= 1:
        return [list_rows(**spec) for spec in specs]
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        return list(pool.map(lambda spec: list_rows(**spec), specs))


def delete(table, row_id):
    table = _t(table)
    if USE_SUPABASE:
        _sb("DELETE", "/rest/v1/%s" % table, params={"id": "eq." + row_id})
        return
    with _lock, _conn() as c:
        c.execute("DELETE FROM %s WHERE id = ?" % table, (row_id,))


# -------------------------------------------------------------------- images
# Admin-uploaded showcase screenshots. Supabase Storage in production (a
# bucket in the same project the rest of this app's data already lives in -
# no separate service to configure); a local static folder when running on
# SQLite, since there is no object storage to talk to there. Vercel's own
# filesystem is NOT used for this even when USE_SUPABASE is false - it is
# ephemeral per invocation, so this path only makes sense for local dev.
SHOWCASE_BUCKET = "showcase"
_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "app", "static", "uploads", "showcase")


def save_showcase_image(item_id, ext, content_type, data):
    """Store one image for a showcase entry, overwriting any previous image
    for that same id, and return the URL to save on the row."""
    ext = (ext or "jpg").lstrip(".").lower()
    if not USE_SUPABASE:
        os.makedirs(_UPLOAD_DIR, exist_ok=True)
        # Clear any previous extension for this id first, so switching from
        # e.g. a .png to a .jpg doesn't leave the old file behind forever.
        for old in os.listdir(_UPLOAD_DIR):
            if old.rsplit(".", 1)[0] == item_id:
                os.remove(os.path.join(_UPLOAD_DIR, old))
        path = os.path.join(_UPLOAD_DIR, "%s.%s" % (item_id, ext))
        with open(path, "wb") as f:
            f.write(data)
        return "/static/uploads/showcase/%s.%s" % (item_id, ext)

    import httpx
    object_path = "%s.%s" % (item_id, ext)
    headers = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY,
              "Content-Type": content_type or "application/octet-stream", "x-upsert": "true"}
    with httpx.Client(base_url=SUPABASE_URL, timeout=30) as c:
        r = c.post("/storage/v1/object/%s/%s" % (SHOWCASE_BUCKET, object_path),
                  headers=headers, content=data)
        # Supabase Storage answers a missing bucket with HTTP 400 (not 404) and
        # the real reason embedded in the JSON body - {"statusCode":"404",
        # "error":"Bucket not found","code":"NoSuchBucket"} - confirmed against
        # this app's own production error. Checking the wire status alone
        # (r.status_code == 404) never caught this; check the body instead.
        if r.status_code >= 400 and "NoSuchBucket" in r.text:
            # Bucket doesn't exist yet - create it (public, so the homepage
            # can hotlink the image with no signed-URL machinery) and retry
            # once, so there is no manual "create a bucket" step for anyone
            # setting this up.
            b = c.post("/storage/v1/bucket", headers={"apikey": SUPABASE_KEY,
                       "Authorization": "Bearer " + SUPABASE_KEY, "Content-Type": "application/json"},
                      json={"id": SHOWCASE_BUCKET, "name": SHOWCASE_BUCKET, "public": True})
            if b.status_code >= 400:
                print("showcase bucket create -> %s %s" % (b.status_code, b.text[:300]))
            r = c.post("/storage/v1/object/%s/%s" % (SHOWCASE_BUCKET, object_path),
                      headers=headers, content=data)
        if r.status_code >= 400:
            raise RuntimeError("Supabase Storage upload failed: %s %s" % (r.status_code, r.text[:300]))
    return "%s/storage/v1/object/public/%s/%s" % (SUPABASE_URL, SHOWCASE_BUCKET, object_path)


# ------------------------------------------------------------------ settings
# Same shape as sadhna-astro's web/store.py settings_* trio: this file owns
# only where the bytes sit, app/settings.py owns what they mean.
def settings_all():
    table = _t("app_settings")
    if USE_SUPABASE:
        rows = _sb("GET", "/rest/v1/%s" % table, params={"select": "key,value"}).json()
        return {r["key"]: r["value"] for r in rows}
    with _lock, _conn() as c:
        rs = c.execute("SELECT key, value FROM %s" % table).fetchall()
    return {r["key"]: r["value"] for r in rs}


def settings_put(key, value):
    table = _t("app_settings")
    now = datetime.utcnow().isoformat(timespec="seconds")
    if USE_SUPABASE:
        _sb("POST", "/rest/v1/%s" % table,
            json={"key": key, "value": value, "updated_at": now},
            headers={"Prefer": "resolution=merge-duplicates"})
        return
    with _lock, _conn() as c:
        c.execute("INSERT INTO %s (key, value, updated_at) VALUES (?, ?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                  "updated_at = excluded.updated_at" % table, (key, value, now))


def settings_delete(key):
    table = _t("app_settings")
    if USE_SUPABASE:
        _sb("DELETE", "/rest/v1/%s" % table, params={"key": "eq." + key})
        return
    with _lock, _conn() as c:
        c.execute("DELETE FROM %s WHERE key = ?" % table, (key,))


def backend():
    return "supabase" if USE_SUPABASE else "sqlite"
