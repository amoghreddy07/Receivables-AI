"""PaymentProvider interface.

Both `SeededSandbox` (deterministic, for evaluation + demos) and
`RazorpayClient` (real test-mode API) implement this exact contract, so the
execution layer is written once against the interface. Results are normalized
dicts; pending payments (money arriving later) are expressed as an explicit
`pending_payment` hint that the agent drains deterministically.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class PaymentProvider(ABC):
    name: str = "abstract"

    # Every op returns: {"ok": bool, "provider_ref": str, "raw": dict,
    #                    "pending_payment": dict|None, "failure_code": str}

    @abstractmethod
    def send_reminder(self, invoice, customer, *, template: str, channel: str, amount_minor: int) -> dict:
        ...

    @abstractmethod
    def create_payment_link(self, invoice, customer, *, amount_minor: int, expire_hours: int, channel: str) -> dict:
        ...

    @abstractmethod
    def retry_payment(self, payment, invoice, customer, *, attempt: int) -> dict:
        ...

    @abstractmethod
    def ptp_followup(self, invoice, customer, *, followup_number: int, channel: str) -> dict:
        ...

    # --- unstructured-context ops (bounded outreach/coordination; amounts are
    #     never edited by any of these) -------------------------------------- #
    @abstractmethod
    def resend_corrected_invoice(self, invoice, customer, *, correction_note: str, channel: str) -> dict:
        """Send the corrected invoice/documentation + a fresh payment link."""
        ...

    @abstractmethod
    def request_ap_update(self, invoice, customer, *, followup_number: int, channel: str) -> dict:
        """Light, non-escalating AP-status follow-up."""
        ...

    @abstractmethod
    def confirm_ptp(self, invoice, customer, *, promised_date_iso: str, channel: str) -> dict:
        """Confirm an explicit promise-to-pay date (agent then waits until then)."""
        ...

    def describe(self) -> str:
        return self.name
