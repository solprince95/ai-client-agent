-- ======================================================
-- Razorpay billing migration
-- Run this once in the Supabase SQL editor.
-- ======================================================

-- 1) profiles: track subscription state
alter table public.profiles
  add column if not exists paid_until timestamptz,
  add column if not exists razorpay_subscription_id text default '';

-- 2) payments: one row per successful charge (used for receipt history)
create table if not exists public.payments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  razorpay_payment_id text default '',
  razorpay_subscription_id text default '',
  amount numeric default 0,
  currency text default 'INR',
  status text default '',
  receipt_number text default '',
  created_at timestamptz default now()
);

alter table public.payments enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = 'payments'
      and policyname = 'Users can read their own payments'
  ) then
    create policy "Users can read their own payments"
      on public.payments
      for select
      using (auth.uid() = user_id);
  end if;
end $$;

-- Note: inserts/updates to payments and profiles.is_paid happen only from
-- the backend (using the service role key), never directly from the
-- browser, so no insert/update policy is needed for regular users here.

notify pgrst, 'reload schema';
