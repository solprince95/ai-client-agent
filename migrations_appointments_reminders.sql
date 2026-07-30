-- Run this in the Supabase SQL editor (Project → SQL Editor → New query)
-- Adds the columns needed for pre-visit reminders and self-serve
-- reschedule/cancel. Safe to run even if some columns already exist.

alter table appointments
  add column if not exists google_event_id text default '',
  add column if not exists reminder_sent boolean default false,
  add column if not exists cancelled_at timestamptz,
  add column if not exists reschedule_requested_text text default '';

-- Backfill: existing confirmed appointments shouldn't get an immediate
-- reminder blast on first deploy if their time has already passed or
-- is very close. This marks anything already confirmed as "already
-- reminded" so only appointments confirmed after this migration are
-- eligible. Remove this line if you'd rather they get caught by the
-- next hourly check instead.
update appointments set reminder_sent = true where status = 'confirmed';
