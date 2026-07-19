-- ======================================================
-- WhatsApp support migration
-- Run this once in the Supabase SQL editor.
-- ======================================================

-- 1) profiles : where each user's WhatsApp Business connection lives.
--    Values are blank until the "Connect WhatsApp" flow is built
--    (Meta Embedded Signup). For now they can be filled in manually
--    for testing.
alter table public.profiles
  add column if not exists whatsapp_access_token text default '',
  add column if not exists whatsapp_phone_number_id text default '',
  add column if not exists whatsapp_business_account_id text default '',
  add column if not exists whatsapp_template_name text default 'business_outreach_intro',
  add column if not exists whatsapp_template_lang text default 'en_US';

-- 2) leads : track WhatsApp outreach status independently from email
--    status, on the same lead row.
alter table public.leads
  add column if not exists whatsapp_status text default 'discovered',
  add column if not exists whatsapp_sent_date date,
  add column if not exists whatsapp_message_sent text default '',
  add column if not exists whatsapp_replied_at timestamptz;

-- 3) whatsapp_log : mirrors sent_log, one row per WhatsApp message sent.
create table if not exists public.whatsapp_log (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  phone text not null,
  business_name text default '',
  sent_date date default current_date,
  message text default '',
  wa_message_id text default '',
  created_at timestamptz default now()
);

alter table public.whatsapp_log enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = 'whatsapp_log'
      and policyname = 'Users can read their own whatsapp_log'
  ) then
    create policy "Users can read their own whatsapp_log"
      on public.whatsapp_log
      for select
      using (auth.uid() = user_id);
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = 'whatsapp_log'
      and policyname = 'Users can insert their own whatsapp_log'
  ) then
    create policy "Users can insert their own whatsapp_log"
      on public.whatsapp_log
      for insert
      with check (auth.uid() = user_id);
  end if;
end $$;

notify pgrst, 'reload schema';
