-- Pacific Yew BDR · `leads` table
-- Run this in your Supabase project: SQL Editor -> New query -> paste -> Run
-- (Supabase auto-adds `id` and `created_at`; do NOT create them manually.)

create table if not exists public.leads (
  business_name   text,
  website         text,
  phone           text,
  agent_analysis  text,
  status          text default 'DRAFT_READY'
);

-- Speed up dedup lookups (the code checks website for repeats)
create index if not exists leads_website_idx on public.leads (website);

-- Allow the anon/service key used by the app to read/write leads.
-- Adjust the role to match the key you put in SUPABASE_KEY.
alter table public.leads enable row level security;

create policy "allow all for service role"
  on public.leads
  for all
  to service_role
  using (true)
  with check (true);
