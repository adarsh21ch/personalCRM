-- Run once in the Supabase SQL editor, same as 0001/0002.
--
-- The public "our work" catalog on the homepage - real projects with a
-- name, a live URL, and a short line about them. Admin-curated at
-- /admin/showcase, deliberately separate from the CRM's clients/products
-- tables: a showcase entry is public marketing copy (and can include your
-- own tools, not just client work), while products/notes are private CRM
-- data that was never meant to be shown to a stranger on the homepage.

create table if not exists crm_showcase (
  id text primary key, created_at timestamptz not null default now(),
  name text not null, url text not null, description text,
  active boolean not null default true
);
