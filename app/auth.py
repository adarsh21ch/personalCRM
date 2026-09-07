# -*- coding: utf-8 -*-
"""
Admin sign-in - one operator, one password. Every client's name, phone,
email and subscription status lives behind this, so it is a real sign-in
(PBKDF2-SHA256 password hash, rate-limited, HMAC-signed session cookie),
not a token in a URL.

Generate a password hash with:
    python3 -c "import sys; sys.path.insert(0,'app'); import auth; print(auth.make_hash('the password'))"

ADMIN_EMAIL, ADMIN_PASSWORD_HASH and ADMIN_SECRET must all be set or sign-in
refuses outright. ADMIN_SECRET also signs the session cookie - rotating it
signs everyone out.
"""
import os, hmac, hashlib, base64, json, time, threading

ITERATIONS = 200_000
SESSION_MAX_AGE = 30 * 24 * 3600
COOKIE = "crm_admin"

MAX_TRIES = 8
LOCKOUT = 15 * 60

_tries = {}
_lock = threading.Lock()


def make_hash(password, salt=None):
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return "pbkdf2:%d:%s:%s" % (ITERATIONS, b64(salt), b64(dk))


def _verify(password, stored):
    try:
        scheme, iters, salt_b64, dk_b64 = stored.split(":")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


def _secret():
    return (os.environ.get("ADMIN_SECRET") or "").encode("utf-8")


def configured():
    return bool(os.environ.get("ADMIN_EMAIL") and os.environ.get("ADMIN_PASSWORD_HASH")
                and _secret())


def locked_for(ip):
    with _lock:
        rec = _tries.get(ip)
        if not rec:
            return 0
        count, first = rec
        if time.time() - first > LOCKOUT:
            _tries.pop(ip, None)
            return 0
        return int(LOCKOUT - (time.time() - first)) if count >= MAX_TRIES else 0


def _record_failure(ip):
    with _lock:
        count, first = _tries.get(ip, (0, time.time()))
        if time.time() - first > LOCKOUT:
            count, first = 0, time.time()
        _tries[ip] = (count + 1, first)


def _clear_failures(ip):
    with _lock:
        _tries.pop(ip, None)


def check(email, password, ip):
    if not configured():
        return False, ("Sign-in is not configured on this server. Set ADMIN_EMAIL, "
                       "ADMIN_PASSWORD_HASH and ADMIN_SECRET.")
    wait = locked_for(ip)
    if wait:
        return False, ("Too many attempts. Try again in %d minute%s."
                       % (max(1, wait // 60), "" if wait // 60 == 1 else "s"))
    want_email = os.environ["ADMIN_EMAIL"].strip().lower()
    ok_email = hmac.compare_digest((email or "").strip().lower(), want_email)
    ok_pass = _verify(password or "", os.environ["ADMIN_PASSWORD_HASH"])
    if ok_email and ok_pass:
        _clear_failures(ip)
        return True, ""
    _record_failure(ip)
    return False, "That email and password do not match."


def issue(email):
    body = base64.urlsafe_b64encode(
        json.dumps({"e": email.strip().lower(), "t": int(time.time())}).encode()).decode()
    sig = base64.urlsafe_b64encode(
        hmac.new(_secret(), body.encode(), hashlib.sha256).digest()).decode()
    return body + "." + sig


def valid(cookie):
    if not cookie or not _secret():
        return None
    try:
        body, sig = cookie.split(".", 1)
        want = base64.urlsafe_b64encode(
            hmac.new(_secret(), body.encode(), hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(sig, want):
            return None
        data = json.loads(base64.urlsafe_b64decode(body))
        if time.time() - data["t"] > SESSION_MAX_AGE:
            return None
        return data["e"]
    except Exception:
        return None
