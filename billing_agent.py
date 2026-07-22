"""
billing_agent.py: "Billing Agent"

Handles automated payments via Razorpay Subscriptions:
  1. Creates a subscription for a user when they click "Upgrade"
  2. Verifies and processes Razorpay webhook events
  3. Activates the user's plan automatically on successful payment
  4. Stores a payment/receipt record for their history

Requires these environment variables (set on Render):
  RAZORPAY_KEY_ID          e.g. rzp_test_xxxxxxxx
  RAZORPAY_KEY_SECRET
  RAZORPAY_PLAN_ID         created once in Razorpay Dashboard > Subscriptions > Plans
  RAZORPAY_WEBHOOK_SECRET  set when you add the webhook in Razorpay Dashboard
"""

import os
import hmac
import hashlib
from datetime import datetime, timedelta

import razorpay

RAZORPAY_KEY_ID        = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET    = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_PLAN_ID       = os.environ.get("RAZORPAY_PLAN_ID", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

PLAN_MONTHLY_CYCLES = 120  # 10 years worth of monthly cycles; renews automatically each
                           # month via Razorpay, cancel anytime, this is just an upper bound

_client = None


def billing_configured() -> bool:
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET and RAZORPAY_PLAN_ID)


def _get_client():
    global _client
    if _client is None and RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        _client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    return _client


# ======================================================
#  CREATE SUBSCRIPTION
# ======================================================
def create_subscription(user_id: str, email: str, full_name: str = "") -> dict:
    """
    Creates a Razorpay subscription for this user against the monthly plan.
    Returns {"ok": True, "subscription_id": ..., "key_id": ...} for the
    frontend to open Razorpay Checkout, or {"ok": False, "message": ...}.
    """
    if not billing_configured():
        return {"ok": False, "message": "Billing isn't fully configured yet. Please try again later."}

    client = _get_client()
    try:
        subscription = client.subscription.create({
            "plan_id": RAZORPAY_PLAN_ID,
            "total_count": PLAN_MONTHLY_CYCLES,
            "quantity": 1,
            "customer_notify": 1,
            "notes": {
                "user_id": user_id,
                "email": email,
                "full_name": full_name,
            },
        })
        return {
            "ok": True,
            "subscription_id": subscription["id"],
            "key_id": RAZORPAY_KEY_ID,
        }
    except Exception as e:
        return {"ok": False, "message": f"Could not start checkout: {e}"}


# ======================================================
#  WEBHOOK VERIFICATION
# ======================================================
def verify_webhook_signature(request_body: bytes, signature: str) -> bool:
    """
    Confirms a webhook actually came from Razorpay, not an impersonator.
    Razorpay signs the raw request body with your Webhook Secret (HMAC-SHA256);
    we recompute it here and compare.
    """
    if not RAZORPAY_WEBHOOK_SECRET or not signature:
        return False
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(),
        request_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ======================================================
#  PROCESS WEBHOOK EVENTS
# ======================================================
def handle_webhook_event(event: dict, supabase) -> dict:
    """
    Processes a verified Razorpay webhook event. Activates the plan and
    stores a receipt on subscription.charged, deactivates on
    subscription.cancelled / subscription.halted.
    """
    event_type = event.get("event", "")
    payload = event.get("payload", {})

    if event_type == "subscription.charged":
        return _handle_charged(payload, supabase)
    elif event_type in ("subscription.cancelled", "subscription.halted", "subscription.completed"):
        return _handle_deactivated(payload, supabase)
    else:
        # Other events (activated, pending, etc.) are just acknowledged,
        # no action needed until money actually moves (subscription.charged).
        return {"ok": True, "handled": False, "event": event_type}


def _extract_user_id(payload: dict) -> str:
    sub = payload.get("subscription", {}).get("entity", {})
    notes = sub.get("notes", {}) or {}
    return notes.get("user_id", "")


def _handle_charged(payload: dict, supabase) -> dict:
    user_id = _extract_user_id(payload)
    payment = payload.get("payment", {}).get("entity", {})
    subscription = payload.get("subscription", {}).get("entity", {})

    if not user_id:
        return {"ok": False, "message": "No user_id in subscription notes, cannot activate."}

    amount_rupees = (payment.get("amount", 0) or 0) / 100  # Razorpay amounts are in paise
    paid_until = (datetime.utcnow() + timedelta(days=35)).isoformat()  # small buffer past 30 days

    try:
        supabase.table("profiles").update({
            "is_paid": True,
            "paid_until": paid_until,
            "razorpay_subscription_id": subscription.get("id", ""),
        }).eq("id", user_id).execute()
    except Exception as e:
        return {"ok": False, "message": f"Could not update profile: {e}"}

    try:
        supabase.table("payments").insert({
            "user_id": user_id,
            "razorpay_payment_id": payment.get("id", ""),
            "razorpay_subscription_id": subscription.get("id", ""),
            "amount": amount_rupees,
            "currency": payment.get("currency", "INR"),
            "status": payment.get("status", "captured"),
            "receipt_number": payment.get("id", ""),
        }).execute()
    except Exception as e:
        # Plan is already activated above; a failed receipt log shouldn't
        # block that, just report it.
        return {"ok": True, "message": f"Activated, but receipt logging failed: {e}"}

    return {"ok": True, "message": f"Plan activated for user {user_id}."}


def _handle_deactivated(payload: dict, supabase) -> dict:
    user_id = _extract_user_id(payload)
    if not user_id:
        return {"ok": False, "message": "No user_id in subscription notes."}
    try:
        supabase.table("profiles").update({"is_paid": False}).eq("id", user_id).execute()
    except Exception as e:
        return {"ok": False, "message": f"Could not deactivate profile: {e}"}
    return {"ok": True, "message": f"Plan deactivated for user {user_id}."}


def cancel_subscription(user_id: str, subscription_id: str, supabase) -> dict:
    """
    Cancels a user's subscription. Uses cancel_at_cycle_end so they keep
    access through the period they already paid for, rather than losing
    access immediately.
    """
    if not subscription_id:
        return {"ok": False, "message": "No active subscription found for this account."}

    client = _get_client()
    if not client:
        return {"ok": False, "message": "Billing isn't fully configured right now."}

    try:
        client.subscription.cancel(subscription_id, {"cancel_at_cycle_end": 1})
    except Exception as e:
        return {"ok": False, "message": f"Could not cancel: {e}"}

    # Optimistic local flag so the UI can reflect "cancelling" immediately,
    # even before Razorpay's subscription.cancelled webhook actually fires
    # at the end of the billing cycle.
    try:
        supabase.table("profiles").update({"cancel_at_period_end": True}).eq("id", user_id).execute()
    except Exception:
        pass  # not critical, the webhook will still correctly deactivate later

    return {"ok": True, "message": "Your subscription will not renew. You'll keep access until the end of this billing period."}


# ======================================================
#  RECEIPTS
# ======================================================
def get_payment_history(user_id: str, supabase) -> list:
    if not supabase:
        return []
    try:
        res = supabase.table("payments").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
        return res.data or []
    except Exception:
        return []
