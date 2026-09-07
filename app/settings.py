# -*- coding: utf-8 -*-
"""
The credentials vault. Every third-party key this app can use lives here,
typed in once from /admin/settings, never in a .env a developer has to touch
again. Same design as sadhna-astro's web/settings.py: secrets are Fernet-
encrypted at rest with a key derived from SETTINGS_KEY (or ADMIN_SECRET), the
database wins over the environment, and a stored secret is never rendered
back to the browser - only its last four characters.

SUPABASE_URL / SUPABASE_SERVICE_KEY are the one exception: they choose which
database backend this vault itself is stored in, so they cannot also live
inside that database (chicken-and-egg) and stay environment-only, same as
sadhna-astro.
"""
import os, base64, hashlib, threading, time

import store

# key -> (env var fallback, is_secret, label, hint)
FIELDS = {
    # --- Razorpay: subscriptions, the recurring monthly charge -------------
    "razorpay_key_id": ("RAZORPAY_KEY_ID", False,
        "Razorpay Key ID", "Starts rzp_test_ or rzp_live_. Dashboard → Account & Settings → API Keys."),
    "razorpay_key_secret": ("RAZORPAY_KEY_SECRET", True,
        "Razorpay Key Secret", "Shown once, when the key pair is generated."),
    "razorpay_webhook_secret": ("RAZORPAY_WEBHOOK_SECRET", True,
        "Razorpay Webhook Secret", "You choose this yourself when adding the webhook in Razorpay. "
                                    "Must match exactly what you type there."),
    # --- WhatsApp: client onboarding links + payment receipts --------------
    "whatsapp_provider": ("WHATSAPP_PROVIDER", False,
        "WhatsApp provider", "'meta' (WhatsApp Cloud API) or 'twilio'. Leave blank to disable WhatsApp sends."),
    "whatsapp_token": ("WHATSAPP_TOKEN", True,
        "Meta Cloud API access token", "From developers.facebook.com → your WhatsApp app → API setup."),
    "whatsapp_phone_id": ("WHATSAPP_PHONE_ID", False,
        "Meta Cloud API phone number ID", "The numeric ID of the sending WhatsApp number."),
    "twilio_account_sid": ("TWILIO_ACCOUNT_SID", False,
        "Twilio Account SID", "Starts AC..., from the Twilio console."),
    "twilio_auth_token": ("TWILIO_AUTH_TOKEN", True,
        "Twilio Auth Token", "From the Twilio console."),
    "twilio_whatsapp_from": ("TWILIO_WHATSAPP_FROM", False,
        "Twilio WhatsApp sender", "e.g. whatsapp:+14155238886"),
    # --- Email: receipts and reminders --------------------------------------
    "resend_api_key": ("RESEND_API_KEY", True,
        "Resend API key", "From resend.com → API Keys. Tried before Gmail if both are set."),
    "resend_from_email": ("RESEND_FROM_EMAIL", False,
        "Resend \"from\" address", "Must be on a domain verified in Resend, e.g. billing@yourdomain.com"),
    "gmail_user": ("GMAIL_USER", False,
        "Gmail address", "The Gmail account to send from."),
    "gmail_app_password": ("GMAIL_APP_PASSWORD", True,
        "Gmail app password", "16-character app password from your Google Account → Security "
                               "(requires 2-Step Verification). Not your normal password."),
    # --- AI providers: keys stored for the automated-messaging work ahead --
    "anthropic_api_key": ("ANTHROPIC_API_KEY", True,
        "Anthropic (Claude) API key", "Starts sk-ant-. Stored for future AI-drafted reminders; not called yet."),
    "gemini_api_key": ("GEMINI_API_KEY", True,
        "Gemini API key", "From aistudio.google.com. Stored for future use; not called yet."),
    "openai_api_key": ("OPENAI_API_KEY", True,
        "OpenAI API key", "Starts sk-. Stored for future use; not called yet."),
    # --- your business, shown on the onboarding page and in receipts -------
    "business_name": ("BUSINESS_NAME", False,
        "Your business / brand name", "Shown on the client onboarding page and in receipts."),
    "support_email": ("SUPPORT_EMAIL", False,
        "Support email", "Shown to clients if a payment fails."),
    "support_phone": ("SUPPORT_PHONE", False,
        "Support phone / WhatsApp", "Shown to clients if a payment fails."),
    # --- tax / invoicing ------------------------------------------------
    "business_address": ("BUSINESS_ADDRESS", False,
        "Registered business address", "Printed on the invoice letterhead."),
    "gstin": ("GSTIN", False,
        "GSTIN", "Leave blank until you have it - invoices show no GST line without it, "
                  "since you can't charge a tax you aren't registered for. Once set, every "
                  "invoice from then on shows the GST breakup automatically."),
    "gst_rate_percent": ("GST_RATE_PERCENT", False,
        "GST rate (%)", "Applied only if GSTIN above is set. Default 18 if left blank. "
                         "Plan prices are treated as GST-inclusive - this backs the tax out "
                         "of the amount actually charged, it does not add anything on top."),
}

GROUPS = [
    ("Razorpay (subscriptions)", ["razorpay_key_id", "razorpay_key_secret", "razorpay_webhook_secret"]),
    ("WhatsApp", ["whatsapp_provider", "whatsapp_token", "whatsapp_phone_id",
                  "twilio_account_sid", "twilio_auth_token", "twilio_whatsapp_from"]),
    ("Email", ["resend_api_key", "resend_from_email", "gmail_user", "gmail_app_password"]),
    ("AI providers", ["anthropic_api_key", "gemini_api_key", "openai_api_key"]),
    ("Business details", ["business_name", "support_email", "support_phone", "business_address"]),
    ("Tax & invoicing", ["gstin", "gst_rate_percent"]),
]

