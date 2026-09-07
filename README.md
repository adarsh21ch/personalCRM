# Personal CRM + Subscriptions

Your own ops dashboard for every website/app you've built for a client: who
they are, which product they're on, whether this month's Razorpay auto-debit
went through, and a link you send them to put themselves on a plan.

Not a multi-tenant SaaS — one operator (you), one Razorpay account, used
across every client relationship you manage.

## What it does

- **Clients & products** — one record per client, with the product(s)/site(s)
  you built for them attached.
- **Three default plans** — ₹999 / ₹1999 / ₹2999 per month, editable, and you
  can add more.
- **Razorpay Subscriptions** — a client opens a link, authorizes a recurring
  mandate once, and Razorpay auto-charges it every month from then on. This
  is Razorpay's *Subscriptions* product (a mandate + auto-debit), not the
  one-time *Orders* flow — different API, different webhook events.
- **Dashboard** — client count, product count, active subscriptions, MRR, and
  a live list of every client with their plan and status (Active / Pending /
  Declined / Cancelled).
- **Webhook-driven status** — Razorpay tells this app when a mandate is
  confirmed, a charge succeeds, a charge fails repeatedly (halted), or a
  client cancels. Nothing here has to be checked by hand in the Razorpay
  dashboard.
- **Credentials vault** — every third-party key (Razorpay, WhatsApp, email,
  AI providers) is entered once at `/admin/settings`, encrypted at rest, and
  read from there instead of environment variables you'd otherwise have to
  edit on every rotation.

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python3 -c "import sys; sys.path.insert(0,'app'); import auth; print(auth.make_hash('choose-a-password'))"
# paste that hash into .env as ADMIN_PASSWORD_HASH, set ADMIN_EMAIL and ADMIN_SECRET too

uvicorn app.main:app --reload --port 8090
```

Open `http://localhost:8090/admin`, sign in, then go to **Settings** and add
your Razorpay test keys (`rzp_test_...`) to try the full subscription flow
with Razorpay's test card `4111 1111 1111 1111`.

## First-time setup, in order

1. **Sign in** with the `ADMIN_EMAIL` / password you generated the hash for.
2. **Settings → Razorpay**: paste your Key ID and Key Secret, hit *Test
   connection*. Then in the Razorpay Dashboard → Settings → Webhooks, add the
   URL shown on the settings page, subscribe it to the `subscription.*` and
   `payment.failed` events, and paste the webhook secret you chose back into
   Settings.
3. **Settings → WhatsApp / Email**: add whichever you use, so onboarding
   links and payment receipts can actually be sent. Both are optional — the
   app works without them, you just share links manually.
4. **Add a client**, generate an onboarding link for a plan, send it.
5. Watch the client's status move from *Awaiting checkout* → *Mandate
   confirmed* → *Active* as Razorpay's webhooks arrive.

## Deploy — Vercel (free)

`api/index.py` re-exports the FastAPI app for Vercel's Python runtime, and
`vercel.json` rewrites every path to it. **Supabase is required on Vercel** —
serverless functions have no persistent disk, so `SUPABASE_URL` /
`SUPABASE_SERVICE_KEY` must both be set or the app has nowhere durable to
write.

1. Vercel dashboard → **Add New → Project** → import the GitHub repo.
   Framework preset: **Other** (it auto-detects Python from
   `requirements.txt` at the repo root).
2. Environment variables (Project → Settings → Environment Variables):
   `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ADMIN_EMAIL`,
   `ADMIN_PASSWORD_HASH`, `ADMIN_SECRET`, `SETTINGS_KEY` — same values as
   below. `PORT` / `CRM_DB` are not used on Vercel.
3. Deploy. Check `https://<your-project>.vercel.app/healthz` →
   `"ok": true, "backend": "supabase"`.
4. **Custom domain**: Project → Settings → Domains → add
   `personalcrm.nevorai.com`. Vercel shows a CNAME target
   (`cname.vercel-dns.com`) — add that as a CNAME record for `personalcrm`
   in your DNS provider's zone for `nevorai.com`. Vercel issues the TLS
   certificate automatically once the record resolves.

One thing worth knowing: the admin login's brute-force lockout
(`app/auth.py`) tracks failed attempts in an in-process dict. That's solid
on a single long-lived container; on Vercel, concurrent cold starts are
isolated instances, so the lockout doesn't reliably span all of them. Not a
real exposure for a private, single-operator tool behind a real password —
just not the same guarantee a persistent server gives you.

## Deploy — Render / any Docker host (alternative)

`Dockerfile` + `render.yaml` are also included, if you'd rather run this as
a normal long-lived container instead of serverless — a persistent process
is what gives the login lockout above its full guarantee, and it's the
better fit for the SQLite fallback if you ever want to run this away from
Supabase. Point any Docker host at `Dockerfile` / `Procfile`; required env
vars are the same list, in `render.yaml` and `.env.example`.

## Architecture notes

- `app/store.py` — SQLite by default; switches to Supabase Postgres the
  moment `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are both set. Same schema
  either way (see `supabase/migrations/0001_init.sql`).
- `app/settings.py` — the credentials vault. Secrets are Fernet-encrypted
  with a key derived from `SETTINGS_KEY` (or `ADMIN_SECRET`); a stored value
  is never rendered back to the browser, only its last four characters.
- `app/rzp.py` — the four Razorpay REST calls this app makes (plans,
  subscriptions, cancel, test connection) over `httpx`, no SDK dependency.
- `app/notify.py` — WhatsApp (Meta Cloud API or Twilio) and email (Resend or
  Gmail SMTP) senders, chosen by whichever is configured. Every call returns
  `(ok, info)` and never raises — a failed notification must not break a
  webhook or an admin action.
- Subscription statuses are Razorpay's own (`created`, `authenticated`,
  `active`, `pending`, `halted`, `cancelled`, `completed`, `expired`),
  grouped on the dashboard into Active / Pending / Declined / Cancelled.
