"""
EHR adapter protocol — the interface every integration must implement.

Adding a new EHR (Dentrix, OpenDental, Eaglesoft, Curve):
  1. Create app/ehr/<name>.py implementing EHRAdapter
  2. Add a case to app/ehr/factory.py
  3. Set ehr_adapter = "<name>" in the practice's PracticeConfig

The adapter receives a BookingRequest and returns a BookingResult.
It is called by the finalize_call flow after a booking is captured.

v0.1: only the NotifyAdapter is implemented.
      All others raise NotImplementedError until a customer needs them.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class BookingRequest:
    """Everything the agent captured during the call."""
    practice_id: str
    practice_name: str
    practice_timezone: str
    # Staff contact — where to send the notification
    escalation_number: str        # practice's phone number (for SMS)
    staff_email: str | None       # practice's email (for email)
    # Patient details
    patient_name: str | None
    patient_phone: str
    service_type: str | None
    requested_time: str | None    # free text, e.g. "Tuesday afternoon"
    notes: str | None
    call_sid: str


@dataclass
class BookingResult:
    """
    What happened after the adapter ran.

    success=True does NOT mean the appointment is confirmed in the EHR —
    for NotifyAdapter it means the staff notification was sent successfully.
    """
    success: bool
    adapter: str          # which adapter handled it
    reference_id: str | None = None   # EHR appointment ID if available
    message: str = ""     # human-readable summary for the audit log


@runtime_checkable
class EHRAdapter(Protocol):
    """Protocol all EHR adapters must satisfy."""

    async def submit_booking(self, req: BookingRequest) -> BookingResult:
        """
        Submit the booking request to the EHR or notification system.
        Must not raise — return BookingResult(success=False, ...) on failure.
        """
        ...
