-- Run once in the Supabase SQL editor. Mirrors app/store.py's SQLite schema
-- exactly - the app talks to both through the same functions.
--
-- Every table is prefixed crm_ on purpose: this app is meant to live inside
-- a Supabase project you already share with other small tools ("Nevorai
-- Tools" style - one Postgres per family of tools, not one per tool), and
-- "clients", "payments", "plans" are exactly the names another tool in that
-- same project might also want. If you ever rename the prefix, set
-- CRM_TABLE_PREFIX to match before the app starts - it must agree with
-- whatever these tables are actually called.

create table if not exists crm_clients (
  id text primary key, created_at timestamptz not null default now(),
  name text not null, email text, phone text, company text, notes text,
  status text not null default 'active'
);

create table if not exists crm_products (
  id text primary key, created_at timestamptz not null default now(),
  client_id text not null references crm_clients(id) on delete cascade,
  name text not null, url text, notes text
);

create table if not exists crm_plans (
  id text primary key, created_at timestamptz not null default now(),
  name text not null, amount_paise integer not null,
  interval text not null default 'monthly',
  rzp_plan_id text, active boolean not null default true
);

create table if not exists crm_subscriptions (
  id text primary key, created_at timestamptz not null default now(),
  client_id text not null references crm_clients(id) on delete cascade,
  product_id text references crm_products(id) on delete set null,
  plan_id text not null references crm_plans(id),
  rzp_subscription_id text, status text not null default 'created',
  amount_paise integer not null,
  next_billing_at timestamptz, started_at timestamptz, cancelled_at timestamptz,
  cancel_reason text, short_url text
);

create table if not exists crm_payments (
  id text primary key, created_at timestamptz not null default now(),
  subscription_id text not null references crm_subscriptions(id) on delete cascade,
  rzp_payment_id text, amount_paise integer not null, status text not null,
  method text, notes text
);

create table if not exists crm_onboard_tokens (
  id text primary key, created_at timestamptz not null default now(),
  client_id text not null references crm_clients(id) on delete cascade,
  plan_id text not null references crm_plans(id), product_id text,
  token text unique not null, used_at timestamptz, subscription_id text
);

create table if not exists crm_app_settings (
  key text primary key, value text not null, updated_at timestamptz not null default now()
);

create unique index if not exists idx_crm_subs_rzp on crm_subscriptions(rzp_subscription_id)
  where rzp_subscription_id is not null;
create unique index if not exists idx_crm_payments_rzp on crm_payments(rzp_payment_id)
  where rzp_payment_id is not null;
create index if not exists idx_crm_products_client on crm_products(client_id);
create index if not exists idx_crm_subs_client on crm_subscriptions(client_id);
create index if not exists idx_crm_payments_sub on crm_payments(subscription_id);

-- The app authenticates with the service_role key, which bypasses RLS, so
-- these tables need no policies for the app itself to work. Row Level
-- Security is left off by default (single-operator tool, one service key);
-- enable and add a policy only if you ever expose this to more than yourself.
