-- Run once in the Supabase SQL editor, same as 0001.
--
-- Adds the column behind per-client custom plans (a one-off amount agreed
-- with a single client, e.g. Rs 10/month, that must never appear in the
-- shared plan list or on the public homepage). A custom plan is stored with
-- active = false and client_id set to the client it belongs to.
--
-- Safe to run more than once, and safe to run before or after deploying the
-- code: app/store.py's supports() detects whether this has been applied and
-- degrades to "custom plans work, but aren't tagged to a client" until it is.

alter table crm_plans add column if not exists client_id text;

create index if not exists idx_crm_plans_client on crm_plans(client_id)
  where client_id is not null;
