"""Real Razorpay test-mode client (OPTIONAL).

Implements the same PaymentProvider interface as SeededSandbox using the
official `razorpay` SDK. It only becomes active when RAZORPAY_KEY_ID and
RAZORPAY_KEY_SECRET are set, and it is NEVER required for tests, evaluation or
the demo. Real money movement arrives as webhook events — this client never
invents `pending_payment` hints; the seeded sandbox does that.

Endpoint usage (Razorpay API v1, test mode):
  POST /v1/payment_links            create a standard payment link
  POST /v1/payment_links/{id}/notify  send reminder for the link
  POST /v1/invoices/{id}/notify       (re)send an issued invoice
  GET  /v1/payments                  list/fetch payment details
Webhook events are verified in app/api (HMAC-SHA256 with the webhook secret).
"""
from __future__ import annotations

from app.config import settings
from app.execution.provider import PaymentProvider


def razorpay_enabled() -> bool:
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret)


class RazorpayClient(PaymentProvider):
    name = "razorpay_testmode"

    def __init__(self, key_id: str = "", key_secret: str = ""):
        self._client = None
        if razorpay_enabled():
            import razorpay  # optional dependency

            self._client = razorpay.Client(auth=(key_id or settings.razorpay_key_id, key_secret or settings.razorpay_key_secret))

    def _check(self) -> None:
        if self._client is None:
            raise RuntimeError("Razorpay credentials not configured — use the seeded sandbox or set RAZORPAY_KEY_ID/SECRET")

    # -- helpers ------------------------------------------------------------- #
    def _customer(self, customer) -> dict:
        return {
            "name": customer.org_name,
            "contact": customer.phone or "+910000000000",
            "email": customer.email or "billing@example.com",
        }

    # -- ops ------------------------------------------------------------------ #
    def send_reminder(self, invoice, customer, *, template, channel, amount_minor) -> dict:
        self._check()
        # (Re)send an issued Razorpay invoice to the customer.
        try:
            body = {"medium": channel}
            resp = self._client.invoice.notify(invoice.rzr_invoice_id, body)
            return {"ok": True, "provider_ref": str(resp.get("id", "")), "raw": resp, "pending_payment": None, "failure_code": ""}
        except Exception as exc:  # network/API failure -> bounded retry handled by caller
            return {"ok": False, "provider_ref": "", "raw": {}, "pending_payment": None, "failure_code": f"RAZORPAY_ERROR:{type(exc).__name__}"}

    def create_payment_link(self, invoice, customer, *, amount_minor, expire_hours, channel) -> dict:
        self._check()
        from datetime import datetime, timedelta

        body = {
            "amount": amount_minor,
            "currency": invoice.currency or "INR",
            "description": f"Outstanding payment — invoice {invoice.rzr_invoice_id}",
            "customer": self._customer(customer),
            "notify": {"email": bool(customer.email), "sms": bool(customer.phone)},
            "reminder_enable": True,
            "notes": {"invoice_id": invoice.rzr_invoice_id, "case_origin": "receivablesai"},
        }
        if expire_hours:
            body["expire_by"] = int((datetime.utcnow() + timedelta(hours=expire_hours)).timestamp())
        try:
            resp = self._client.payment_link.create(body)
            return {"ok": True, "provider_ref": str(resp.get("id", "")), "raw": resp, "pending_payment": None, "failure_code": ""}
        except Exception as exc:
            return {"ok": False, "provider_ref": "", "raw": {}, "pending_payment": None, "failure_code": f"RAZORPAY_ERROR:{type(exc).__name__}"}

    def retry_payment(self, payment, invoice, customer, *, attempt: int) -> dict:
        # Razorpay has no 'retry this exact payment' endpoint: retrying a failed
        # charge is modelled honestly as a fresh payment-link + notify, which is
        # what the smart-retry playbook maps to against the real API. The sandbox
        # models an immediate charge retry for evaluation purposes.
        return self.create_payment_link(invoice, customer, amount_minor=payment.amount_minor, expire_hours=72, channel="email")

    def ptp_followup(self, invoice, customer, *, followup_number: int, channel: str) -> dict:
        # A promise-to-pay follow-up is a reminder message referencing the
        # promised date; implemented as an invoice re-notification.
        return self.send_reminder(invoice, customer, template=f"ptp_{followup_number}", channel=channel, amount_minor=invoice.outstanding_minor)

    # -- unstructured-context ops --------------------------------------------- #
    # The real Razorpay API has no dedicated "corrected invoice" / "AP nudge" /
    # "promise confirmation" endpoints — against real test-mode these map to
    # invoice re-notifications / a fresh payment link carrying the right notes,
    # and the actual money movement arrives as webhook events (this client
    # never fabricates pending_payment hints; only the sandbox does).
    def resend_corrected_invoice(self, invoice, customer, *, correction_note: str, channel: str) -> dict:
        return self.create_payment_link(
            invoice, customer,
            amount_minor=invoice.outstanding_minor, expire_hours=72, channel=channel,
        )

    def request_ap_update(self, invoice, customer, *, followup_number: int, channel: str) -> dict:
        return self.send_reminder(
            invoice, customer, template=f"ap_status_{followup_number}", channel=channel, amount_minor=invoice.outstanding_minor
        )

    def confirm_ptp(self, invoice, customer, *, promised_date_iso: str, channel: str) -> dict:
        return self.send_reminder(
            invoice, customer, template="ptp_confirm", channel=channel, amount_minor=invoice.outstanding_minor
        )

    def fetch_payment(self, payment_id: str) -> dict | None:
        self._check()
        try:
            return self._client.payment.fetch(payment_id)
        except Exception:
            return None
