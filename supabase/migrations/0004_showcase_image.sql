-- Run once in the Supabase SQL editor, same as 0001/0002/0003.
--
-- Column behind admin-uploaded showcase screenshots (replacing the
-- auto-generated mshots preview, which some sites never rendered for).
-- The actual image bytes go to Supabase Storage, not this column - this
-- just holds the public URL app/store.py's save_showcase_image() returns.
-- The storage bucket itself ("showcase", public) is created automatically
-- on first upload if it doesn't exist yet - nothing to set up here for it.

alter table crm_showcase add column if not exists image_url text;
