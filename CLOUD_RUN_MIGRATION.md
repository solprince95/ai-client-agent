# Migrating Vajra Labs from Render to Google Cloud Run

## What's already done
- `Dockerfile` — containerizes the app, same gunicorn worker layout as the Render `Procfile`.
- `.dockerignore` — keeps the image lean, excludes `.git`, `Media/`, `PAYMENT.txt`, `.bak` files.
- `cloudbuild.yaml` — builds the image, pushes it to Artifact Registry, deploys to Cloud Run. This is the Cloud Run equivalent of Render's "push to deploy."
- `app.py` — the Follow-up Agent's hourly background job is now gated behind `ENABLE_INPROCESS_SCHEDULER`, and there's a new `/internal/followup-check` endpoint for Cloud Scheduler to call instead (see "The scheduler problem" below).

## One-time setup

### 1. Install and authenticate the gcloud CLI
```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

### 2. Enable the APIs you'll need
```bash
gcloud services enable run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com
```

### 3. Create an Artifact Registry repo for the image
```bash
gcloud artifacts repositories create vajra-labs \
  --repository-format=docker \
  --location=asia-south1
```

### 4. First deploy (manual, to confirm it works)
From inside the project folder:
```bash
gcloud run deploy vajra-labs \
  --source . \
  --region=asia-south1 \
  --allow-unauthenticated
```
`--source .` tells Cloud Run to build the Dockerfile itself for this first deploy, no need to push an image by hand.

### 5. Set your environment variables
Everything currently in Render's Environment tab needs to move here. Full list found in the code:
```
FLASK_SECRET, SUPABASE_URL, SUPABASE_KEY, ANTHROPIC_API_KEY,
GOOGLE_MAPS_API_KEY, GOOGLE_CALENDAR_CLIENT_ID, GOOGLE_CALENDAR_CLIENT_SECRET,
RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_PLAN_ID, RAZORPAY_WEBHOOK_SECRET,
BREVO_API_KEY, META_APP_ID, META_CONFIG_ID,
MAINTENANCE_MODE, MAINTENANCE_BYPASS_KEY
```
Set them (better: use `--set-secrets` with Secret Manager for anything sensitive, but `--set-env-vars` works to get started):
```bash
gcloud run services update vajra-labs \
  --region=asia-south1 \
  --set-env-vars="SUPABASE_URL=...,SUPABASE_KEY=...,ANTHROPIC_API_KEY=...,RAZORPAY_KEY_ID=...,RAZORPAY_KEY_SECRET=..." \
  --set-env-vars="ENABLE_INPROCESS_SCHEDULER=0,CRON_SECRET=some-long-random-string"
```
(Split across multiple `--set-env-vars` calls if the list gets unwieldy in one line, or use a `.env.yaml` file with `--env-vars-file`.)

## The scheduler problem, and how it's handled
Cloud Run can scale an idle service down to zero, which would silently kill the old in-process `BackgroundScheduler` thread that runs the Follow-up Agent hourly. Two ways to handle it; pick one:

**Option A — Cloud Scheduler (recommended, cheaper)**
Set `ENABLE_INPROCESS_SCHEDULER=0` and `CRON_SECRET` on the service, then create a Cloud Scheduler job that hits the new endpoint hourly:
```bash
gcloud scheduler jobs create http followup-check \
  --schedule="0 * * * *" \
  --uri="https://YOUR-CLOUD-RUN-URL/internal/followup-check" \
  --http-method=POST \
  --headers="X-Cron-Secret=some-long-random-string" \
  --location=asia-south1
```
This only runs (and only costs you anything) once an hour, on-demand, instead of paying for an always-on instance.

**Option B — keep it in-process, force an always-on instance**
Leave `ENABLE_INPROCESS_SCHEDULER=1` (the default) and set a minimum instance count so the app never scales to zero:
```bash
gcloud run services update vajra-labs --region=asia-south1 --min-instances=1
```
Simpler, but you pay for one instance running 24/7 even with no traffic. Option A is the better fit here.

## Custom domain (vajralabs.co.in)
```bash
gcloud run domain-mappings create --service=vajra-labs --domain=vajralabs.co.in --region=asia-south1
```
This gives you DNS records to add at your domain registrar (same propagation wait as last time, typically 15 min–48 hrs).

## Billing
Cloud Run/GCP billing officially supports UPI in India, so this sidesteps the RuPay/card decline issue entirely once your GCP billing account is set up with UPI.

## Ongoing deploys
Once this is working, connect the Cloud Build trigger to your GitHub repo (Cloud Console → Cloud Build → Triggers → Connect Repository) pointed at `cloudbuild.yaml`, and every push to `main` will rebuild and redeploy automatically, matching the git-push-to-deploy flow you had on Render.

## Things worth double-checking before cutover
- Update `GOOGLE_CALENDAR_CLIENT_ID`'s OAuth redirect URI in Google Cloud Console to the new Cloud Run/domain URL.
- Update the Razorpay webhook URL (`/api/webhooks/razorpay`) in the Razorpay dashboard to point at the new domain.
- Test `/internal/followup-check` manually once (with the correct header) before relying on Cloud Scheduler.
