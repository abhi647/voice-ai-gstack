"""
NotifyAdapter — v0.1 EHR integration.

No EHR API. When a booking is captured:
  1. SMS to the practice's escalation number:
       "New booking request: Jane Smith, cleaning, Tuesday afternoon.
        Patient: +15550000000. Call SID: CA123."
  2. Email to practice staff (if staff_email is configured):
       Subject: "New booking request — Jane Smith"
       Body: formatted summary with all captured details.

The practice's office manager sees it, calls the patient back, and books
the slot in their existing EHR. Zero integration complexity for v0.1.

When a customer has a real EHR API, swap ehr_adapter to "dentrix" or
"opendental" in their PracticeConfig — this adapter is never called again.
"""

import logging

from app.config import settings
from app.ehr.base import BookingRequest, BookingResult

logger = logging.getLogger(__name__)


class NotifyAdapter:
    """Send SMS + email to practice staff. No EHR API required."""

    async def submit_booking(self, req: BookingRequest) -> BookingResult:
        sms_ok = await self._send_sms(req)
        email_ok = await self._send_email(req)

        if not sms_ok and not email_ok:
            return BookingResult(
                success=False,
                adapter="notify",
                message="Both SMS and email notifications failed",
            )

        channels = []
        if sms_ok:
            channels.append("SMS")
        if email_ok:
            channels.append("email")

        return BookingResult(
            success=True,
            adapter="notify",
            message=f"Staff notified via {' + '.join(channels)}",
        )

    async def send_booking_confirmation_sms(self, req: BookingRequest) -> bool:
        """
        Text the *patient* to confirm their booking request was received.

        Example: "Hi Jane! Your appointment request at Sunrise Dental has been
        received. We'll call you to confirm the slot. Questions? Call us at +1..."

        Returns True on success, False on failure (non-raising).
        """
        if not req.patient_phone:
            return False

        if not settings.twilio_account_sid or not settings.twilio_auth_token:
            logger.warning("Twilio not configured — skipping patient confirmation SMS")
            return False

        if not settings.twilio_sms_from:
            logger.warning("twilio_sms_from not configured — skipping patient confirmation SMS")
            return False

        name_part = f"Hi {req.patient_name.split()[0]}! " if req.patient_name else ""
        service_part = f" for {req.service_type}" if req.service_type else ""
        time_part = f" around {req.requested_time}" if req.requested_time else ""
        body = (
            f"{name_part}Your appointment request{service_part}{time_part} at "
            f"{req.practice_name} has been received. "
            f"We'll call you shortly to confirm your slot. "
            f"Ref: {req.call_sid}"
        )

        try:
            from twilio.rest import Client
            client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
            client.messages.create(
                to=req.patient_phone,
                from_=settings.twilio_sms_from,
                body=body,
            )
            logger.info(f"Patient confirmation SMS sent to {req.patient_phone} for call {req.call_sid}")
            return True
        except Exception as e:
            logger.error(f"Patient confirmation SMS failed for {req.call_sid}: {e}")
            return False

    async def _send_sms(self, req: BookingRequest) -> bool:
        """
        Send an SMS to the practice's escalation number via Twilio.
        Returns True on success, False on failure (non-raising).
        """
        if not settings.twilio_account_sid or not settings.twilio_auth_token:
            logger.warning("Twilio not configured — skipping SMS notification")
            return False

        if not settings.twilio_sms_from:
            logger.warning("twilio_sms_from not configured — skipping SMS notification")
            return False

        body = _sms_body(req)

        try:
            from twilio.rest import Client
            client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
            client.messages.create(
                to=req.escalation_number,
                from_=settings.twilio_sms_from,
                body=body,
            )
            logger.info(f"Booking SMS sent to {req.escalation_number} for call {req.call_sid}")
            return True
        except Exception as e:
            logger.error(f"SMS notification failed for {req.call_sid}: {e}")
            return False

    async def _send_email(self, req: BookingRequest) -> bool:
        """
        Send an email to practice staff via SendGrid.
        Returns True on success, False on failure (non-raising).
        """
        if not req.staff_email:
            return False  # not configured for this practice — not an error

        if not settings.sendgrid_api_key:
            logger.warning("SendGrid not configured — skipping email notification")
            return False

        subject = f"New booking request — {req.patient_name or 'patient'}"
        html_body = _email_html(req)
        plain_body = _email_plain(req)

        try:
            import httpx
            resp = await httpx.AsyncClient().post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={
                    "Authorization": f"Bearer {settings.sendgrid_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "personalizations": [{"to": [{"email": req.staff_email}]}],
                    "from": {"email": settings.sendgrid_from_email, "name": "Voice AI Receptionist"},
                    "subject": subject,
                    "content": [
                        {"type": "text/plain", "value": plain_body},
                        {"type": "text/html", "value": html_body},
                    ],
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            logger.info(f"Booking email sent to {req.staff_email} for call {req.call_sid}")
            return True
        except Exception as e:
            logger.error(f"Email notification failed for {req.call_sid}: {e}")
            return False


# ---------------------------------------------------------------------------
# Message formatters
# ---------------------------------------------------------------------------


def _sms_body(req: BookingRequest) -> str:
    parts = [f"[{req.practice_name}] New booking request"]
    if req.patient_name:
        parts.append(f"Patient: {req.patient_name}")
    if req.service_type:
        parts.append(f"Service: {req.service_type}")
    if req.requested_time:
        parts.append(f"Requested: {req.requested_time}")
    parts.append(f"Phone: {req.patient_phone}")
    parts.append(f"Ref: {req.call_sid}")
    return "\n".join(parts)


def _email_plain(req: BookingRequest) -> str:
    return f"""New booking request captured by your AI receptionist.

Patient name:     {req.patient_name or "not captured"}
Patient phone:    {req.patient_phone}
Service:          {req.service_type or "not specified"}
Requested time:   {req.requested_time or "not specified"}
Notes:            {req.notes or "none"}

Please call the patient back to confirm the appointment slot in your system.

Call reference: {req.call_sid}
"""


def _email_html(req: BookingRequest) -> str:
    def row(label: str, value: str) -> str:
        return (
            f'<tr><td style="padding:4px 12px 4px 0;color:#666;white-space:nowrap">'
            f'<b>{label}</b></td>'
            f'<td style="padding:4px 0">{value}</td></tr>'
        )

    return f"""<div style="font-family:sans-serif;max-width:480px">
  <h2 style="color:#1a1a1a">New booking request</h2>
  <p style="color:#444">Your AI receptionist captured this booking. Please call the patient
  to confirm the slot in your system.</p>
  <table style="border-collapse:collapse;width:100%">
    {row("Patient", req.patient_name or "<em>not captured</em>")}
    {row("Phone", req.patient_phone)}
    {row("Service", req.service_type or "<em>not specified</em>")}
    {row("Requested time", req.requested_time or "<em>not specified</em>")}
    {row("Notes", req.notes or "<em>none</em>")}
    {row("Call ref", f'<code>{req.call_sid}</code>')}
  </table>
</div>"""
