# Vajra Labs — Product Pivot Plan
**From: outbound cold lead-gen tool**
**To: AI-powered instant lead response & booking assistant for clinics**

Based on 20+ real sales calls with zero conversions on the outbound model, and consistent feedback that "finding leads + generic outreach" is already commoditized (free Excel/placeholder tools, existing Maps scrapers).

---

## Build status (updated 30 Jul 2026)
All ten v1 core-loop features below are now built:

1. Instant lead response — ✅ web chat widget live
2. Lead qualification — ✅ `conversation_agent.py`
3. Clinic knowledge base / FAQ — ✅
4. Appointment booking — ✅ real Google Calendar integration
5. Reminders and rescheduling — ✅ `reminder_agent.py` (24h-before email reminder) + self-serve reschedule/cancel via `/appointment/<id>/manage`
6. Auto follow-up sequences — ✅ `followup_agent.py`
7. Human takeover — ✅ dashboard live-conversation view
8. Consent and opt-in capture — ✅
9. Activity log / audit trail — ✅
10. Simple CRM sync (Sheets) — **deliberately skipped.** The dashboard reads live from Supabase and already does everything the Sheets sync was a stand-in for (and does it better: real-time, takeover buttons, appointment status), so a Sheets copy would just be redundant. Revisit only if a specific need comes up (e.g. handing raw data to someone who won't use the dashboard) — that'd be a small one-off CSV export, not a live sync.

**What's left is not engineering:**
- WhatsApp channel — blocked on Meta Business Verification/App Review, capped at 5 test recipients in the meantime.
- First paying customer — the actual goal every feature above serves.
- Infra migration off Render to Google Cloud Run — in progress, blocked on GCP billing identity verification (submitted, pending review as of 30 Jul 2026). See `CLOUD_RUN_MIGRATION.md`.

---

## Why this pivot makes sense
The rejected product asked businesses to trust a stranger's cold outreach. The new product solves a problem business owners already feel acutely: **leads going cold because nobody replies fast enough.** This is a felt, urgent pain, not a "nice to have," and it's a well-proven category (Meta itself launched "Business AI on WhatsApp" for Indian SMBs in May 2026, confirming real market demand). The differentiator isn't novelty, it's execution and niche focus.

---

## New main features (v1 core loop)
These become the actual product. In priority build order:

1. **Instant lead response** — reply within 1 minute when the channel is available (not overpromising uptime across channels still pending approval, like WhatsApp)
2. **Lead qualification** — ask 3-5 smart questions specific to the clinic type (dental, dermatology, physio, and general practice each need different intake questions, this isn't one generic set)
3. **Clinic knowledge base / FAQ replies** — the agent knows the clinic's hours, services, pricing, and location, so it can answer routine questions itself instead of escalating everything
4. **Appointment booking** — book a consultation/visit directly into the clinic's calendar
5. **Reminders and rescheduling** — a reminder before the visit, and an easy way for the lead to reschedule or cancel without calling in. This is a big driver of no-show reduction, not optional
6. **Auto follow-up sequences** — if a lead goes quiet, follow up at 24h, 2 days, 4 days, 1 week
7. **Human takeover** — clinic staff can jump into any conversation, especially on real buying intent or a question the AI can't handle
8. **Consent and opt-in capture** — clear opt-in before follow-up messaging, especially required for WhatsApp business-initiated messages, and just good practice under India's data protection rules
9. **Activity log / audit trail** — who replied, when the bot handed over, what got booked, clinics care about this for accountability
10. **Simple CRM sync** — every lead, score, and conversation logged to Google Sheets, explicitly a temporary MVP sync layer, not the long-term CRM


## What to add (net-new engineering)
- **Inbound message webhook(s)** — currently the product only sends, it never receives. This is the single biggest architecture change.
- **Conversation state machine** — track where each lead is in the qualification flow, not a one-shot message like today's Email/WhatsApp Agent
- **Clinic knowledge base storage** — a simple structured profile per clinic (hours, services, pricing, FAQs) the agent references when answering
- **Calendar integration** — Google Calendar OAuth per clinic, for real appointment booking, reminders, and rescheduling
- **Scheduler** — a background job (cron/APScheduler) for follow-up sequences and pre-visit reminders
- **CRM sync (Sheets)** — push each lead/conversation/score to a Google Sheet via the Sheets API
- **Human takeover UI** — a live conversation view in the dashboard where staff can see and reply directly, pausing the bot for that thread
- **Lead scoring logic** — a simple hot/warm/cold rubric, deliberately not sophisticated at first
- **Consent capture flow** — opt-in language and a stored consent record per lead
- **Activity log** — a simple timestamped record per conversation (bot replies, handoffs, bookings, cancellations)

## What to keep, mostly unchanged
- **Research Agent** (Claude-powered) — repurposed: instead of researching a *prospect's* business before cold outreach, it now helps the qualification agent understand *the clinic's own services* to ask better questions and respond more naturally
- **Billing** (Razorpay, ₹999/mo, live and tested)
- **Branding, domain, legal pages** (all already done)
- **Auth/account system** (Supabase-based login, unchanged)
- **Agent-naming pattern in the UI** ("X Agent is working...") — carries over to the new agents

## What to remove entirely
- **Discovery Agent** (Google Maps lead scraping) — fully removed, not the product anymore
- **Old outbound Email Agent** (cold email sending) — removed
- **Old outbound WhatsApp Agent** (cold WhatsApp sending) — removed
- **The current Leads tab / CRM** as built — replaced by an **inbound conversations and bookings** view, not just a re-labeled leads list

---

## On multi-doctor/service availability rules
Real feedback raised this as a gap. I'm deliberately keeping it **out of true v1**: if v1 targets one clinic to prove the loop, a single calendar with the clinic's operating hours is enough, building a full per-doctor/per-service availability engine before one clinic is even using the core loop would be premature. The fix is to **not paint ourselves into a corner**: the data model (appointments tied to a `provider_id` and `service_type` from day one, even if there's only ever one provider at first) should make adding real multi-doctor rules later a data change, not a rewrite.

---

## Clinic-specific qualification questions (starting point, configured per customer)
These live in each clinic's own profile/knowledge base, not hardcoded in the product. Starting point, using dental as the first real example since that's who you're pitching first:
1. What are you looking to get help with? (open-ended, captures intent)
2. Is this your first visit, or have you visited before?
3. Do you have a preferred date/time, or is it urgent?
4. Any specific doctor/specialist you'd like to see? (if the clinic has multiple)
5. Do you have insurance, or is this self-pay? (if relevant to the clinic's billing)

When the next clinic signs up (dermatology, physio, whatever comes next), they configure their own version of these, same product, no code change needed.

Score: urgency + clear intent + willingness to book = hot. Vague/browsing = warm. Price-shopping only, no real interest = cold.

---

## Channel strategy — the one open risk
Your WhatsApp Business API access is still **pending Meta's Business Verification + App Review**, capped at 5 test recipients until that clears. Since the whole v1 is built around instant response and qualification, **the underlying conversation engine should be channel-agnostic from day one**, so it can run on:
- **Web chat widget** (embed on the clinic's own website) — zero external gating, works immediately
- **Email** — already fully working today
- **WhatsApp** — plugs in the moment Meta approval clears, same conversation logic underneath

This avoids rebuilding the core logic twice, and means you're not blocked waiting on Meta to start onboarding real clinics.

---

## What to explicitly avoid in v1 (scope discipline)
- **No WhatsApp-specific logic** until the channel is actually live, don't build around a channel you can't fully use yet
- **No HubSpot/Zoho integration** before one real clinic is using the core loop, Sheets is enough to prove value
- **No sophisticated scoring model**, a simple hot/warm/cold rubric is enough at first
- **No multi-doctor availability engine** yet (see note above), single calendar is enough for the first clinic

## Better v1 shape — narrow on purpose, but only in go-to-market
Two different things were bundled in the earlier advice, worth separating clearly:

- **The engine stays generic, product-wide**: one codebase serving "Clinic" as a category, not hardcoded per specialty. Each clinic's specifics, services, hours, pricing, FAQs, qualification questions, live in a **per-customer knowledge base/profile**, the same pattern the original Vajra Labs already used (`your_service`/`your_about` fields). A dermatology clinic and a dental clinic both just fill in their own details into the same underlying system.
- **Go-to-market stays narrow, for now**: your first sales conversations and demo should target **dental specifically**, not because the product needs it, but because "built for dentists" is a sharper pitch than "built for clinics" when you're trying to close your very first real customer. This is a messaging choice, not a technical constraint, once dental is proven, the exact same product already works for the next dermatologist or physio who signs up, no rebuild required.

Other scope discipline still holds for true v1:
- **One channel first**: web chat widget
- **One goal first**: book appointments, not "do everything"
- **One data sink first**: Google Sheets + the dashboard
- **One human handoff path first**: owner/staff takeover in the dashboard

---

## Suggested build phases
Given the scope, building this in one shot isn't realistic. Suggested sequence:

**Phase 1 — Core loop, one channel, configured for the first real customer (dental)**
Web chat widget + generic qualification-conversation engine (configured with dental questions/knowledge for the first customer) + clinic knowledge base/FAQ + lead scoring + consent capture + basic Sheets logging + activity log. Provable end-to-end with a single dental clinic, generalizes to any clinic type by just changing that customer's config, not the code.

**Phase 2 — Booking, reminders, rescheduling**
Google Calendar integration, AI-driven appointment booking inside the conversation, pre-visit reminders, self-serve reschedule/cancel.

**Phase 3 — Follow-ups + human takeover**
Scheduler for automated follow-ups on gone-quiet leads, live dashboard for staff to take over conversations.

**Phase 4 — Multi-channel, then scale out**
Add Email as a second channel, then WhatsApp once Meta approval clears. Onboarding a second/third clinic (different specialty) at this point is just configuration, not new engineering, only multi-doctor availability rules and any CRM integrations beyond Sheets would be genuinely new build work.

---

## New agent names (draft, open to your naming preferences)
- **Reply Agent** *(name reused, new job)* — handles instant first response
- **Qualify Agent** — runs the question flow, scores the lead
- **Booking Agent** — handles calendar scheduling
- **Follow-up Agent** — manages the drip sequence for gone-quiet leads
- **Research Agent** *(kept)* — now helps agents understand the clinic's own services/context