ENC_PREFIX = "enc:v1:"
_SALT = b"personal-crm/app_settings/v1"
_ITERATIONS = 200_000
TTL = 20

_cache = {"at": 0.0, "raw": {}, "error": None}
_lock = threading.Lock()
_fernet_cache = {"secret": None, "f": None}

try:
    from cryptography.fernet import Fernet
except Exception:
    Fernet = None


def _key_material():
    return (os.environ.get("SETTINGS_KEY") or os.environ.get("ADMIN_SECRET") or "").strip()


def _fernet():
    secret = _key_material()
    if not secret or Fernet is None:
        return None
    with _lock:
        if _fernet_cache["secret"] == secret and _fernet_cache["f"] is not None:
            return _fernet_cache["f"]
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), _SALT, _ITERATIONS)
    f = Fernet(base64.urlsafe_b64encode(dk))
    with _lock:
        _fernet_cache["secret"], _fernet_cache["f"] = secret, f
    return f


def can_encrypt():
    return _fernet() is not None


def encryption_problem():
    if Fernet is None:
        return "The 'cryptography' package is not installed, so secrets cannot be stored safely."
    if not _key_material():
        return "SETTINGS_KEY (or ADMIN_SECRET) is not set, so secrets cannot be stored safely."
    return ""


def _encrypt(value):
    f = _fernet()
    if f is None:
        raise ValueError(encryption_problem())
    return ENC_PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(stored):
    if not stored.startswith(ENC_PREFIX):
        return stored
    f = _fernet()
    if f is None:
        return None
    try:
        return f.decrypt(stored[len(ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return None


def _raw(force=False):
    with _lock:
        if not force and time.time() - _cache["at"] <= TTL:
            return dict(_cache["raw"]), _cache["error"]
    try:
        raw, err = store.settings_all(), None
    except Exception as exc:
        raw, err = None, str(exc)[:300]
    with _lock:
        if raw is not None:
            _cache["raw"] = raw
        _cache["error"] = err
        _cache["at"] = time.time()
        return dict(_cache["raw"]), _cache["error"]


def resolve(key):
    """(value, source). source is 'database', 'environment', 'unreadable' or ''."""
    env_name, is_secret = FIELDS[key][0], FIELDS[key][1]
    raw, _ = _raw()
    stored = (raw.get(key) or "").strip()
    if stored:
        plain = _decrypt(stored)
        if plain:
            return plain, "database"
        if is_secret:
            return "", "unreadable"
    env = (os.environ.get(env_name) or "").strip()
    if env:
        return env, "environment"
    return "", ""


def get(key):
    return resolve(key)[0]


def put(key, value):
    if key not in FIELDS:
        raise ValueError("Unknown setting.")
    value = (value or "").strip()
    if not value:
        raise ValueError("That field was empty.")
    is_secret = FIELDS[key][1]
    store.settings_put(key, _encrypt(value) if is_secret else value)
    _raw(force=True)


def clear(key):
    if key not in FIELDS:
        raise ValueError("Unknown setting.")
    store.settings_delete(key)
    _raw(force=True)


def mask(key):
    value, source = resolve(key)
    is_secret = FIELDS[key][1]
    if source == "unreadable":
        return "stored, but this server cannot decrypt it"
    if not value:
        return ""
    if not is_secret:
        return value
    return ("•" * 8 + value[-4:]) if len(value) >= 12 else "•" * 12


def rows():
    out = []
    for key, (env_name, is_secret, label, hint) in FIELDS.items():
        value, source = resolve(key)
        out.append(dict(key=key, label=label, hint=hint, secret=is_secret,
                        env_name=env_name, source=source, shown=mask(key), set=bool(value)))
    return out


def store_problem():
    _, err = _raw()
    if not err:
        return ""
    return "The settings table could not be read: %s" % err


# ------------------------------------------------------------ derived facts
def razorpay_key_id():
    return get("razorpay_key_id")


def razorpay_key_secret():
    return get("razorpay_key_secret")


def razorpay_webhook_secret():
    return get("razorpay_webhook_secret")


def razorpay_missing():
    return [k for k in ("razorpay_key_id", "razorpay_key_secret", "razorpay_webhook_secret") if not get(k)]


def razorpay_ready():
    """True once all three Razorpay values resolve. Subscriptions can then be
    created and webhook signatures verified. There is no separate on/off
    switch here (unlike sadhna-astro's payments_enabled) - a personal ops
    tool has one operator, and if the keys are in, they mean it."""
    return not razorpay_missing()


def razorpay_mode():
    kid = razorpay_key_id()
    if kid.startswith("rzp_test_"):
        return "test"
    return "live" if kid else ""


def business_name():
    return get("business_name") or "Your Studio"


def support_email():
    return get("support_email")


def support_phone():
    return get("support_phone")


def business_address():
    return get("business_address")


def gstin():
    return get("gstin")


def gst_rate_percent():
    """A float, never raises. Only meaningful when gstin() is set - callers
    must check that themselves, this just answers "what rate" if they do."""
    raw = get("gst_rate_percent")
    if not raw:
        return 18.0
    try:
        return float(raw)
    except ValueError:
        return 18.0


def whatsapp_ready():
    provider = get("whatsapp_provider")
    if provider == "meta":
        return bool(get("whatsapp_token") and get("whatsapp_phone_id"))
    if provider == "twilio":
        return bool(get("twilio_account_sid") and get("twilio_auth_token") and get("twilio_whatsapp_from"))
    return False


def email_ready():
    return bool(get("resend_api_key") or (get("gmail_user") and get("gmail_app_password")))
